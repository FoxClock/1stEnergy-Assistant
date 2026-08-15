"""1st Energy API client.

Vendored inside the integration package because HACS copies only
`custom_components/<domain>/` onto the target system — a sibling library
would not be importable there without publishing it to PyPI first.

Nothing in this package or in `domain/` may import `homeassistant`. That rule
is what keeps the client independently testable and extractable later.
"""

from .client import FirstEnergyClient
from .exceptions import (
    ApiError,
    AuthenticationError,
    FirstEnergyError,
    TokenExpiredError,
)
from .parsers import ParseError

__all__ = [
    "ApiError",
    "AuthenticationError",
    "FirstEnergyClient",
    "FirstEnergyError",
    "ParseError",
    "TokenExpiredError",
]
