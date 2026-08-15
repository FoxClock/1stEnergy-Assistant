"""1st Energy integration for Home Assistant.

Unofficial. Reads an Australian 1st Energy account through the private API
behind their customer portal, and feeds hourly energy and cost statistics into
the Energy dashboard.

Meter data lags roughly a day, so this integration is historical by nature:
statistics are backdated to the hours they belong to rather than published as
live sensor states.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, AuthenticationError, FirstEnergyClient, FirstEnergyError
from .const import CONF_ACCOUNT_ID
from .coordinator import FirstEnergyCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type FirstEnergyConfigEntry = ConfigEntry[FirstEnergyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: FirstEnergyConfigEntry) -> bool:
    client = FirstEnergyClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    try:
        accounts = await client.async_get_accounts()
    except AuthenticationError as err:
        from homeassistant.exceptions import ConfigEntryAuthFailed

        raise ConfigEntryAuthFailed("1st Energy rejected the stored credentials") from err
    except (ApiError, FirstEnergyError) as err:
        raise ConfigEntryNotReady(f"Cannot reach 1st Energy: {err}") from err

    account_id = entry.data[CONF_ACCOUNT_ID]
    account = next((a for a in accounts if a.account_id == account_id), None)
    if account is None:
        raise ConfigEntryNotReady(
            f"Account {account_id} is no longer visible on this login")

    coordinator = FirstEnergyCoordinator(hass, entry, client, account)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FirstEnergyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
