"""Metering models: service point, meters, registers."""

from __future__ import annotations

from dataclasses import dataclass, field

# The API reports a jurisdiction code, not a timezone. Interval reads are
# indexed from *local* midnight, so resolving this correctly is what keeps
# buckets aligned — particularly in South Australia and the Northern Territory,
# which sit at half-hour offsets, and Queensland and Western Australia, which
# do not observe daylight saving at all.
JURISDICTION_TIMEZONES = {
    "NSW": "Australia/Sydney",
    "ACT": "Australia/Sydney",
    "VIC": "Australia/Melbourne",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
    "TAS": "Australia/Hobart",
    "WA": "Australia/Perth",
    "NT": "Australia/Darwin",
}


@dataclass(frozen=True, slots=True)
class Register:
    """A single meter register.

    A service point commonly carries registers that are no longer live —
    the test account has a REMOVED register alongside a CURRENT one. Only
    `CURRENT` registers should produce statistics, or a decommissioned meter's
    history gets mixed into the active series.
    """

    register_id: str
    status: str
    unit_of_measure: str
    controlled_load: bool = False
    network_tariff_code: str | None = None
    time_of_day: str | None = None
    multiplier: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status.upper() == "CURRENT"


@dataclass(frozen=True, slots=True)
class Meter:
    meter_id: str
    status: str | None
    registers: tuple[Register, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ServicePoint:
    """A physical connection point, identified by its NMI.

    The NMI is the stable identifier across account changes, which is why
    statistic IDs key on it rather than on `service_point_id`.
    """

    service_point_id: str
    nmi: str
    status: str
    jurisdiction_code: str | None
    is_generator: bool
    meters: tuple[Meter, ...] = field(default_factory=tuple)

    @property
    def active_registers(self) -> tuple[Register, ...]:
        return tuple(r for m in self.meters for r in m.registers if r.is_active)

    @property
    def timezone_name(self) -> str:
        """IANA timezone for this connection's jurisdiction.

        Falls back to Sydney, which covers the majority of 1st Energy's
        footprint. A wrong guess here shifts every bucket, so an unknown
        jurisdiction is worth logging where this is used.
        """
        code = (self.jurisdiction_code or "").upper()
        return JURISDICTION_TIMEZONES.get(code, "Australia/Sydney")

    @property
    def has_controlled_load(self) -> bool:
        return any(r.controlled_load for r in self.active_registers)
