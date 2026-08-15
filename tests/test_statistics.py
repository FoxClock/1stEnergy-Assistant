"""Hourly bucketing tests, including the daylight-saving cases.

Real DST payloads are unavailable — the test account was opened in June 2026,
after the April transition, and the next one is 4 October 2026. These build
synthetic days with the correct slot counts instead, which exercises the
timestamp arithmetic even though it cannot prove the API's own behaviour on
those days.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.first_energy.api.parsers import parse_usage
from custom_components.first_energy.domain import UsageDay
from custom_components.first_energy.services.statistics import bucket_hourly, cumulative

SYDNEY = ZoneInfo("Australia/Sydney")
ADELAIDE = ZoneInfo("Australia/Adelaide")  # UTC+9:30 — 1st Energy sells into SA
UTC = UTC


def make_day(day: date, slots: int, *, kwh: float = 0.1, minutes: int = 5) -> UsageDay:
    return UsageDay(
        service_point_id="1", register_id="E1", read_date=day,
        unit_of_measure="kWh", controlled_load=False, interval_minutes=minutes,
        energy_kwh=kwh * slots, cost_aud=0.01 * slots,
        intervals=tuple([kwh] * slots),
        costings=tuple([0.01] * slots),
        tou=tuple(["Off Peak"] * slots),
    )


class TestRealData:
    def test_thirty_days_yields_the_expected_bucket_count(self, usage_30d_payload):
        days = parse_usage(usage_30d_payload)
        result = bucket_hourly(days, SYDNEY)
        assert len(result.buckets) == 31 * 24
        assert not result.warnings

    def test_bucketed_energy_matches_the_daily_aggregates(self, usage_30d_payload):
        days = parse_usage(usage_30d_payload)
        result = bucket_hourly(days, SYDNEY)
        assert sum(b.energy_kwh for b in result.buckets) == pytest.approx(
            sum(d.energy_kwh for d in days), abs=1e-4)

    def test_bucketed_cost_matches_the_daily_aggregates(self, usage_30d_payload):
        days = parse_usage(usage_30d_payload)
        result = bucket_hourly(days, SYDNEY)
        assert sum(b.cost_aud for b in result.buckets) == pytest.approx(
            sum(d.cost_aud for d in days), abs=1e-3)

    def test_buckets_land_on_utc_hour_boundaries(self, usage_7d_payload):
        result = bucket_hourly(parse_usage(usage_7d_payload), SYDNEY)
        for b in result.buckets:
            assert b.start.tzinfo is not None
            assert (b.start.minute, b.start.second) == (0, 0)

    def test_buckets_are_contiguous_and_ordered(self, usage_7d_payload):
        starts = [b.start for b in bucket_hourly(parse_usage(usage_7d_payload), SYDNEY).buckets]
        assert starts == sorted(starts)
        assert all(b - a == timedelta(hours=1) for a, b in pairwise(starts))

    def test_peak_window_is_captured(self, usage_7d_payload):
        """Endeavour residential peak runs 16:00-20:00 local."""
        result = bucket_hourly(parse_usage(usage_7d_payload), SYDNEY)
        peak = [b for b in result.buckets if b.dominant_tou == "Peak"]
        assert peak
        local_hours = {b.start.astimezone(SYDNEY).hour for b in peak}
        assert local_hours <= {16, 17, 18, 19}

    def test_days_without_intervals_produce_nothing(self, usage_no_intervals_payload):
        result = bucket_hourly(parse_usage(usage_no_intervals_payload), SYDNEY)
        assert result.buckets == ()


class TestDaylightSaving:
    def test_short_day_yields_23_buckets(self):
        """4 October 2026, NSW clocks forward: 23 hours, 276 five-minute slots."""
        result = bucket_hourly([make_day(date(2026, 10, 4), 276)], SYDNEY)
        assert len(result.buckets) == 23
        assert result.warnings and "daylight saving" in result.warnings[0]

    def test_long_day_yields_25_distinct_buckets(self):
        """5 April 2026, NSW clocks back: 25 hours, 300 slots.

        The repeated local hour 02:00 must stay two separate buckets. Keying on
        local time would silently merge them and lose an hour of consumption.
        """
        result = bucket_hourly([make_day(date(2026, 4, 5), 300)], SYDNEY)
        assert len(result.buckets) == 25
        assert len({b.start for b in result.buckets}) == 25
        assert sum(b.energy_kwh for b in result.buckets) == pytest.approx(30.0)

    def test_normal_day_raises_no_warning(self):
        result = bucket_hourly([make_day(date(2026, 8, 12), 288)], SYDNEY)
        assert len(result.buckets) == 24
        assert not result.warnings


class TestHalfHourOffsetJurisdiction:
    def test_south_australia_still_lands_on_utc_hours(self):
        """Adelaide is UTC+9:30, so local midnight is 14:30 UTC.

        Home Assistant stores statistics on UTC hour boundaries regardless, so
        the first and last buckets of the local day are partial. What must not
        happen is a bucket starting at half past.
        """
        result = bucket_hourly([make_day(date(2026, 8, 12), 288)], ADELAIDE)
        assert all(b.start.minute == 0 for b in result.buckets)
        assert sum(b.energy_kwh for b in result.buckets) == pytest.approx(28.8)
        assert len(result.buckets) == 25  # two partial hours at the edges


class TestCumulative:
    def test_running_totals_accumulate(self, usage_7d_payload):
        buckets = bucket_hourly(parse_usage(usage_7d_payload), SYDNEY).buckets
        rows = cumulative(buckets)
        assert rows[0]["sum"] == pytest.approx(rows[0]["state"])
        assert rows[-1]["sum"] == pytest.approx(
            sum(b.energy_kwh for b in buckets), abs=1e-3)

    def test_offset_seeds_the_series(self, usage_7d_payload):
        """Without this the Energy dashboard sawtooths on every poll."""
        buckets = bucket_hourly(parse_usage(usage_7d_payload), SYDNEY).buckets
        plain = cumulative(buckets)
        seeded = cumulative(buckets, energy_offset=1000.0, cost_offset=50.0)
        assert seeded[0]["sum"] == pytest.approx(plain[0]["sum"] + 1000.0)
        assert seeded[-1]["cost_sum"] == pytest.approx(plain[-1]["cost_sum"] + 50.0)

    def test_sums_never_decrease(self, usage_30d_payload):
        """A decreasing sum is what the dashboard renders as negative usage."""
        rows = cumulative(bucket_hourly(parse_usage(usage_30d_payload), SYDNEY).buckets)
        sums = [r["sum"] for r in rows]
        assert all(b >= a for a, b in pairwise(sums))
