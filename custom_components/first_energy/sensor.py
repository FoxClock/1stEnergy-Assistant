"""Sensors for the parts of an account that genuinely are current state.

Consumption deliberately does not appear here. It is a day old and belongs in
long-term statistics with historical timestamps — publishing it as a sensor
state would file yesterday's kilowatt-hours under today. See `statistics.py`.

What remains is account-level information that really is current: the balance,
the next invoice, and how far the meter data has actually reached.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_DOLLAR
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FirstEnergyConfigEntry
from .const import DOMAIN
from .coordinator import FirstEnergyCoordinator, FirstEnergyData


@dataclass(frozen=True, kw_only=True)
class FirstEnergySensorDescription(SensorEntityDescription):
    value: Callable[[FirstEnergyData], Decimal | date | str | None]


SENSORS: tuple[FirstEnergySensorDescription, ...] = (
    FirstEnergySensorDescription(
        key="balance",
        translation_key="balance",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        value=lambda data: data.balance,
    ),
    FirstEnergySensorDescription(
        key="next_invoice_amount",
        translation_key="next_invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        value=lambda data: inv.amount if (inv := data.next_invoice) else None,
    ),
    FirstEnergySensorDescription(
        key="next_invoice_due",
        translation_key="next_invoice_due",
        device_class=SensorDeviceClass.DATE,
        value=lambda data: inv.due_date if (inv := data.next_invoice) else None,
    ),
    FirstEnergySensorDescription(
        key="last_read_date",
        translation_key="last_read_date",
        device_class=SensorDeviceClass.DATE,
        # Worth surfacing: it makes the roughly one-day lag visible, so an
        # empty-looking Energy dashboard can be recognised as normal rather
        # than as a broken integration.
        value=lambda data: data.last_read_date,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FirstEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        FirstEnergySensor(coordinator, description) for description in SENSORS
    )


class FirstEnergySensor(CoordinatorEntity[FirstEnergyCoordinator], SensorEntity):
    entity_description: FirstEnergySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FirstEnergyCoordinator,
        description: FirstEnergySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account = coordinator.account
        self._attr_unique_id = f"{account.account_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account.account_id)},
            name=f"1st Energy {account.account_number}",
            manufacturer="1st Energy",
            model=account.plan_name or "Electricity",
            configuration_url="https://myaccount.1stenergy.com.au",
        )

    @property
    def native_value(self) -> Decimal | date | str | None:
        return self.entity_description.value(self.coordinator.data)
