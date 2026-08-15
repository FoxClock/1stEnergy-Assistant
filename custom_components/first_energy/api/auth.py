"""Token lifecycle for the 1st Energy private API.

Two tokens are required on **every** authenticated request, and they have
different lifetimes and different sources:

* the **BFF token** — `POST {portal}/api/GetBffToken` with an empty body and no
  credentials at all. It is app-level, issued by Entra, and lasts one hour.
  It goes in `authorization: Bearer <token>`.
* the **access token** — `POST {api}/v1/auth/login` with the username and
  password, carrying the BFF token as its bearer. Lasts 24 hours. It goes in
  `adaptor-authorization: <token>` as a **bare** value; adding a `Bearer`
  prefix here fails.

Because the BFF token expires hourly while the access token lasts a day, a
long-lived integration spends most of its time refreshing only the former.
Re-logging in whenever the BFF token expires would send the password to the
server twenty-four times more often than necessary, so the two are tracked
separately.

No refresh endpoint was ever observed. When the access token does expire, the
only route back is a fresh login.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp

from .exceptions import ApiError, AuthenticationError

_LOGGER = logging.getLogger(__name__)

UTC = UTC

# Refresh this far before nominal expiry. Covers clock skew between us and
# Azure, plus the flight time of the request the token is about to be used on.
REFRESH_MARGIN = timedelta(minutes=2)

# Used only when a token's `exp` claim cannot be read.
FALLBACK_BFF_LIFETIME = timedelta(minutes=55)
FALLBACK_ACCESS_LIFETIME = timedelta(hours=23)


def decode_jwt_expiry(token: str) -> datetime | None:
    """Read the `exp` claim without verifying the signature.

    We are not the audience for these tokens and cannot validate them; we only
    want to know when to stop using them. A malformed token returns None, and
    the caller falls back to a conservative fixed lifetime.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, UTC)


@dataclass(slots=True)
class Token:
    value: str
    expires_at: datetime

    @property
    def is_fresh(self) -> bool:
        return datetime.now(UTC) + REFRESH_MARGIN < self.expires_at


class Authenticator:
    """Acquires and caches both tokens.

    Not internally locked: Home Assistant drives this from a single
    coordinator on the event loop, so concurrent refreshes do not arise. If a
    second caller is ever added, guard `async_headers` with an asyncio.Lock.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        api_base: str,
        portal_base: str,
        user_agent: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._api = api_base
        self._portal = portal_base
        self._ua = user_agent
        self._bff: Token | None = None
        self._access: Token | None = None

    @property
    def access_token_expiry(self) -> datetime | None:
        return self._access.expires_at if self._access else None

    def invalidate(self, *, access_token_too: bool = False) -> None:
        """Drop cached tokens so the next request re-acquires them."""
        self._bff = None
        if access_token_too:
            self._access = None

    async def async_headers(self) -> dict[str, str]:
        """Both credential headers, refreshing whichever has gone stale."""
        bff = await self._async_bff_token()
        access = await self._async_access_token(bff)
        return {
            "authorization": f"Bearer {bff}",
            "adaptor-authorization": access,
        }

    async def async_validate_credentials(self) -> None:
        """Prove the username and password work. Used by the config flow."""
        self.invalidate(access_token_too=True)
        bff = await self._async_bff_token()
        await self._async_access_token(bff)

    async def _async_bff_token(self) -> str:
        if self._bff and self._bff.is_fresh:
            return self._bff.value

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": self._portal,
            "referer": f"{self._portal}/login",
            "user-agent": self._ua,
        }
        async with self._session.post(
            f"{self._portal}/api/GetBffToken", json={}, headers=headers
        ) as response:
            body = await response.text()
            if response.status != 200:
                raise ApiError(response.status, body, dict(response.headers))

        # The response is a bare quoted JWT string, not a JSON object.
        token = body.strip().strip('"')
        if not token:
            raise ApiError(200, "GetBffToken returned an empty body")

        expiry = decode_jwt_expiry(token) or datetime.now(UTC) + FALLBACK_BFF_LIFETIME
        self._bff = Token(token, expiry)
        _LOGGER.debug("Acquired BFF token, expires %s", expiry.isoformat())
        return token

    async def _async_access_token(self, bff: str) -> str:
        if self._access and self._access.is_fresh:
            return self._access.value

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": self._portal,
            "referer": f"{self._portal}/",
            "user-agent": self._ua,
            "authorization": f"Bearer {bff}",
        }
        async with self._session.post(
            f"{self._api}/v1/auth/login",
            json={"username": self._username, "password": self._password},
            headers=headers,
        ) as response:
            body = await response.text()
            headers_out = dict(response.headers)

        if response.status == 401:
            # At the login endpoint specifically, a 401 that the application
            # itself produced does mean the credentials are wrong. An edge
            # refusal means our BFF token was stale, which is recoverable.
            error = ApiError(401, body, headers_out)
            if error.refused_at_edge:
                raise error
            raise AuthenticationError(401, body, headers_out)
        if response.status != 200:
            raise ApiError(response.status, body, headers_out)

        try:
            token = json.loads(body)["access_token"]
        except (ValueError, KeyError) as err:
            raise ApiError(200, f"login response had no access_token: {body[:200]}") from err

        expiry = decode_jwt_expiry(token) or datetime.now(UTC) + FALLBACK_ACCESS_LIFETIME
        self._access = Token(token, expiry)
        _LOGGER.debug("Acquired access token, expires %s", expiry.isoformat())
        return token
