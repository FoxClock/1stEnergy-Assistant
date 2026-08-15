"""Async client for the 1st Energy private API.

Undocumented and reverse-engineered from browser traffic; 1st Energy may change
or withdraw it without notice. Every request carries both credential headers
(see `auth.py`) plus the browser-ish header set the portal sends — the CDR
`x-fapi-*` trio and the `sec-fetch-*` group. Drop-testing showed most of these
are unnecessary, but `origin` and `user-agent` were never tested in isolation
and the cost of keeping them is nil.

No Home Assistant imports: this module takes an `aiohttp.ClientSession` and
returns domain objects.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import aiohttp

from ..domain import Account, Invoice, ServicePoint, UsageDay
from .auth import Authenticator
from .exceptions import ApiError, AuthenticationError, FirstEnergyError
from .parsers import (
    parse_accounts,
    parse_balance,
    parse_invoices,
    parse_service_point,
    parse_usage,
)

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://endpoint-firstenergy-mobileapp-prod-dchufubea3frdfhc.a01.azurefd.net"
PORTAL_BASE = "https://myaccount.1stenergy.com.au"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Days per usage request. The API returned 46 days in a single page without
# complaint, but the true ceiling is unknown — the test account was too new to
# probe it. Chunking keeps each response a predictable size and each failure
# cheap to retry.
WINDOW_DAYS = 30

# Pause between chunks during a backfill. This endpoint is undocumented and a
# burst of rapid logins during the original reverse-engineering may already have
# tripped a temporary block, so backfill deliberately ambles.
BACKFILL_DELAY_SECONDS = 2.0


class FirstEnergyClient:
    """Typed access to the endpoints this integration needs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        api_base: str = API_BASE,
        portal_base: str = PORTAL_BASE,
        backfill_delay: float = BACKFILL_DELAY_SECONDS,
    ) -> None:
        self._session = session
        self._api = api_base.rstrip("/")
        self._backfill_delay = backfill_delay
        self._auth = Authenticator(
            session,
            username,
            password,
            api_base=self._api,
            portal_base=portal_base.rstrip("/"),
            user_agent=USER_AGENT,
        )

    # ------------------------------------------------------------ transport

    def _base_headers(self) -> dict[str, str]:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": PORTAL_BASE,
            "referer": f"{PORTAL_BASE}/",
            "user-agent": USER_AGENT,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-fapi-interaction-id": "1.0",
            "x-fapi-auth-date": now,
            "x-fapi-customer-ip-address": "127.0.0.1",
        }

    async def _async_get(self, path: str) -> Any:
        """GET with one automatic retry after re-authenticating.

        A 401 here is ambiguous — the body is byte-identical whether the bearer
        was missing, the token expired, or the password is wrong. The response
        *headers* are the only signal: an application rejection carries
        `x-powered-by`, an edge refusal carries only `x-azure-ref`.

        The overwhelmingly common case is simply that the one-hour BFF token
        aged out mid-poll, which is routine. So the first 401 always triggers a
        silent re-auth and retry. Only a second 401 — after presenting freshly
        minted tokens — is treated as a real credential failure worth
        interrupting the user for.
        """
        for attempt in (1, 2):
            headers = {**self._base_headers(), **await self._auth.async_headers()}
            try:
                async with self._session.get(
                    f"{self._api}{path}", headers=headers, timeout=DEFAULT_TIMEOUT
                ) as response:
                    status = response.status
                    body = await response.text()
                    response_headers = dict(response.headers)
            except aiohttp.ClientError as err:
                raise FirstEnergyError(f"request to {path} failed: {err}") from err
            except TimeoutError as err:
                raise FirstEnergyError(f"request to {path} timed out") from err

            if status == 401 and attempt == 1:
                _LOGGER.debug("401 on %s; refreshing tokens and retrying", path)
                # An edge refusal usually means only the BFF token is stale.
                # An application refusal may mean the access token has expired
                # too, so discard both and start clean.
                error = ApiError(401, body, response_headers)
                self._auth.invalidate(access_token_too=not error.refused_at_edge)
                continue

            if status == 401:
                raise AuthenticationError(status, body, response_headers)
            if not 200 <= status < 300:
                raise ApiError(status, body, response_headers)

            return await _decode(body, path)

        raise FirstEnergyError(f"unreachable retry state for {path}")

    # ------------------------------------------------------------ endpoints

    async def async_validate_credentials(self) -> None:
        await self._auth.async_validate_credentials()

    async def async_get_accounts(self, fuel_type: str = "ELECTRICITY") -> tuple[Account, ...]:
        payload = await self._async_get(f"/v1/energy/accounts?fuel-type={fuel_type}")
        return parse_accounts(payload)

    async def async_get_service_point(self, service_point_id: str) -> ServicePoint:
        payload = await self._async_get(f"/v1/electricity/servicepoints/{service_point_id}")
        return parse_service_point(payload)

    async def async_get_balance(self, account_id: str) -> Decimal:
        payload = await self._async_get(f"/v1/accounts/{account_id}/balance")
        return parse_balance(payload)

    async def async_get_invoices(self, account_id: str) -> tuple[Invoice, ...]:
        payload = await self._async_get(f"/v1/accounts/{account_id}/invoices")
        return parse_invoices(payload)

    async def async_get_usage(
        self,
        service_point_id: str,
        oldest: date,
        newest: date,
        *,
        with_intervals: bool = True,
    ) -> tuple[UsageDay, ...]:
        """One window of usage. Both dates inclusive.

        The API rejects equal `oldest-date` and `newest-date` with
        `400 DATE1_BEFORE_DATE2`, so a single-day request is widened by a day
        and the surplus filtered out here. Callers should not have to know that.
        """
        if newest < oldest:
            oldest, newest = newest, oldest

        requested_oldest = oldest
        if oldest == newest:
            oldest = oldest - timedelta(days=1)

        span = (newest - oldest).days + 1
        query = [
            "page=1",
            f"page-size={span + 1}",
            f"interval-reads={'MIN_30' if with_intervals else 'NONE'}",
            f"oldest-date={oldest.isoformat()}",
            f"newest-date={newest.isoformat()}",
        ]
        payload = await self._async_get(
            f"/v1/electricity/servicepoints/{service_point_id}/usage?" + "&".join(query)
        )
        days = parse_usage(payload)
        return tuple(d for d in days if requested_oldest <= d.read_date <= newest)

    async def async_get_usage_range(
        self,
        service_point_id: str,
        oldest: date,
        newest: date,
        *,
        with_intervals: bool = True,
        chunk_days: int = WINDOW_DAYS,
        delay: float | None = None,
    ) -> tuple[UsageDay, ...]:
        """A long range, fetched newest-first in chunks.

        Newest-first matters for backfill: the recent data users actually care
        about lands in the Energy dashboard first, so a long historical sync is
        useful from its first chunk rather than only at the end.

        Stops early on the first empty chunk. Available history is bounded by
        when the customer joined — the reference account had 46 days — and
        walking further back just wastes requests on an endpoint worth being
        gentle with.
        """
        if newest < oldest:
            oldest, newest = newest, oldest
        pause = self._backfill_delay if delay is None else delay

        collected: list[UsageDay] = []
        window_end = newest
        first = True
        while window_end >= oldest:
            window_start = max(oldest, window_end - timedelta(days=chunk_days - 1))
            if not first and pause:
                await asyncio.sleep(pause)
            first = False

            chunk = await self.async_get_usage(
                service_point_id, window_start, window_end, with_intervals=with_intervals
            )
            if not chunk:
                _LOGGER.debug(
                    "No data for %s..%s; assuming start of available history",
                    window_start, window_end,
                )
                break
            collected.extend(chunk)
            window_end = window_start - timedelta(days=1)

        collected.sort(key=lambda d: (d.read_date, d.register_id))
        return tuple(collected)


async def _decode(body: str, path: str) -> Any:
    import json

    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError as err:
        raise ApiError(200, f"{path}: response was not JSON: {body[:200]}") from err
