"""Tests for circuit breakers and health monitor."""

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from src.circuit_breakers import (
    BreakerAction,
    BreakerTrip,
    BreakerType,
    CircuitBreakers,
)
from src.health_monitor import HealthMonitor, HealthStatus
from src.models import (
    Chain,
    DataSource,
    GasPrice,
    ProtocolName,
    SpendingScope,
    ValidatedRate,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def breakers():
    """Circuit breakers with default config."""
    return CircuitBreakers()


@pytest.fixture
def breakers_custom():
    """Circuit breakers with custom thresholds."""
    return CircuitBreakers({
        "circuit_breakers": {
            "depeg_threshold": 0.005,
            "tvl_drop_1h_pct": 0.30,
            "tvl_drop_24h_pct": 0.10,
            "gas_freeze_gwei": 200,
            "rate_divergence_block": 0.02,
        }
    })


@pytest.fixture
def normal_gas():
    return GasPrice(
        base_fee_gwei=Decimal("0.01"),
        priority_fee_gwei=Decimal("0.001"),
        source="test",
    )


@pytest.fixture
def high_gas():
    return GasPrice(
        base_fee_gwei=Decimal("180"),
        priority_fee_gwei=Decimal("25"),
        source="test",
    )


@pytest.fixture
def healthy_rates():
    return [
        ValidatedRate(
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            apy_median=Decimal("3.50"),
            apy_sources={
                DataSource.DEFILLAMA: Decimal("3.45"),
                DataSource.ONCHAIN: Decimal("3.55"),
            },
            tvl_usd=Decimal("200000000"),
            utilization=Decimal("0.65"),
            is_valid=True,
        ),
        ValidatedRate(
            protocol=ProtocolName.MORPHO,
            chain=Chain.BASE,
            apy_median=Decimal("4.20"),
            apy_sources={
                DataSource.DEFILLAMA: Decimal("4.15"),
                DataSource.ONCHAIN: Decimal("4.25"),
            },
            tvl_usd=Decimal("400000000"),
            utilization=Decimal("0.55"),
            is_valid=True,
        ),
    ]


@pytest.fixture
def scope():
    return SpendingScope()


# ── Depeg tests ──────────────────────────────────────────────────────────

class TestDepegBreaker:
    def test_normal_price_no_trip(self, breakers):
        trips = breakers.check_depeg(Decimal("1.0"))
        assert trips == []

    def test_minor_deviation_no_trip(self, breakers):
        trips = breakers.check_depeg(Decimal("0.998"))
        assert trips == []

    def test_above_threshold_trips(self, breakers):
        trips = breakers.check_depeg(Decimal("0.990"))
        assert len(trips) == 1
        assert trips[0].breaker == BreakerType.DEPEG
        assert trips[0].action == BreakerAction.EMERGENCY_WITHDRAW_ALL
        assert trips[0].severity == "critical"

    def test_overpeg_trips(self, breakers):
        trips = breakers.check_depeg(Decimal("1.010"))
        assert len(trips) == 1
        assert trips[0].breaker == BreakerType.DEPEG

    def test_exact_threshold_no_trip(self, breakers):
        """At exactly the threshold, should NOT trip (> not >=)."""
        trips = breakers.check_depeg(Decimal("0.995"))
        assert trips == []

    def test_slightly_past_threshold_trips(self, breakers):
        trips = breakers.check_depeg(Decimal("0.9949"))
        assert len(trips) == 1

    def test_custom_threshold(self):
        breakers = CircuitBreakers({
            "circuit_breakers": {"depeg_threshold": 0.01}
        })
        # 0.995 is within 0.01 tolerance
        trips = breakers.check_depeg(Decimal("0.995"))
        assert trips == []
        # 0.989 exceeds it
        trips = breakers.check_depeg(Decimal("0.989"))
        assert len(trips) == 1


# ── Gas freeze tests ──────────────────────────────────────────────────────

class TestGasFreeze:
    def test_normal_gas_no_trip(self, breakers, normal_gas):
        trips = breakers.check_gas_freeze(normal_gas)
        assert trips == []

    def test_high_gas_trips(self, breakers, high_gas):
        trips = breakers.check_gas_freeze(high_gas)
        assert len(trips) == 1
        assert trips[0].breaker == BreakerType.GAS_FREEZE
        assert trips[0].action == BreakerAction.FREEZE_ALL
        assert trips[0].severity == "critical"

    def test_exact_ceiling_no_trip(self, breakers):
        gas = GasPrice(
            base_fee_gwei=Decimal("190"),
            priority_fee_gwei=Decimal("10"),
            source="test",
        )
        trips = breakers.check_gas_freeze(gas)
        assert trips == []

    def test_just_above_ceiling_trips(self, breakers):
        gas = GasPrice(
            base_fee_gwei=Decimal("195"),
            priority_fee_gwei=Decimal("10"),
            source="test",
        )
        trips = breakers.check_gas_freeze(gas)
        assert len(trips) == 1


# ── Rate divergence tests ────────────────────────────────────────────────

class TestRateDivergence:
    def test_no_divergence_no_trip(self, breakers, healthy_rates):
        trips = breakers.check_rate_divergence(healthy_rates)
        assert trips == []

    def test_high_divergence_trips(self, breakers):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("3.50"),
                apy_sources={
                    DataSource.DEFILLAMA: Decimal("3.0"),
                    DataSource.ONCHAIN: Decimal("6.0"),
                },
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.65"),
                is_valid=True,
            ),
        ]
        trips = breakers.check_rate_divergence(rates)
        assert len(trips) == 1
        assert trips[0].breaker == BreakerType.RATE_DIVERGENCE
        assert trips[0].action == BreakerAction.PAUSE_PROTOCOL
        assert trips[0].protocol == "aave-v3"

    def test_single_source_no_trip(self, breakers):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("3.50"),
                apy_sources={DataSource.DEFILLAMA: Decimal("3.50")},
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.65"),
                is_valid=True,
            ),
        ]
        trips = breakers.check_rate_divergence(rates)
        assert trips == []


