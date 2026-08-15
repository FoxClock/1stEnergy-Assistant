"""Pure functions mapping 1st Energy payloads onto domain objects.

No network, no Home Assistant — every function here takes an already-decoded
JSON payload and returns domain objects, so the whole module is testable
against captured fixtures.

The API is undocumented and inconsistent about types: identifiers arrive as
strings in some payloads, money as decimal strings, energy as floats. Parsers
normalise on the way in so nothing downstream has to care.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain import Account, Invoice, Meter, Register, ServicePoint, UsageDay
from .exceptions import FirstEnergyError


class ParseError(FirstEnergyError):
    """A payload did not have the shape we require."""


def _date(value: Any) -> date | None:
    """ISO date, or None. Tolerates the full timestamps some fields carry."""
    if not value:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _data(payload: Any, *, what: str) -> dict:
    if not isinstance(payload, dict):
        raise ParseError(f"{what}: expected an object, got {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"{what}: payload has no 'data' object")
    return data


# ---------------------------------------------------------------- accounts

def parse_accounts(payload: Any) -> tuple[Account, ...]:
    """`GET /v1/energy/accounts?fuel-type=ELECTRICITY`.

    An account with no service points is still returned — the gas response on
    the test account is exactly that. Such accounts are parsed rather than
    dropped, so the caller can tell "no gas connection" from "request failed".
    """
    data = _data(payload, what="accounts")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise ParseError("accounts: 'data.accounts' is not a list")

    out = []
    for raw in accounts:
        plans = raw.get("plans") or []
        plan_name = None
        if plans and isinstance(plans[0], dict):
            plan_name = plans[0].get("nickname") or (
                plans[0].get("planOverview") or {}).get("displayName")

        sp_ids = tuple(
            str(sp["servicePointId"])
            for sp in (raw.get("servicePoints") or [])
            if isinstance(sp, dict) and sp.get("servicePointId") is not None
        )

        out.append(Account(
            account_id=str(raw["accountId"]),
            account_number=str(raw.get("accountNumber", "")),
            open_status=str(raw.get("openStatus", "")),
            creation_date=_date(raw.get("creationDate")),
            plan_name=plan_name,
            service_point_ids=sp_ids,
        ))
    return tuple(out)


def parse_balance(payload: Any) -> Decimal:
    """`GET /v1/accounts/{id}/balance` — a decimal string, kept exact."""
    data = _data(payload, what="balance")
    balance = _decimal(data.get("balance"))
    if balance is None:
        raise ParseError("balance: missing or unparseable 'balance'")
    return balance


def parse_invoices(payload: Any) -> tuple[Invoice, ...]:
    """`GET /v1/accounts/{id}/invoices`, newest first."""
    data = _data(payload, what="invoices")
    invoices = data.get("invoices")
    if not isinstance(invoices, list):
        raise ParseError("invoices: 'data.invoices' is not a list")

    out = []
    for raw in invoices:
        period = raw.get("period") or {}
        discount = (raw.get("payOnTimeDiscount") or {}).get("discountAmount")
        out.append(Invoice(
            invoice_number=str(raw.get("invoiceNumber", "")),
            issue_date=_date(raw.get("issueDate")),
            due_date=_date(raw.get("dueDate")),
            amount=_decimal(raw.get("invoiceAmount")) or Decimal("0"),
            gst_amount=_decimal(raw.get("gstAmount")) or Decimal("0"),
            payment_status=str(raw.get("paymentStatus", "")),
            period_start=_date(period.get("startDate")),
            period_end=_date(period.get("endDate")),
            pay_on_time_discount=_decimal(discount),
        ))
    out.sort(key=lambda i: (i.issue_date or date.min), reverse=True)
    return tuple(out)


# ----------------------------------------------------------- service point

def parse_service_point(payload: Any) -> ServicePoint:
    """`GET /v1/electricity/servicepoints/{spid}`."""
    data = _data(payload, what="service point")

    meters = []
    for raw_meter in data.get("meters") or []:
        specs = raw_meter.get("specifications") or {}
        registers = tuple(
            Register(
                register_id=str(r.get("registerId", "")),
                status=str(r.get("status", "")),
                unit_of_measure=str(r.get("unitOfMeasure", "")),
                controlled_load=bool(r.get("controlledLoad", False)),
                network_tariff_code=r.get("networkTariffCode"),
                time_of_day=r.get("timeOfDay"),
                multiplier=_float(r.get("multiplier")),
            )
            for r in (raw_meter.get("registers") or [])
        )
        meters.append(Meter(
            meter_id=str(raw_meter.get("meterId", "")),
            status=specs.get("status"),
            registers=registers,
        ))

    nmi = data.get("nationalMeteringId")
    if not nmi:
        raise ParseError("service point: missing 'nationalMeteringId'")

    return ServicePoint(
        service_point_id=str(data.get("servicePointId", "")),
        nmi=str(nmi),
        status=str(data.get("servicePointStatus", "")),
        jurisdiction_code=data.get("jurisdictionCode"),
        is_generator=bool(data.get("isGenerator", False)),
        meters=tuple(meters),
    )


# ------------------------------------------------------------------ usage

def parse_usage(payload: Any) -> tuple[UsageDay, ...]:
    """`GET /v1/electricity/servicepoints/{spid}/usage`, oldest first.

    With `interval-reads=MIN_30` the response carries three parallel arrays at
    the meter's native resolution — energy, cost and time-of-use band. Their
    lengths are checked here: downstream bucketing zips them, and a mismatch
    would silently attribute cost to the wrong hour rather than fail loudly.

    Days whose slot count disagrees with `readIntervalLength` are NOT rejected.
    That is exactly what a daylight-saving transition looks like, and dropping
    those days would punch a hole in the Energy dashboard twice a year.
    """
    data = _data(payload, what="usage")
    reads = data.get("reads")
    if not isinstance(reads, list):
        raise ParseError("usage: 'data.reads' is not a list")

    out = []
    for raw in reads:
        interval = raw.get("intervalRead") or {}
        energy = tuple(_float(v) or 0.0 for v in (interval.get("intervalReads") or []))
        cost = tuple(_float(v) or 0.0 for v in (interval.get("intervalCostings") or []))
        tou = tuple(str(v) for v in (interval.get("intervalTOU") or []))

        if cost and len(cost) != len(energy):
            raise ParseError(
                f"usage {raw.get('readStartDate')}: {len(energy)} energy slots "
                f"but {len(cost)} cost slots")
        if tou and len(tou) != len(energy):
            raise ParseError(
                f"usage {raw.get('readStartDate')}: {len(energy)} energy slots "
                f"but {len(tou)} time-of-use slots")

        read_date = _date(raw.get("readStartDate"))
        if read_date is None:
            raise ParseError(f"usage: unparseable readStartDate {raw.get('readStartDate')!r}")

        out.append(UsageDay(
            service_point_id=str(raw.get("servicePointId", "")),
            register_id=str(raw.get("registerId", "")),
            read_date=read_date,
            unit_of_measure=str(raw.get("unitOfMeasure", "kWh")),
            controlled_load=bool(raw.get("controlledLoad", False)),
            interval_minutes=int(interval.get("readIntervalLength") or 0),
            energy_kwh=_float(interval.get("aggregateValue")),
            cost_aud=_float(interval.get("aggregateCosting")),
            intervals=energy,
            costings=cost,
            tou=tou,
        ))

    out.sort(key=lambda u: (u.read_date, u.register_id))
    return tuple(out)
