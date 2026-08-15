#!/usr/bin/env python3
"""Capture sanitised 1st Energy API fixtures for integration development.

Run this on a machine on the same residential connection the integration will
run from. Cloudflare has been observed to issue the BFF token to residential
IPs without a challenge; a datacenter IP may be challenged instead.

    export FIRSTENERGY_USER='you@example.com'
    python capture_fixtures.py                 # prompts for the password

Writes JSON into ./dev/fixtures/. Every response is passed through a sanitiser
that replaces account identifiers, the NMI, names, addresses, contact details
and tokens with stable pseudonyms — stable meaning the same real value maps to
the same fake value in every file, so cross-file references still line up and
the fixtures remain useful as test data.

Sanitisation is best-effort on an undocumented API. READ THE FILES BEFORE YOU
COMMIT THEM. `dev/fixtures/_index.json` lists what was captured; skim a couple
of payloads for anything the scrubber did not recognise.

Standard library only, so it runs anywhere with Python 3.11+.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

API = "https://endpoint-firstenergy-mobileapp-prod-dchufubea3frdfhc.a01.azurefd.net"
PORTAL = "https://myaccount.1stenergy.com.au"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

OUT = Path(__file__).resolve().parent / "fixtures"
DELAY = 1.5  # seconds between requests; this API may already have rate-limited us once

# Response headers worth keeping — they distinguish an application rejection
# from an edge one, which is the only way to read a 401 correctly.
KEEP_HEADERS = (
    "x-powered-by", "x-v", "api-supported-versions", "x-azure-ref",
    "content-type", "etag", "retry-after",
)


# ---------------------------------------------------------------- sanitiser

# Keys whose *values* are replaced wholesale, regardless of content.
PII_KEYS = {
    "firstname", "lastname", "middlename", "fullname", "name", "customername",
    "email", "emailaddress", "phone", "phonenumber", "mobile", "mobilenumber",
    "dateofbirth", "dob", "addressline1", "addressline2", "addressline3",
    "streetaddress", "street", "suburb", "city", "postcode", "postalcode",
    "unitnumber", "streetnumber", "streetname", "fulladdress", "address",
    "bankaccountnumber", "bsb", "cardnumber", "abn",
    # CDR uses the Australian Postal Address File (PAF) structure, which
    # scatters an address across many narrow fields. Each is weak alone but
    # they reassemble into a precise street address.
    "mailingname", "flatunitnumber", "flatunittype", "floorlevelnumber",
    "floorleveltype", "lotnumber", "buildingname1", "buildingname2",
    "streetsuffix", "localityname", "postaldeliverynumber", "postaldeliverytype",
    # dpid is the Australia Post Delivery Point Identifier — an 8-digit code
    # that resolves to one specific letterbox. More identifying than the
    # address text it sits beside.
    "dpid", "deliverypointidentifier", "gnaf", "gnafid",
    "access_token", "refresh_token", "token", "bfftoken", "authorization",
}

# Keys whose values are *pseudonymised consistently* — they're structural, so
# tests need them to stay internally consistent, but they identify the account.
ID_KEYS = {
    "accountid", "accountnumber", "servicepointid", "nationalmeteringid",
    "nmi", "meterid", "customerid", "invoicenumber", "premiseid", "siteid",
}


# Free-text substitution only applies to values at least this long. Shorter
# ones (a postcode, a 4-digit meter number) would corrupt unrelated numeric
# data — "2000" appears in dates, kWh readings and IDs alike.
MIN_SUBSTRING_LEN = 6


class Sanitiser:
    """Replaces identifying values with stable pseudonyms.

    Runs in two passes over each payload. The first collects every identifier
    and PII value into the replacement map; the second applies it. Two passes
    matter because a free-text field can mention an account number *before* the
    walk reaches the field that defines it — a single pass would leave that
    mention untouched.

    Pseudonyms are stable for the whole run, so the same real value maps to the
    same fake in every fixture and cross-file references still line up. The
    counter is global rather than per-key so that distinct real values never
    collide on one fake (an accountId and a servicePointId must stay tellable
    apart in test data).
    """

    def __init__(self) -> None:
        self.map: dict[str, str] = {}      # real -> fake, for substring scrubbing
        self._n = 0
        self.hits = 0

    def _fake(self, key: str, value: str) -> str:
        if value in self.map:
            return self.map[value]
        self._n += 1
        n = self._n
        if key in ("nationalmeteringid", "nmi"):
            fake = f"999999{n:04d}"                    # NMIs are 10-11 chars
        elif key == "invoicenumber":
            fake = f"INV{n:06d}"
        elif value.isdigit():
            fake = str(100000 + n)
        else:
            fake = f"{key}-{n:04d}"
        self.map[value] = fake
        return fake

    # -- pass 1 ---------------------------------------------------------
    def _collect(self, node, key: str | None = None) -> None:
        k = (key or "").lower().replace("_", "").replace("-", "")
        if isinstance(node, dict):
            for kk, vv in node.items():
                self._collect(vv, kk)
            return
        if isinstance(node, list):
            for v in node:
                self._collect(v, key)
            return
        if node is None or node == "":
            return
        if k in ID_KEYS:
            self._fake(k, str(node))
        elif k in PII_KEYS and isinstance(node, str):
            # Register long PII values so they're also scrubbed where they
            # appear inside free text elsewhere in the payload.
            if len(node) >= MIN_SUBSTRING_LEN:
                self.map.setdefault(node, f"<redacted:{k}>")

    def _scrub_strings(self, text: str) -> str:
        for real, fake in self.map.items():
            if len(real) >= MIN_SUBSTRING_LEN and real in text:
                text = text.replace(real, fake)
                self.hits += 1
        return text

    # -- pass 2 ---------------------------------------------------------
    def _apply(self, node, key: str | None = None):
        k = (key or "").lower().replace("_", "").replace("-", "")
        if isinstance(node, dict):
            return {kk: self._apply(vv, kk) for kk, vv in node.items()}
        if isinstance(node, list):
            return [self._apply(v, key) for v in node]

        if k in PII_KEYS and node is not None:
            self.hits += 1
            return None if node == "" else f"<redacted:{k}>"

        if k in ID_KEYS and node is not None:
            fake = self._fake(k, str(node))
            self.hits += 1
            return int(fake) if isinstance(node, int) and fake.isdigit() else fake

        if isinstance(node, str):
            # JWTs and anything shaped like one.
            if re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", node) and len(node) > 100:
                self.hits += 1
                return "<redacted:jwt>"
            return self._scrub_strings(node)
        return node

    def walk(self, node, key: str | None = None):
        self._collect(node, key)
        return self._apply(node, key)


# ------------------------------------------------------------------ client

class ApiError(Exception):
    pass


class Client:
    def __init__(self, token=None, bearer=None):
        self.token = token
        self.bearer = bearer

    def _headers(self) -> dict[str, str]:
        h = {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": PORTAL,
            "referer": PORTAL + "/",
            "user-agent": UA,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-fapi-interaction-id": "1.0",
            "x-fapi-auth-date": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "x-fapi-customer-ip-address": "127.0.0.1",
        }
        if self.bearer:
            h["authorization"] = f"Bearer {self.bearer}"
        if self.token:
            h["adaptor-authorization"] = self.token
        return h

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API + path, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), dict(e.headers)
        except urllib.error.URLError as e:
            return 0, f"network error: {e.reason}", {}


def get_bff_token() -> str:
    req = urllib.request.Request(
        PORTAL + "/api/GetBffToken", data=b"{}", method="POST",
        headers={"accept": "application/json, text/plain, */*",
                 "content-type": "application/json",
                 "accept-encoding": "identity",
                 "origin": PORTAL, "referer": PORTAL + "/login",
                 "user-agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(errors="replace").strip('"')


# ------------------------------------------------------------------ capture

class Capture:
    def __init__(self, api: Client, sanitiser: Sanitiser) -> None:
        self.api = api
        self.san = sanitiser
        self.index: list[dict] = []
        OUT.mkdir(parents=True, exist_ok=True)

    def grab(self, name: str, path: str, note: str = "") -> dict | None:
        """Fetch, sanitise, write. Returns the *unsanitised* parsed body so the
        caller can keep using real IDs for subsequent requests."""
        time.sleep(DELAY)
        status, text, headers = self.api.request("GET", path)
        entry = {
            "name": name,
            "path": self.san._scrub_strings(path),
            "status": status,
            "note": note,
            "headers": {k: v for k, v in headers.items()
                        if k.lower() in KEEP_HEADERS},
        }

        parsed = None
        if status == 200 and text:
            try:
                parsed = json.loads(text)
            except ValueError:
                entry["note"] += " (non-JSON body)"

        if parsed is not None:
            clean = self.san.walk(parsed)
            (OUT / f"{name}.json").write_text(
                json.dumps(clean, indent=2, sort_keys=False))
            entry["file"] = f"{name}.json"
            entry["bytes"] = len(text)
        else:
            entry["body"] = self.san._scrub_strings(text[:400])

        self.index.append(entry)
        flag = "ok " if status == 200 else "!! "
        print(f"  {flag}{status}  {name}")
        return parsed

    def write_index(self) -> None:
        (OUT / "_index.json").write_text(json.dumps({
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "note": "Sanitised fixtures. Review before committing.",
            "requests": self.index,
        }, indent=2))


def main() -> int:
    user = os.environ.get("FIRSTENERGY_USER") or input("1st Energy email: ")
    password = os.environ.get("FIRSTENERGY_PASS") or getpass("Password: ")

    print("\nauthenticating…")
    bff = get_bff_token()
    status, text, _ = Client(bearer=bff).request(
        "POST", "/v1/auth/login", {"username": user, "password": password})
    del password
    if status != 200:
        print(f"login failed: HTTP {status}\n{text[:300]}")
        print("\nA 401 here is ambiguous — it looks identical for a bad "
              "password and a stale token. Re-run before assuming the "
              "credentials are wrong.")
        return 1

    access = json.loads(text)["access_token"]
    api = Client(token=access, bearer=bff)
    san = Sanitiser()
    cap = Capture(api, san)
    print("logged in\n")

    # -- discovery -----------------------------------------------------
    print("discovery:")
    accounts = cap.grab("accounts_electricity",
                        "/v1/energy/accounts?fuel-type=ELECTRICITY")
    cap.grab("accounts_gas", "/v1/energy/accounts?fuel-type=GAS",
             "open question: gas payload shape")

    if not accounts or not accounts.get("data", {}).get("accounts"):
        print("no electricity accounts returned — stopping")
        cap.write_index()
        return 1

    acct = accounts["data"]["accounts"][0]
    acct_id = acct["accountId"]
    spid = acct["servicePoints"][0]["servicePointId"]

    if len(accounts["data"]["accounts"]) > 1:
        print(f"  note: {len(accounts['data']['accounts'])} accounts found — "
              "capturing the first only")

    cap.grab("servicepoint", f"/v1/electricity/servicepoints/{spid}")

    # -- account -------------------------------------------------------
    print("\naccount:")
    cap.grab("account_detail", f"/v1/accounts/{acct_id}")
    cap.grab("account_balance", f"/v1/accounts/{acct_id}/balance")
    cap.grab("account_invoices", f"/v1/accounts/{acct_id}/invoices")

    # -- usage windows -------------------------------------------------
    # Data lags ~1 day; step back 2 to be safely inside available data.
    end = date.today() - timedelta(days=2)
    print("\nusage windows:")

    def usage(name, oldest, newest, intervals="MIN_30", size=40, note=""):
        p = (f"/v1/electricity/servicepoints/{spid}/usage"
             f"?page=1&page-size={size}&interval-reads={intervals}"
             f"&oldest-date={oldest}&newest-date={newest}")
        return cap.grab(name, p, note)

    usage("usage_recent_7d", end - timedelta(days=7), end,
          note="primary shape: 5-min slots + daily aggregates")
    usage("usage_30d", end - timedelta(days=30), end, size=40,
          note="reconciliation: sum(intervalReads) vs aggregateValue")
    usage("usage_no_intervals", end - timedelta(days=3), end,
          intervals="NONE", size=10, note="288 zero slots expected")

    # DST days — the handoff flags these as validated only synthetically.
    # NSW: clocks back first Sunday April, forward first Sunday October.
    print("\nDST transitions (open question in the handoff):")
    usage("usage_dst_autumn_2026", date(2026, 4, 3), date(2026, 4, 7),
          note="2026-04-05 NSW clocks back: expect a 25-hour day")
    usage("usage_dst_spring_2025", date(2025, 10, 3), date(2025, 10, 7),
          note="2025-10-05 NSW clocks forward: expect a 23-hour day")

    # -- how far back does history go? ---------------------------------
    print("\nhistory depth probe (decides backfill scope):")
    for years in (1, 2, 3, 5):
        anchor = end.replace(year=end.year - years)
        usage(f"usage_depth_{years}y", anchor - timedelta(days=3), anchor,
              intervals="NONE", size=5,
              note=f"data available {years} year(s) back?")

    # -- page-size ceiling ---------------------------------------------
    print("\npage-size probe (open question: documented max unknown):")
    for size in (100, 250, 500):
        p = (f"/v1/electricity/servicepoints/{spid}/usage"
             f"?page=1&page-size={size}&interval-reads=NONE")
        cap.grab(f"pagesize_{size}", p, f"requested page-size={size}")

    # -- weekly / monthly ----------------------------------------------
    print("\nweekly and monthly envelopes:")
    wk = end.isoformat()
    cap.grab("week_costed", f"/v1/electricity/account/{acct_id}/usage/"
             f"{spid}/{wk}?monthly=false&costed=true",
             "`consumption` holds AUD here, not kWh")
    cap.grab("week_uncosted", f"/v1/electricity/account/{acct_id}/usage/"
             f"{spid}/{wk}?monthly=false&costed=false",
             "`consumption` holds kWh here")
    cap.grab("month_costed", f"/v1/electricity/account/{acct_id}/usage/"
             f"{spid}/{wk}?monthly=true&costed=true",
             "open question: monthly inner field shape")

    cap.write_index()
    print(f"\n{len(cap.index)} requests, {san.hits} values scrubbed")
    print(f"written to {OUT}")
    print("\nReview the files before committing — sanitisation is best-effort "
          "on an undocumented API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