# ── TVL crash tests ──────────────────────────────────────────────────────

class TestTVLCrash:
    def test_first_observation_no_trip(self, breakers, healthy_rates):
        """First TVL observation can't detect a crash."""
        trips = breakers.check_tvl_crash(healthy_rates)
        assert trips == []

    def test_stable_tvl_no_trip(self, breakers):
        """Stable TVL should not trip."""
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("3.50"),
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.65"),
                is_valid=True,
            ),
        ]
        # Record twice with same TVL
        breakers.check_tvl_crash(rates)
        trips = breakers.check_tvl_crash(rates)
        assert trips == []


# ── check_all integration ─────────────────────────────────────────────────

class TestCheckAll:
    def test_all_clear(self, breakers, healthy_rates, normal_gas):
        trips = breakers.check_all(healthy_rates, normal_gas)
        assert trips == []

    def test_multiple_breakers(self, breakers, healthy_rates, high_gas):
        """Depeg + gas freeze = multiple trips."""
        trips = breakers.check_all(healthy_rates, high_gas, Decimal("0.990"))
        assert len(trips) >= 2
        types = {t.breaker for t in trips}
        assert BreakerType.DEPEG in types
        assert BreakerType.GAS_FREEZE in types


# ── Helper methods ───────────────────────────────────────────────────────

class TestBreakerHelpers:
    def test_has_critical_trips(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.DEPEG,
                action=BreakerAction.EMERGENCY_WITHDRAW_ALL,
                severity="critical",
                message="test",
            ),
        ]
        assert breakers.has_critical_trips(trips) is True

    def test_no_critical_trips(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.RATE_DIVERGENCE,
                action=BreakerAction.PAUSE_PROTOCOL,
                severity="warning",
                message="test",
                protocol="aave-v3",
            ),
        ]
        assert breakers.has_critical_trips(trips) is False

    def test_requires_emergency_withdraw(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.DEPEG,
                action=BreakerAction.EMERGENCY_WITHDRAW_ALL,
                severity="critical",
                message="test",
            ),
        ]
        assert breakers.requires_emergency_withdraw(trips) is True

    def test_no_emergency_withdraw(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.GAS_FREEZE,
                action=BreakerAction.FREEZE_ALL,
                severity="critical",
                message="test",
            ),
        ]
        assert breakers.requires_emergency_withdraw(trips) is False

    def test_frozen_protocols_all(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.GAS_FREEZE,
                action=BreakerAction.FREEZE_ALL,
                severity="critical",
                message="test",
            ),
        ]
        frozen = breakers.get_frozen_protocols(trips)
        assert "__all__" in frozen

    def test_frozen_protocols_specific(self, breakers):
        trips = [
            BreakerTrip(
                breaker=BreakerType.RATE_DIVERGENCE,
                action=BreakerAction.PAUSE_PROTOCOL,
                severity="warning",
                message="test",
                protocol="aave-v3",
            ),
        ]
        frozen = breakers.get_frozen_protocols(trips)
        assert "aave-v3" in frozen
        assert "__all__" not in frozen

    def test_empty_trips_no_frozen(self, breakers):
        frozen = breakers.get_frozen_protocols([])
        assert frozen == set()


# ── Health Monitor tests ──────────────────────────────────────────────────

