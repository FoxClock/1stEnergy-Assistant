"""Statistics import tests against a real recorder database.

These are the tests worth having. The parsing and bucketing layers are pure and
easy to reason about; the recorder interaction is where the failure modes are
subtle, invisible in the log, and only show up as a wrong-looking Energy
dashboard days later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.first_energy.const import STAT_COST, STAT_ENERGY, statistic_id
from custom_components.first_energy.services.statistics import HourlyBucket
from custom_components.first_energy.statistics import (
    async_import_buckets,
    async_last_statistic_hour,
)

UTC = UTC
NMI = "4310274874"
START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def buckets(count: int, *, start: datetime = START, kwh: float = 1.0, cost: float = 0.25):
    return [
        HourlyBucket(
            start=start + timedelta(hours=i),
            register_id="E1",
            energy_kwh=kwh,
            cost_aud=cost,
            energy_by_tou={"Off Peak": kwh},
        )
        for i in range(count)
    ]


async def read_back(hass: HomeAssistant, kind: str, *, end=None):
    stat_id = statistic_id(NMI, kind)
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        START - timedelta(hours=1),
        end or START + timedelta(days=7),
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return rows.get(stat_id, [])


class TestImport:
    async def test_energy_and_cost_are_both_written(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        written = await async_import_buckets(
            hass, NMI, buckets(24), display_name="1st Energy test"
        )
        assert written == 24

        energy = await read_back(hass, STAT_ENERGY)
        cost = await read_back(hass, STAT_COST)
        assert len(energy) == 24
        assert len(cost) == 24
        assert energy[0]["state"] == pytest.approx(1.0)
        assert cost[0]["state"] == pytest.approx(0.25)

    async def test_sums_are_cumulative(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        await async_import_buckets(hass, NMI, buckets(5), display_name="test")
        energy = await read_back(hass, STAT_ENERGY)
        assert [r["sum"] for r in energy] == pytest.approx([1.0, 2.0, 3.0, 4.0, 5.0])

    async def test_a_later_import_continues_the_running_total(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        """The second poll must not restart the sum at zero.

        If it did, the cumulative series would fall off a cliff and the Energy
        dashboard would render the drop as negative consumption.
        """
        await async_import_buckets(hass, NMI, buckets(3), display_name="test")
        await async_wait_recording_done(hass)
        await async_import_buckets(
            hass, NMI, buckets(3, start=START + timedelta(hours=3)), display_name="test"
        )

        energy = await read_back(hass, STAT_ENERGY)
        assert len(energy) == 6
        sums = [r["sum"] for r in energy]
        assert sums == pytest.approx([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        assert all(b >= a for a, b in pairwise(sums))

    async def test_empty_import_is_a_no_op(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        assert await async_import_buckets(hass, NMI, [], display_name="test") == 0


class TestLastStatisticHour:
    async def test_none_before_anything_is_stored(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        assert await async_last_statistic_hour(hass, NMI) is None

    async def test_reports_the_newest_stored_hour(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        await async_import_buckets(hass, NMI, buckets(10), display_name="test")
        await async_wait_recording_done(hass)
        last = await async_last_statistic_hour(hass, NMI)
        assert last == START + timedelta(hours=9)


class TestReimportIsIdempotent:
    """The coordinator re-requests a rolling window on every poll.

    That overlap is deliberate — it repairs gaps left by a failed poll without
    any reconciliation logic. But it means the same hours get written
    repeatedly, and the cumulative `sum` must not grow each time. Nothing in
    the log would reveal this; it surfaces days later as an Energy dashboard
    reporting far more consumption than the meter did.
    """

    async def test_reimporting_the_same_hours_does_not_inflate_the_total(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        await async_import_buckets(hass, NMI, buckets(24), display_name="test")
        await async_wait_recording_done(hass)
        await async_import_buckets(hass, NMI, buckets(24), display_name="test")

        energy = await read_back(hass, STAT_ENERGY)
        assert len(energy) == 24
        assert energy[-1]["sum"] == pytest.approx(24.0)

    async def test_an_overlapping_window_continues_correctly(
        self, recorder_mock, enable_custom_integrations, hass: HomeAssistant
    ):
        """The realistic case: poll two overlaps poll one, then extends past it."""
        await async_import_buckets(hass, NMI, buckets(24), display_name="test")
        await async_wait_recording_done(hass)
        # Hours 12-35: twelve already stored, twelve new.
        await async_import_buckets(
            hass, NMI, buckets(24, start=START + timedelta(hours=12)), display_name="test"
        )

        energy = await read_back(hass, STAT_ENERGY)
        assert len(energy) == 36
        # One kWh per hour, so after 36 distinct hours the total is 36.
        assert energy[-1]["sum"] == pytest.approx(36.0)
        sums = [r["sum"] for r in energy]
        assert all(b >= a for a, b in pairwise(sums))
