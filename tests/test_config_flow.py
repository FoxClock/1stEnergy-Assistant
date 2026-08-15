"""Config and reauthentication flow tests, run against a real Home Assistant.

Fixture order in these signatures is load-bearing: `recorder_mock` must resolve
before `hass`, because it prepares the statistics database that Home Assistant
opens during startup. `enable_custom_integrations` then makes HA willing to
look inside `custom_components/` at all. Requesting them the other way round
fails with an opaque assertion inside pytest-homeassistant-custom-component.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.first_energy.api.exceptions import ApiError, AuthenticationError
from custom_components.first_energy.const import CONF_ACCOUNT_ID, DOMAIN
from custom_components.first_energy.domain import Account

ACCOUNT = Account(
    account_id="638594", account_number="516645", open_status="OPEN",
    creation_date=None, plan_name="Residential Time of Use",
    service_point_ids=("663701",),
)
SECOND_ACCOUNT = Account(
    account_id="700001", account_number="700111", open_status="OPEN",
    creation_date=None, plan_name="Residential", service_point_ids=("770001",),
)
NO_CONNECTION = Account(
    account_id="900001", account_number="900111", open_status="OPEN",
    creation_date=None, plan_name=None, service_point_ids=(),
)

CREDENTIALS = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "hunter2"}


def patch_client(*, accounts=(ACCOUNT,), validate_error=None):
    """Patch the client used by the flow, leaving the flow logic itself real."""
    validate = AsyncMock(side_effect=validate_error)
    return patch.multiple(
        "custom_components.first_energy.config_flow.FirstEnergyClient",
        async_validate_credentials=validate,
        async_get_accounts=AsyncMock(return_value=tuple(accounts)),
    )


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


class TestUserFlow:
    async def test_single_account_is_added_without_extra_questions(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        result = await start(hass)
        assert result["type"] is FlowResultType.FORM

        with patch_client(), patch(
            "custom_components.first_energy.async_setup_entry", return_value=True
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "1st Energy 516645"
        assert result["data"][CONF_ACCOUNT_ID] == "638594"
        assert result["data"][CONF_PASSWORD] == "hunter2"

    async def test_multiple_accounts_prompt_for_a_choice(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        result = await start(hass)
        with patch_client(accounts=(ACCOUNT, SECOND_ACCOUNT)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "account"

        with patch_client(accounts=(ACCOUNT, SECOND_ACCOUNT)), patch(
            "custom_components.first_energy.async_setup_entry", return_value=True
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_ACCOUNT_ID: "700001"}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ACCOUNT_ID] == "700001"

    async def test_accounts_without_a_connection_are_not_offered(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        """A gas-only account has no service point and nothing to poll."""
        result = await start(hass)
        with patch_client(accounts=(NO_CONNECTION,)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "no_accounts"}

    async def test_wrong_password_is_reported_as_such(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        result = await start(hass)
        with patch_client(validate_error=AuthenticationError(401, "nope")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_transport_failure_is_not_reported_as_a_bad_password(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        """An edge refusal carries the same body as a credential rejection.

        Telling the user their password is wrong when it is not sends them
        round a pointless loop of resetting a working password.
        """
        result = await start(hass)
        with patch_client(validate_error=ApiError(401, "nope", {"x-azure-ref": "0"})):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_the_same_account_cannot_be_added_twice(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        MockConfigEntry(
            domain=DOMAIN, unique_id="638594", data={**CREDENTIALS, CONF_ACCOUNT_ID: "638594"}
        ).add_to_hass(hass)

        result = await start(hass)
        with patch_client():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestReauth:
    async def test_new_password_is_saved(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id="638594",
            data={**CREDENTIALS, CONF_ACCOUNT_ID: "638594"},
        )
        entry.add_to_hass(hass)

        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"

        with patch_client(), patch(
            "custom_components.first_energy.async_setup_entry", return_value=True
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "new-password"}
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_PASSWORD] == "new-password"
        # The username must survive: reauth only ever asks for the password.
        assert entry.data[CONF_USERNAME] == "user@example.com"

    async def test_still_wrong_password_keeps_asking(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id="638594",
            data={**CREDENTIALS, CONF_ACCOUNT_ID: "638594"},
        )
        entry.add_to_hass(hass)

        result = await entry.start_reauth_flow(hass)
        with patch_client(validate_error=AuthenticationError(401, "nope")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "still-wrong"}
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
