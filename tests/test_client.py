"""Client tests: token lifecycle, 401 disambiguation, window arithmetic.

Every test runs the real client against a real (local) HTTP server, so header
construction, retry logic and parsing all execute for real. Only the far end is
fake.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import aiohttp
import pytest
from conftest import load
from fake_api import APP_HEADERS, EDGE_HEADERS, UNAUTHORIZED, FakeApi, jwt

from custom_components.first_energy.api.auth import decode_jwt_expiry
from custom_components.first_energy.api.client import FirstEnergyClient
from custom_components.first_energy.api.exceptions import ApiError, AuthenticationError

UTC = UTC
EMPTY_USAGE = {"data": {"reads": []}, "meta": {"totalRecords": 0}}


@pytest.fixture
async def api(socket_enabled):
    """`socket_enabled` comes from pytest-homeassistant-custom-component.

    That plugin blocks all sockets by default, loopback included, to stop tests
    reaching the real internet. These tests deliberately run a local server, so
    they opt back in explicitly.
    """
    fake = FakeApi()
    fake.base_url = await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def client(api):
    async with aiohttp.ClientSession() as session:
        yield FirstEnergyClient(
            session, "user@example.com", "hunter2",
            api_base=api.base_url, portal_base=api.base_url, backfill_delay=0,
        )


class TestJwtDecoding:
    def test_reads_the_expiry_claim(self):
        assert decode_jwt_expiry(jwt(timedelta(hours=2))) > datetime.now(UTC)

    @pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.!!!!.c", "a.eyJ9.c"])
    def test_malformed_tokens_return_none_rather_than_raising(self, bad):
        """The caller then falls back to a conservative fixed lifetime."""
        assert decode_jwt_expiry(bad) is None


class TestAuthentication:
    async def test_sends_both_credential_headers(self, api, client):
        api.stub_auth()
        api.queue("accounts", payload=load("accounts_electricity"))
        await client.async_get_accounts()

        sent = api.for_endpoint("/v1/energy/accounts")[0].headers
        assert sent["authorization"].startswith("Bearer ")
        assert sent["adaptor-authorization"]
        # The adaptor header takes a bare token; a Bearer prefix is rejected.
        assert not sent["adaptor-authorization"].lower().startswith("bearer")

    async def test_bff_token_is_fetched_without_credentials(self, api, client):
        api.stub_auth()
        api.queue("accounts", payload=load("accounts_electricity"))
        await client.async_get_accounts()

        bff = api.for_endpoint("/api/GetBffToken")[0]
        assert "hunter2" not in bff.body
        assert bff.body in ("{}", "")

    async def test_wrong_password_is_reported_as_an_auth_failure(self, api, client):
        api.queue("bff", body=f'"{jwt()}"', always=True)
        api.queue("login", status=401, body=UNAUTHORIZED, headers=APP_HEADERS)
        with pytest.raises(AuthenticationError):
            await client.async_validate_credentials()

    async def test_edge_refusal_at_login_is_not_blamed_on_the_password(self, api, client):
        """Identical status and body — only the missing x-powered-by separates them.

        Reporting this as a bad password would send the user off to re-enter
        credentials that were never wrong.
        """
        api.queue("bff", body=f'"{jwt()}"', always=True)
        api.queue("login", status=401, body=UNAUTHORIZED, headers=EDGE_HEADERS)
        with pytest.raises(ApiError) as excinfo:
            await client.async_validate_credentials()
        assert not isinstance(excinfo.value, AuthenticationError)
        assert excinfo.value.refused_at_edge

    async def test_password_is_not_resent_on_every_request(self, api, client):
        api.stub_auth()
        api.queue("accounts", payload=load("accounts_electricity"), always=True)
        await client.async_get_accounts()
        await client.async_get_accounts()
        assert api.count("login") == 1

    async def test_expired_bff_token_refreshes_without_a_new_login(self, api, client):
        """The BFF token dies hourly; the access token lasts a day.

        Re-logging in on every BFF expiry would send the password to the server
        twenty-four times more often than necessary.
        """
        api.stub_auth(bff_ttl=timedelta(seconds=1))
        api.queue("accounts", payload=load("accounts_electricity"), always=True)
        await client.async_get_accounts()
        await client.async_get_accounts()
        assert api.count("bff") == 2
        assert api.count("login") == 1


class TestUnauthorizedRetry:
    async def test_a_single_401_is_retried_silently(self, api, client):
        """Routine case: the hourly BFF token aged out mid-poll."""
        api.stub_auth()
        api.queue("accounts", status=401, body=UNAUTHORIZED, headers=EDGE_HEADERS)
        api.queue("accounts", payload=load("accounts_electricity"))
        assert len(await client.async_get_accounts()) == 1

    async def test_edge_401_does_not_discard_the_access_token(self, api, client):
        """Only the BFF token is suspect, so the password stays unused."""
        api.stub_auth()
        api.queue("accounts", status=401, body=UNAUTHORIZED, headers=EDGE_HEADERS)
        api.queue("accounts", payload=load("accounts_electricity"))
        await client.async_get_accounts()
        assert api.count("login") == 1

    async def test_application_401_forces_a_full_reauthentication(self, api, client):
        """Here the access token itself may have expired, so re-login."""
        api.stub_auth()
        api.queue("accounts", status=401, body=UNAUTHORIZED, headers=APP_HEADERS)
        api.queue("accounts", payload=load("accounts_electricity"))
        await client.async_get_accounts()
        assert api.count("login") == 2

    async def test_a_second_401_surfaces_as_an_auth_failure(self, api, client):
        """Freshly minted tokens still rejected — now it is worth asking the user."""
        api.stub_auth()
        api.queue("accounts", status=401, body=UNAUTHORIZED, headers=APP_HEADERS, always=True)
        with pytest.raises(AuthenticationError):
            await client.async_get_accounts()

    async def test_server_errors_are_not_mistaken_for_auth_problems(self, api, client):
        api.stub_auth()
        api.queue("accounts", status=503, body="upstream unavailable")
        with pytest.raises(ApiError) as excinfo:
            await client.async_get_accounts()
        assert excinfo.value.status == 503
        assert not isinstance(excinfo.value, AuthenticationError)


class TestEndpoints:
    async def test_balance_and_invoices(self, api, client):
        api.stub_auth()
        api.queue("balance", payload=load("account_balance"))
        api.queue("invoices", payload=load("account_invoices"))
        assert await client.async_get_balance("638594")
        invoices = await client.async_get_invoices("638594")
        assert invoices[0].invoice_number

    async def test_service_point(self, api, client):
        api.stub_auth()
        api.queue("servicepoint", payload=load("servicepoint"))
        sp = await client.async_get_service_point("663701")
        assert sp.jurisdiction_code == "NSW"
        assert sp.timezone_name == "Australia/Sydney"


class TestUsageWindows:
    async def test_single_day_request_is_widened_then_filtered(self, api, client):
        """Equal dates return 400 DATE1_BEFORE_DATE2, so ask for two and trim."""
        api.stub_auth()
        api.queue("usage", payload=load("usage_recent_7d"))
        days = await client.async_get_usage("663701", date(2026, 8, 13), date(2026, 8, 13))

        query = api.for_endpoint("/usage")[0].query
        assert query["oldest-date"] == "2026-08-12"
        assert query["newest-date"] == "2026-08-13"
        assert [d.read_date for d in days] == [date(2026, 8, 13)]

    async def test_requests_five_minute_intervals_by_default(self, api, client):
        api.stub_auth()
        api.queue("usage", payload=load("usage_recent_7d"))
        await client.async_get_usage("663701", date(2026, 8, 6), date(2026, 8, 13))
        assert api.for_endpoint("/usage")[0].query["interval-reads"] == "MIN_30"

    async def test_reversed_dates_are_tolerated(self, api, client):
        api.stub_auth()
        api.queue("usage", payload=load("usage_recent_7d"))
        assert await client.async_get_usage("663701", date(2026, 8, 13), date(2026, 8, 6))

    async def test_range_is_chunked_and_stops_at_the_start_of_history(self, api, client):
        """The first empty chunk marks where the customer's account begins.

        Continuing past it would spend requests proving there is nothing there.
        """
        api.stub_auth()
        api.queue("usage", payload=load("usage_30d"))
        api.queue("usage", payload=EMPTY_USAGE)
        api.queue("usage", payload=load("usage_30d"), always=True)  # must not be reached

        days = await client.async_get_usage_range(
            "663701", date(2020, 1, 1), date(2026, 8, 13), chunk_days=30
        )
        assert api.count("usage") == 2
        assert days
        assert days[0].read_date < days[-1].read_date

    async def test_range_requests_newest_data_first(self, api, client):
        """So a long backfill is useful from its very first chunk."""
        api.stub_auth()
        api.queue("usage", payload=load("usage_30d"))
        api.queue("usage", payload=EMPTY_USAGE)
        await client.async_get_usage_range(
            "663701", date(2026, 1, 1), date(2026, 8, 13), chunk_days=30
        )
        assert api.for_endpoint("/usage")[0].query["newest-date"] == "2026-08-13"

    async def test_range_covers_the_whole_span_without_gaps(self, api, client):
        api.stub_auth()
        api.queue("usage", payload=load("usage_30d"), always=True)
        await client.async_get_usage_range(
            "663701", date(2026, 6, 15), date(2026, 8, 13), chunk_days=30
        )
        windows = [
            (r.query["oldest-date"], r.query["newest-date"])
            for r in api.for_endpoint("/usage")
        ]
        assert windows[0][1] == "2026-08-13"
        assert windows[-1][0] == "2026-06-15"
        for (older_start, _), (_, newer_end) in zip(windows[1:], windows[:-1], strict=True):
            assert date.fromisoformat(newer_end) - date.fromisoformat(older_start) > timedelta(0)
