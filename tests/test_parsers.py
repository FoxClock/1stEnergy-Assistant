"""Parser tests against sanitised captures of the live API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from custom_components.first_energy.api.parsers import (
    ParseError,
    parse_accounts,
    parse_balance,
    parse_invoices,
    parse_service_point,
    parse_usage,
)


class TestAccounts:
    def test_parses_the_electricity_account(self, accounts_payload):
        accounts = parse_accounts(accounts_payload)
        assert len(accounts) == 1
        account = accounts[0]
        assert account.is_open
        assert account.creation_date == date(2026, 6, 26)
        assert account.plan_name == "Residential Time of Use"
        assert len(account.service_point_ids) == 1

    def test_account_without_service_points_still_parses(self, gas_payload):
        """The gas response carries an account but no connection.

        Parsing rather than dropping it lets the caller distinguish "no gas
        service" from "the request failed".
        """
        accounts = parse_accounts(gas_payload)
        assert len(accounts) == 1
        assert accounts[0].service_point_ids == ()

    def test_rejects_a_payload_without_data(self):
        with pytest.raises(ParseError, match="no 'data' object"):
            parse_accounts({"nope": True})


class TestBalance:
    def test_balance_stays_exact(self, balance_payload):
        balance = parse_balance(balance_payload)
        assert balance == Decimal("151.87")
        # Decimal, not float — money must not acquire binary rounding.
        assert isinstance(balance, Decimal)


class TestInvoices:
    def test_parses_invoice(self, invoices_payload):
        invoices = parse_invoices(invoices_payload)
        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.amount == Decimal("138.06")
        assert inv.gst_amount == Decimal("13.81")
        assert inv.due_date == date(2026, 8, 17)
        assert inv.period_start == date(2026, 6, 29)
        assert not inv.is_paid
        assert inv.pay_on_time_discount == Decimal("5.52236")


class TestServicePoint:
    def test_parses_meters_and_registers(self, service_point_payload):
        sp = parse_service_point(service_point_payload)
        assert sp.nmi
        assert sp.jurisdiction_code == "NSW"
        assert sp.is_generator is False
        assert len(sp.meters) == 2

    def test_only_current_registers_are_active(self, service_point_payload):
        """The account carries a REMOVED register beside a CURRENT one.

        Including the decommissioned one would blend a dead meter's history
        into the live series.
        """
        sp = parse_service_point(service_point_payload)
        all_registers = [r for m in sp.meters for r in m.registers]
        assert len(all_registers) > len(sp.active_registers)
        assert [r.register_id for r in sp.active_registers] == ["E1"]
        assert not sp.has_controlled_load

    def test_requires_an_nmi(self):
        with pytest.raises(ParseError, match="nationalMeteringId"):
            parse_service_point({"data": {"servicePointId": "1"}})


class TestUsage:
    def test_parses_days_oldest_first(self, usage_7d_payload):
        days = parse_usage(usage_7d_payload)
        assert len(days) == 8
        assert days[0].read_date < days[-1].read_date
        assert all(d.interval_minutes == 5 for d in days)
        assert all(len(d.intervals) == 288 for d in days)

    def test_energy_slots_reconcile_with_the_daily_aggregate(self, usage_30d_payload):
        """The whole design rests on this holding."""
        for day in parse_usage(usage_30d_payload):
            assert day.energy_kwh == pytest.approx(sum(day.intervals), abs=1e-6)

    def test_cost_slots_reconcile_with_the_daily_aggregate(self, usage_30d_payload):
        """Per-interval cost was assumed not to exist. It does, and it adds up."""
        for day in parse_usage(usage_30d_payload):
            assert day.cost_aud == pytest.approx(sum(day.costings), abs=1e-4)

    def test_time_of_use_bands_are_present_and_aligned(self, usage_7d_payload):
        for day in parse_usage(usage_7d_payload):
            assert len(day.tou) == len(day.intervals)
            assert set(day.tou) <= {"Peak", "Off Peak", "Shoulder"}

    def test_response_without_intervals_is_flagged_not_trusted(
        self, usage_no_intervals_payload
    ):
        """`interval-reads=NONE` still returns 288 zeroes and a length of 0.

        `has_intervals` is what stops a caller dividing by zero or importing a
        day of phantom consumption.
        """
        days = parse_usage(usage_no_intervals_payload)
        assert days
        assert all(not d.has_intervals for d in days)
        assert all(d.expected_slots is None for d in days)

    def test_mismatched_cost_array_is_rejected(self, usage_7d_payload):
        """Zipping arrays of different lengths would misattribute cost silently."""
        payload = usage_7d_payload
        payload["data"]["reads"][0]["intervalRead"]["intervalCostings"] = [0.1, 0.2]
        with pytest.raises(ParseError, match="cost slots"):
            parse_usage(payload)
