"""Collapse interval reads into the hourly buckets Home Assistant stores.

No Home Assistant imports — this is pure arithmetic over domain objects, so it
can be tested without spinning up a recorder.

Two decisions worth stating, because both are easy to get subtly wrong:

**Buckets are keyed on UTC hours.** Home Assistant's recorder stores long-term
statistics on UTC hour boundaries, so that is what we produce. It also sidesteps
two traps. On a daylight-saving "fall back" day the local hour 02:00 happens
twice; bucketing locally would merge two distinct hours into one and lose an
hour of consumption. And 1st Energy sells into South Australia, which sits at
UTC+9:30 — local hour boundaries there do not align with UTC ones at all.
Bucketing in UTC is correct in every jurisdiction; a 5-minute slot never
straddles a UTC hour boundary because 30 minutes divides evenly by 5.

**Timestamps advance in absolute time from local midnight.** Slot *i* starts at
`local_midnight + i * interval` measured as elapsed time, not as a naive clock
increment. On a transition day the clock jumps but elapsed time does not, so
the slots stay pinned to the right instants.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo

from ..domain import UsageDay

UTC = UTC


@dataclass(frozen=True, slots=True)
class HourlyBucket:
    """One UTC hour of consumption for one register."""

    start: datetime
    register_id: str
    energy_kwh: float = 0.0
    cost_aud: float = 0.0
    energy_by_tou: dict[str, float] = field(default_factory=dict)

    @property
    def dominant_tou(self) -> str | None:
        """The time-of-use band accounting for most energy in this hour."""
        if not self.energy_by_tou:
            return None
        return max(self.energy_by_tou.items(), key=lambda kv: kv[1])[0]


@dataclass(frozen=True, slots=True)
class BucketResult:
    buckets: tuple[HourlyBucket, ...]
    warnings: tuple[str, ...] = ()


def bucket_hourly(
    days: Iterable[UsageDay],
    local_tz: tzinfo,
    *,
    register_id: str | None = None,
) -> BucketResult:
    """Collapse daily interval reads into hourly buckets, oldest first.

    `local_tz` is the service point's jurisdiction timezone, used only to
    resolve each `read_date` to an instant. Pass the register to isolate a
    single series; otherwise every register in `days` is bucketed together,
    which is only correct when there is exactly one.

    Days without populated intervals are skipped — a request made without
    `interval-reads` still returns 288 zero slots and a `readIntervalLength`
    of 0, which would otherwise produce a day of phantom zeroes.
    """
    energy: dict[tuple[datetime, str], float] = {}
    cost: dict[tuple[datetime, str], float] = {}
    tou_split: dict[tuple[datetime, str], dict[str, float]] = {}
    warnings: list[str] = []

    for day in days:
        if register_id is not None and day.register_id != register_id:
            continue
        if not day.has_intervals:
            continue

        expected = day.expected_slots
        if expected is not None and len(day.intervals) != expected:
            # Not an error: this is what a DST transition looks like. Surface
            # it so an unexpected one is visible in the log rather than silent.
            warnings.append(
                f"{day.read_date} ({day.register_id}): {len(day.intervals)} slots, "
                f"expected {expected} — daylight saving transition?")

        midnight_local = datetime.combine(
            day.read_date, datetime.min.time(), tzinfo=local_tz)
        base = midnight_local.astimezone(UTC)
        step = timedelta(minutes=day.interval_minutes)

        for i, kwh in enumerate(day.intervals):
            hour = (base + step * i).replace(minute=0, second=0, microsecond=0)
            key = (hour, day.register_id)
            energy[key] = energy.get(key, 0.0) + kwh
            if i < len(day.costings):
                cost[key] = cost.get(key, 0.0) + day.costings[i]
            if i < len(day.tou):
                band = tou_split.setdefault(key, {})
                band[day.tou[i]] = band.get(day.tou[i], 0.0) + kwh

    buckets = tuple(
        HourlyBucket(
            start=hour,
            register_id=reg,
            energy_kwh=round(energy[(hour, reg)], 6),
            cost_aud=round(cost.get((hour, reg), 0.0), 6),
            energy_by_tou={k: round(v, 6) for k, v in
                           sorted(tou_split.get((hour, reg), {}).items())},
        )
        for hour, reg in sorted(energy, key=lambda k: (k[0], k[1]))
    )
    return BucketResult(buckets=buckets, warnings=tuple(warnings))


def cumulative(
    buckets: Sequence[HourlyBucket],
    *,
    energy_offset: float = 0.0,
    cost_offset: float = 0.0,
) -> list[dict]:
    """Attach running totals, ready to hand to Home Assistant.

    The offsets must be the last `sum` already stored for these statistic IDs,
    read back from the recorder. Seeding them at zero on every poll makes the
    Energy dashboard sawtooth — each import would restart the cumulative series
    from nothing and the dashboard renders the drop as negative consumption.
    """
    rows: list[dict] = []
    running_energy = energy_offset
    running_cost = cost_offset
    for bucket in buckets:
        running_energy += bucket.energy_kwh
        running_cost += bucket.cost_aud
        rows.append({
            "start": bucket.start,
            "state": round(bucket.energy_kwh, 4),
            "sum": round(running_energy, 4),
            "cost_state": round(bucket.cost_aud, 4),
            "cost_sum": round(running_cost, 4),
        })
    return rows