class TestHealthMonitor:
    def test_healthy_system(self, breakers, scope, healthy_rates, normal_gas):
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(healthy_rates, normal_gas)

        assert health.is_operational is True
        assert health.gas_ok is True
        assert health.depeg_ok is True
        assert len(health.breaker_trips) == 0
        assert len(health.safe_protocols) > 0

    def test_degraded_with_high_gas(self, breakers, scope, healthy_rates, high_gas):
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(healthy_rates, high_gas)

        assert health.is_operational is False
        assert health.gas_ok is False

    def test_degraded_with_depeg(self, breakers, scope, healthy_rates, normal_gas):
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(
            healthy_rates, normal_gas, usdc_price=Decimal("0.980")
        )

        assert health.is_operational is False
        assert health.depeg_ok is False

    def test_protocol_health_all_checks(self, breakers, scope, healthy_rates, normal_gas):
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(healthy_rates, normal_gas)

        for name, proto_health in health.protocols.items():
            assert proto_health.checks_total == 6
            assert proto_health.status == HealthStatus.HEALTHY
            assert proto_health.is_safe_to_deposit is True
            assert proto_health.should_withdraw is False

    def test_low_tvl_warning(self, breakers, scope, normal_gas):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.COMPOUND_V3,
                chain=Chain.BASE,
                apy_median=Decimal("2.50"),
                apy_sources={DataSource.DEFILLAMA: Decimal("2.50")},
                tvl_usd=Decimal("2000000"),  # Below $50M minimum
                utilization=Decimal("0.30"),
                is_valid=True,
            ),
        ]
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates, normal_gas)

        proto = health.protocols["compound-v3"]
        assert proto.status != HealthStatus.HEALTHY
        assert proto.is_safe_to_deposit is False
        assert any("TVL" in issue for issue in proto.issues)

    def test_high_utilization_warning(self, breakers, scope, normal_gas):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("8.50"),
                apy_sources={DataSource.DEFILLAMA: Decimal("8.50")},
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.95"),  # Above 90% cap
                is_valid=True,
            ),
        ]
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates, normal_gas)

        proto = health.protocols["aave-v3"]
        assert any("Utilization" in issue for issue in proto.issues)

    def test_invalid_rate_blocks_protocol(self, breakers, scope, normal_gas):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.MORPHO,
                chain=Chain.BASE,
                apy_median=Decimal("4.20"),
                apy_sources={
                    DataSource.DEFILLAMA: Decimal("3.0"),
                    DataSource.ONCHAIN: Decimal("6.0"),
                },
                tvl_usd=Decimal("400000000"),
                utilization=Decimal("0.55"),
                is_valid=False,
            ),
        ]
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates, normal_gas)

        proto = health.protocols["morpho-v1"]
        assert proto.is_safe_to_deposit is False
        assert any("cross-validation" in issue.lower() for issue in proto.issues)

    def test_safe_protocols_list(self, breakers, scope, healthy_rates, normal_gas):
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(healthy_rates, normal_gas)

        assert "aave-v3" in health.safe_protocols
        assert "morpho-v1" in health.safe_protocols

    def test_critical_protocols_list(self, breakers, scope, normal_gas):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("3.50"),
                apy_sources={DataSource.DEFILLAMA: Decimal("3.50")},
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.65"),
                is_valid=True,
            ),
        ]
        # Trip a protocol-specific breaker by injecting divergent rates
        divergent_rates = [
            ValidatedRate(
                protocol=ProtocolName.MORPHO,
                chain=Chain.BASE,
                apy_median=Decimal("4.00"),
                apy_sources={
                    DataSource.DEFILLAMA: Decimal("1.0"),
                    DataSource.ONCHAIN: Decimal("8.0"),
                },
                tvl_usd=Decimal("400000000"),
                utilization=Decimal("0.55"),
                is_valid=False,
            ),
        ]
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates + divergent_rates, normal_gas)

        # Morpho should be critical (invalid rate + divergence breaker)
        assert "morpho-v1" in health.critical_protocols

    def test_insane_apy_blocks(self, breakers, scope, normal_gas):
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                apy_median=Decimal("60.0"),  # 60% APY = insane
                apy_sources={DataSource.DEFILLAMA: Decimal("60.0")},
                tvl_usd=Decimal("200000000"),
                utilization=Decimal("0.65"),
                is_valid=True,
            ),
        ]
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates, normal_gas)

        proto = health.protocols["aave-v3"]
        assert proto.is_safe_to_deposit is False
        assert any("sanity" in issue.lower() for issue in proto.issues)


# ── Config loading tests ─────────────────────────────────────────────────

class TestBreakerConfig:
    def test_default_config(self):
        breakers = CircuitBreakers()
        assert breakers.depeg_threshold == Decimal("0.005")
        assert breakers.gas_freeze_gwei == 200
        assert breakers.rate_divergence_block == Decimal("0.02")

    def test_custom_config(self):
        breakers = CircuitBreakers({
            "circuit_breakers": {
                "depeg_threshold": 0.01,
                "gas_freeze_gwei": 300,
                "rate_divergence_block": 0.05,
            }
        })
        assert breakers.depeg_threshold == Decimal("0.01")
        assert breakers.gas_freeze_gwei == 300
        assert breakers.rate_divergence_block == Decimal("0.05")

    def test_empty_config_uses_defaults(self):
        breakers = CircuitBreakers({})
        assert breakers.depeg_threshold == Decimal("0.005")

    def test_none_config_uses_defaults(self):
        breakers = CircuitBreakers(None)
        assert breakers.depeg_threshold == Decimal("0.005")
