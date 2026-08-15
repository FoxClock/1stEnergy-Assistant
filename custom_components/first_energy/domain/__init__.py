"""Domain models for 1st Energy accounts, meters and usage.

Frozen slotted dataclasses. Nothing here imports Home Assistant — that rule is
what keeps the client independently testable and extractable later.

On numeric types: the API is inconsistent, so we follow it rather than fight
it. Values that arrive as decimal *strings* (invoice amounts, account balance)
become `Decimal`, because they are exact and money should stay exact. Values
that arrive as JSON *floats* (interval energy and cost) stay `float` — routing
them through Decimal would dress up precision that was already lost upstream.
"""

from .account import Account, Invoice
from .meter import Meter, Register, ServicePoint
from .usage import UsageDay

__all__ = ["Account", "Invoice", "Meter", "Register", "ServicePoint", "UsageDay"]
