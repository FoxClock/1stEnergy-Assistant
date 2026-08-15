"""Account-level models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Account:
    """A 1st Energy billing account.

    `service_point_ids` comes from the account listing rather than the service
    point detail endpoint, so it is available before any further requests.
    """

    account_id: str
    account_number: str
    open_status: str
    creation_date: date | None
    plan_name: str | None
    service_point_ids: tuple[str, ...]

    @property
    def is_open(self) -> bool:
        return self.open_status.upper() == "OPEN"


@dataclass(frozen=True, slots=True)
class Invoice:
    """An issued invoice.

    Amounts are `Decimal` because the API sends them as decimal strings.
    `pay_on_time_discount` is the discount amount, not the discounted total.
    """

    invoice_number: str
    issue_date: date | None
    due_date: date | None
    amount: Decimal
    gst_amount: Decimal
    payment_status: str
    period_start: date | None
    period_end: date | None
    pay_on_time_discount: Decimal | None = None

    @property
    def is_paid(self) -> bool:
        return self.payment_status.upper() == "PAID"
