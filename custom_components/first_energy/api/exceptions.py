"""
Author:     Hayden Foxwell
Purpose:
    Custom exceptions for the 1st Energy API client.

    A 401 from this API is ambiguous: the body is byte-identical for a missing
    bearer token, an expired token and a wrong password. The only way to tell
    them apart is the response headers — an application rejection carries
    `x-powered-by` / `x-v` / `api-supported-versions`, while a rejection at the
    Azure Front Door edge carries only `x-azure-ref`.

    This matters for Home Assistant. Raising ConfigEntryAuthFailed on an edge
    rejection would nag the user for their password every time the one-hour BFF
    token expires, which is routine rather than an error.
"""

from __future__ import annotations


class FirstEnergyError(Exception):
    """Base for everything this client raises."""


class ApiError(FirstEnergyError):
    """A non-2xx response from the API."""

    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.headers = headers or {}

    @property
    def refused_at_edge(self) -> bool:
        """True when Azure Front Door rejected this before the app saw it.

        Application responses carry `x-powered-by`; edge refusals carry only
        `x-azure-ref`. An edge refusal usually means a stale BFF token, which
        is recoverable by re-authenticating rather than by asking the user for
        new credentials.
        """
        lowered = {k.lower() for k in self.headers}
        return "x-powered-by" not in lowered and "x-azure-ref" in lowered


class AuthenticationError(ApiError):
    """Credentials were rejected by the application itself."""


class TokenExpiredError(ApiError):
    """A token expired; re-authenticate and retry. Not a user-facing failure."""
