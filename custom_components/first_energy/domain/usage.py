"""Usage models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class UsageDay:
    """One day of interval data for one register.

    The CDR endpoint returns three parallel arrays at the native meter
    resolution (5 minutes on the test account, so 288 slots): energy in kWh,
    cost in AUD, and the time-of-use band name. They are validated as
    equal-length at parse time, because downstream bucketing zips them and a
    silent length mismatch would misattribute cost to the wrong hour.

    `intervals` sums exactly to `energy_kwh`, and `costings` to `cost_aud` —
    verified across 39 days of real data with zero drift.
    """

    service_point_id: str
    register_id: str
    read_date: date
    unit_of_measure: str
    controlled_load: bool
    interval_minutes: int
    energy_kwh: float | None
    cost_aud: float | None
    intervals: tuple[float, ...] = ()
    costings: tuple[float, ...] = ()
    tou: tuple[str, ...] = ()

    @property
    def has_intervals(self) -> bool:
        """False when the request omitted `interval-reads=MIN_30`.

        In that case the API still returns 288 slots, but all zero, and
        `interval_minutes` is 0 — so never divide by it without checking.
        """
        return self.interval_minutes > 0 and any(self.intervals)

    @property
    def expected_slots(self) -> int | None:
        if self.interval_minutes <= 0:
            return None
        return 24 * 60 // self.interval_minutes
