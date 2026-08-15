"""A real HTTP server standing in for the 1st Energy API.

Chosen over `aioresponses`, which patches `ClientSession` internals and lags
behind aiohttp releases — it is broken against aiohttp 3.14 at time of writing,
and pinning the test suite to an older aiohttp than Home Assistant ships would
mean testing a stack nobody runs.

This serves real responses over loopback instead, so the genuine aiohttp client
path executes: connection handling, header encoding, status parsing. Tests queue
responses per endpoint and afterwards inspect exactly what was sent.
"""

from __future__ import annotations

import base64
import json
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

UTC = UTC

# An application-level response carries these; an Azure Front Door refusal does
# not. This is the only way to tell an expired token from a wrong password.
APP_HEADERS = {"x-powered-by": "ASP.NET", "x-v": "1.7.2"}
EDGE_HEADERS = {"x-azure-ref": "0abc"}
UNAUTHORIZED = json.dumps(
    {"Code": "Unauthorized", "Message": "You are not authorized to access this resource."}
)


def jwt(expires_in: timedelta = timedelta(hours=1)) -> str:
    """A structurally valid unsigned JWT carrying a real `exp` claim."""
    exp = int((datetime.now(UTC) + expires_in).timestamp())
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class RecordedRequest:
    __slots__ = ("body", "headers", "method", "path", "query")

    def __init__(self, method: str, path: str, query: dict, headers: dict, body: str) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.method} {self.path}?{self.query}>"


class FakeApi:
    """Queue responses per endpoint, then inspect what the client sent."""

    ENDPOINTS = ("bff", "login", "accounts", "servicepoint", "usage", "balance", "invoices")

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._queued: dict[str, deque] = defaultdict(deque)
        self._fallback: dict[str, tuple] = {}
        self.server: TestServer | None = None

    # -- setup ---------------------------------------------------------

    def queue(
        self,
        endpoint: str,
        *,
        status: int = 200,
        payload: Any = None,
        body: str | None = None,
        headers: dict | None = None,
        always: bool = False,
    ) -> None:
        """Add one response. `always` makes it the fallback once the queue drains."""
        if endpoint not in self.ENDPOINTS:
            raise ValueError(f"unknown endpoint {endpoint!r}")
        text = body if body is not None else json.dumps(payload if payload is not None else {})
        entry = (status, text, headers or {})
        if always:
            self._fallback[endpoint] = entry
        else:
            self._queued[endpoint].append(entry)

    def stub_auth(self, *, bff_ttl: timedelta = timedelta(hours=1)) -> None:
        """Working credentials, refreshable indefinitely."""
        self.queue("bff", body=f'"{jwt(bff_ttl)}"', always=True)
        self.queue(
            "login",
            payload={"access_token": jwt(timedelta(hours=24)), "refresh_token": jwt()},
            always=True,
        )

    def count(self, endpoint: str) -> int:
        paths = {
            "bff": "/api/GetBffToken",
            "login": "/v1/auth/login",
            "accounts": "/v1/energy/accounts",
            "usage": "/usage",
            "balance": "/balance",
            "invoices": "/invoices",
        }
        needle = paths[endpoint]
        return sum(1 for r in self.requests if needle in r.path)

    def for_endpoint(self, endpoint: str) -> list[RecordedRequest]:
        return [r for r in self.requests if endpoint in r.path]

    # -- server --------------------------------------------------------

    async def _handle(self, endpoint: str, request: web.Request) -> web.Response:
        self.requests.append(RecordedRequest(
            request.method, request.path, dict(request.query),
            {k.lower(): v for k, v in request.headers.items()},
            await request.text(),
        ))
        if self._queued[endpoint]:
            status, text, headers = self._queued[endpoint].popleft()
        elif endpoint in self._fallback:
            status, text, headers = self._fallback[endpoint]
        else:
            status, text, headers = 500, f"no response queued for {endpoint}", {}
        return web.Response(
            status=status, text=text,
            headers={"content-type": "application/json", **headers},
        )

    async def start(self) -> str:
        app = web.Application()

        def route(endpoint: str):
            async def handler(request: web.Request) -> web.Response:
                return await self._handle(endpoint, request)
            return handler

        app.router.add_post("/api/GetBffToken", route("bff"))
        app.router.add_post("/v1/auth/login", route("login"))
        app.router.add_get("/v1/energy/accounts", route("accounts"))
        app.router.add_get("/v1/electricity/servicepoints/{spid}", route("servicepoint"))
        app.router.add_get("/v1/electricity/servicepoints/{spid}/usage", route("usage"))
        app.router.add_get("/v1/accounts/{aid}/balance", route("balance"))
        app.router.add_get("/v1/accounts/{aid}/invoices", route("invoices"))

        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()
