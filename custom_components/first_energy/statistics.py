"""Import 1st Energy history into Home Assistant's long-term statistics.

This is the Home Assistant boundary: it takes hourly buckets produced by
`services.statistics` (which knows nothing of HA) and writes them to the
recorder.

Why external statistics rather than sensors: the data arrives roughly a day
late. A normal sensor with `state_class: total_increasing` records whatever
value it holds at the moment the recorder samples it, which would file
Thursday's consumption under Friday and skew every Energy dashboard total.
External statistics are written with explicit historical timestamps, so each
kilowatt-hour lands in the hour it was actually used.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STAT_COST, STAT_ENERGY, statistic_id
from .services.statistics import HourlyBucket

_LOGGER = logging.getLogger(__name__)


async def _async_baseline_sum(
    hass: HomeAssistant, stat_id: str, first_hour: datetime
) -> float:
    """The cumulative total to resume from when writing at `first_hour`.

    Seeding correctly is not optional, and there are two distinct cases.

    When the new data starts *after* everything stored, the last recorded `sum`
    is the baseline — the ordinary append. Starting from zero instead would make
    the series collapse and climb again, which the Energy dashboard renders as
    negative consumption.

    When the new data *overlaps* what is stored — which happens on every single
    poll, because the coordinator deliberately re-requests a rolling window —
    the last stored `sum` already contains the hours about to be rewritten.
    Resuming from it would count them twice, inflating every subsequent hour and
    silently overstating consumption for as long as the integration runs. The
    baseline in that case is the total as at the hour immediately *before* the
    first one being written.

    Recorder access is synchronous and must not touch the event loop, hence the
    executor hops.
    """
    recorder = get_instance(hass)
    last = await recorder.async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"start", "sum"}
    )
    if not last or stat_id not in last or not last[stat_id]:
        return 0.0

    row = last[stat_id][0]
    last_start = dt_util.utc_from_timestamp(row["start"])
    if first_hour > last_start:
        return float(row.get("sum") or 0.0)

    previous_hour = first_hour - timedelta(hours=1)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, previous_hour, first_hour, {stat_id}, "hour", None, {"sum"},
    )
    series = rows.get(stat_id) or []
    if not series:
        # Rewriting from the very beginning of the series, or across a gap with
        # nothing before it. Either way there is no earlier total to carry.
        return 0.0
    return float(series[-1].get("sum") or 0.0)


def _metadata(
    stat_id: str, name: str, unit: str | None, unit_class: str | None
) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_of_measurement=unit,
        unit_class=unit_class,
    )


async def async_import_buckets(
    hass: HomeAssistant,
    nmi: str,
    buckets: Sequence[HourlyBucket],
    *,
    display_name: str,
) -> int:
    """Write energy and cost statistics for one meter. Returns hours written.

    Re-importing an hour already stored is safe and intentional — the recorder
    replaces rows matching a statistic id and start time. That is what lets the
    coordinator re-request a rolling window every poll and quietly repair gaps
    left by a failed run, without any explicit reconciliation logic.
    """
    if not buckets:
        return 0

    energy_id = statistic_id(nmi, STAT_ENERGY)
    cost_id = statistic_id(nmi, STAT_COST)

    first_hour = buckets[0].start
    energy_sum = await _async_baseline_sum(hass, energy_id, first_hour)
    cost_sum = await _async_baseline_sum(hass, cost_id, first_hour)

    energy_rows: list[StatisticData] = []
    cost_rows: list[StatisticData] = []
    for bucket in buckets:
        energy_sum += bucket.energy_kwh
        cost_sum += bucket.cost_aud
        energy_rows.append(
            StatisticData(start=bucket.start, state=bucket.energy_kwh, sum=energy_sum)
        )
        cost_rows.append(
            StatisticData(start=bucket.start, state=bucket.cost_aud, sum=cost_sum)
        )

    async_add_external_statistics(
        hass,
        _metadata(energy_id, f"{display_name} energy",
                  UnitOfEnergy.KILO_WATT_HOUR, "energy"),
        energy_rows,
    )
    async_add_external_statistics(
        hass,
        _metadata(cost_id, f"{display_name} cost", CURRENCY_DOLLAR, None),
        cost_rows,
    )

    _LOGGER.debug(
        "Imported %d hourly buckets for %s (%s .. %s)",
        len(buckets), nmi, buckets[0].start.isoformat(), buckets[-1].start.isoformat(),
    )
    return len(buckets)


async def async_last_statistic_hour(hass: HomeAssistant, nmi: str):
    """Timestamp of the newest stored hour, or None if nothing is stored yet.

    Used to decide between a first-run backfill and a routine rolling update.
    """
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id(nmi, STAT_ENERGY), True, {"start"}
    )
    stat_id = statistic_id(nmi, STAT_ENERGY)
    if not rows or stat_id not in rows or not rows[stat_id]:
        return None
    return dt_util.utc_from_timestamp(rows[stat_id][0]["start"])
