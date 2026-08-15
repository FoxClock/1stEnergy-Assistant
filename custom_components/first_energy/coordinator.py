"""Polling coordinator and historical backfill."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthenticationError, FirstEnergyClient, FirstEnergyError
from .const import (
    CONF_BACKFILL_DONE,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    ROLLING_WINDOW_DAYS,
    UPDATE_INTERVAL,
)
from .domain import Account, Invoice, ServicePoint
from .services.statistics import bucket_hourly
from .statistics import async_import_buckets

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FirstEnergyData:
    """What the sensors read. Statistics go to the recorder, not here."""

    account: Account
    service_point: ServicePoint
    balance: Decimal | None = None
    invoices: tuple[Invoice, ...] = ()
    last_read_date: date | None = None
    hours_imported: int = 0

    @property
    def next_invoice(self) -> Invoice | None:
        unpaid = [i for i in self.invoices if not i.is_paid and i.due_date]
        return min(unpaid, key=lambda i: i.due_date) if unpaid else None

    @property
    def latest_invoice(self) -> Invoice | None:
        return self.invoices[0] if self.invoices else None


class FirstEnergyCoordinator(DataUpdateCoordinator[FirstEnergyData]):
    """Polls a rolling window and keeps the Energy dashboard fed."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FirstEnergyClient,
        account: Account,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {account.account_number}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.account = account
        self._service_point: ServicePoint | None = None
        self._backfill_started = False

    async def _async_setup(self) -> None:
        """One-off discovery, run before the first refresh."""
        if not self.account.service_point_ids:
            raise UpdateFailed(
                f"Account {self.account.account_number} has no electricity connection")
        self._service_point = await self._call(
            self.client.async_get_service_point(self.account.service_point_ids[0])
        )
        _LOGGER.debug(
            "Discovered NMI %s in %s (%s)",
            self._service_point.nmi,
            self._service_point.jurisdiction_code,
            self._service_point.timezone_name,
        )

    async def _async_update_data(self) -> FirstEnergyData:
        service_point = self._service_point
        assert service_point is not None  # _async_setup guarantees this

        balance = await self._call(self.client.async_get_balance(self.account.account_id))
        invoices = await self._call(self.client.async_get_invoices(self.account.account_id))

        # Meter data lags about a day, so "today" is never available. Ask for a
        # rolling window rather than a single day: re-importing hours we already
        # hold is free, and it silently repairs anything a failed poll missed.
        newest = dt_util.now().date() - timedelta(days=1)
        oldest = newest - timedelta(days=ROLLING_WINDOW_DAYS)
        days = await self._call(
            self.client.async_get_usage(service_point.service_point_id, oldest, newest)
        )

        hours = await self._async_import(service_point, days)

        if not self._backfill_started and not self.config_entry.data.get(CONF_BACKFILL_DONE):
            self._backfill_started = True
            self.config_entry.async_create_background_task(
                self.hass, self._async_backfill(service_point), f"{DOMAIN}_backfill"
            )

        return FirstEnergyData(
            account=self.account,
            service_point=service_point,
            balance=balance,
            invoices=invoices,
            last_read_date=days[-1].read_date if days else None,
            hours_imported=hours,
        )

    async def _async_import(self, service_point: ServicePoint, days) -> int:
        """Bucket and write, isolating each active register."""
        if not days:
            return 0
        tz = ZoneInfo(service_point.timezone_name)
        result = bucket_hourly(days, tz)
        for warning in result.warnings:
            _LOGGER.info("Interval count anomaly: %s", warning)
        return await async_import_buckets(
            self.hass,
            service_point.nmi,
            result.buckets,
            display_name=f"1st Energy {service_point.nmi}",
        )

    async def _async_backfill(self, service_point: ServicePoint) -> None:
        """Walk history backwards once, in the background.

        Deliberately not part of `_async_update_data`. A full history walk is
        many sequential requests against a rate-limit-shy endpoint; running it
        inside the update would block setup past Home Assistant's timeout and
        leave the integration looking broken while it worked perfectly.
        """
        newest = dt_util.now().date() - timedelta(days=1)
        oldest = newest - timedelta(days=MAX_BACKFILL_DAYS)
        _LOGGER.info("Starting history backfill for NMI %s", service_point.nmi)
        try:
            days = await self.client.async_get_usage_range(
                service_point.service_point_id, oldest, newest
            )
            hours = await self._async_import(service_point, days)
        except FirstEnergyError as err:
            # Not fatal: the rolling window keeps working and the next restart
            # retries, since the completion flag is only set on success.
            _LOGGER.warning("Backfill for %s did not complete: %s", service_point.nmi, err)
            self._backfill_started = False
            return

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_DONE: True},
        )
        _LOGGER.info(
            "Backfill complete for NMI %s: %d hourly buckets from %d days",
            service_point.nmi, hours, len(days),
        )

    async def _call(self, awaitable):
        """Translate client errors into the outcomes Home Assistant expects."""
        try:
            return await awaitable
        except AuthenticationError as err:
            # Only raised after a retry with freshly minted tokens also failed,
            # so this really is the password rather than an expired session.
            raise ConfigEntryAuthFailed(
                "1st Energy rejected the stored credentials") from err
        except ApiError as err:
            raise UpdateFailed(f"1st Energy API returned HTTP {err.status}") from err
        except FirstEnergyError as err:
            raise UpdateFailed(str(err)) from err
