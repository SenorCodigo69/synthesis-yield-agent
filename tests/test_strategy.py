"""Tests for the strategy engine — risk scoring, net APY, allocation, rebalancing."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.models import (
    Chain,
    DataSource,
    GasPrice,
    ProtocolName,
    SpendingScope,
    ValidatedRate,
)
from src.strategy.risk_scorer import RiskScore, score_protocol_risk, PROTOCOL_METADATA
from src.strategy.net_apy import NetAPY, calculate_net_apy, estimate_gas_cost_usd
from src.strategy.allocator import AllocationPlan, compute_allocations, _apply_caps
from src.strategy.rebalancer import (
    RebalanceSignal,
    RebalanceTracker,
    TriggerType,
    check_rebalance_triggers,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_rate(
    protocol: ProtocolName = ProtocolName.AAVE_V3,
    apy: Decimal = Decimal("3.5"),
    tvl: Decimal = Decimal("200000000"),
    util: Decimal = Decimal("0.65"),
    is_valid: bool = True,
) -> ValidatedRate:
    return ValidatedRate(
        protocol=protocol,
        chain=Chain.BASE,
        apy_median=apy,
        apy_sources={DataSource.DEFILLAMA: apy, DataSource.ONCHAIN: apy},
        tvl_usd=tvl,
        utilization=util,
        is_valid=is_valid,
    )


def make_gas(gwei: Decimal = Decimal("0.01")) -> GasPrice:
    return GasPrice(
        base_fee_gwei=gwei,
        priority_fee_gwei=Decimal("0.001"),
        source="test",
    )


def default_scope() -> SpendingScope:
    return SpendingScope()


# ═══════════════════════════════════════════════════════════════════════════════
# Risk Scorer Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRiskScorer:
    def test_aave_risk_score_reasonable(self):
        """Aave V3 should score low risk — large TVL, old, well-audited."""
        rate = make_rate(ProtocolName.AAVE_V3, tvl=Decimal("500000000"), util=Decimal("0.60"))
        score = score_protocol_risk(rate)
        assert score.total < Decimal("0.15")
        assert score.protocol == ProtocolName.AAVE_V3

    def test_compound_risk_score_reasonable(self):
        """Compound V3 should score low risk — similar to Aave."""
        rate = make_rate(ProtocolName.COMPOUND_V3, tvl=Decimal("300000000"), util=Decimal("0.50"))
        score = score_protocol_risk(rate)
        assert score.total < Decimal("0.15")

    def test_morpho_slightly_higher_risk(self):
        """Morpho is newer — should score slightly higher than Aave/Compound."""
        rate = make_rate(ProtocolName.MORPHO, tvl=Decimal("100000000"), util=Decimal("0.40"))
        score = score_protocol_risk(rate)
        # Still low overall, but should reflect newer protocol
        assert score.total < Decimal("0.25")

    def test_high_utilization_increases_risk(self):
        """95% utilization should significantly increase risk score."""
        low_util = make_rate(util=Decimal("0.40"))
        high_util = make_rate(util=Decimal("0.95"))

        score_low = score_protocol_risk(low_util)
        score_high = score_protocol_risk(high_util)

        assert score_high.total > score_low.total
        assert score_high.utilization_score > score_low.utilization_score

    def test_low_tvl_increases_risk(self):
        """Low TVL should increase risk score."""
        high_tvl = make_rate(tvl=Decimal("500000000"))
        low_tvl = make_rate(tvl=Decimal("5000000"))

        assert score_protocol_risk(low_tvl).total > score_protocol_risk(high_tvl).total

    def test_risk_score_clamped_zero_to_one(self):
        """Score should always be between 0 and 1."""
        for proto in ProtocolName:
            rate = make_rate(proto)
            score = score_protocol_risk(rate)
            assert Decimal("0") <= score.total <= Decimal("1")

    def test_penalty_is_complement(self):
        """Penalty should be 1 - total."""
        rate = make_rate()
        score = score_protocol_risk(rate)
        assert score.penalty == Decimal("1") - score.total

    def test_risk_details_populated(self):
        """Should include human-readable detail strings."""
        rate = make_rate()
        score = score_protocol_risk(rate)
        assert len(score.details) >= 4  # TVL, age, audit, util, bad_debt

    def test_all_protocols_have_metadata(self):
        """Every ProtocolName should have static metadata."""
        for proto in ProtocolName:
            assert proto in PROTOCOL_METADATA, f"Missing metadata for {proto}"


# ═══════════════════════════════════════════════════════════════════════════════
# Net APY Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNetAPY:
    def test_net_apy_less_than_gross(self):
        """Net APY should always be less than or equal to gross APY."""
        rate = make_rate(apy=Decimal("4.0"))
        gas = make_gas(Decimal("1"))
        result = calculate_net_apy(rate, gas, Decimal("10000"))
        assert result.net_apy <= result.gross_apy

    def test_larger_deposit_lower_gas_impact(self):
        """Larger deposits should dilute gas cost more."""
        rate = make_rate(apy=Decimal("4.0"))
        gas = make_gas(Decimal("5"))

        small = calculate_net_apy(rate, gas, Decimal("1000"))
        large = calculate_net_apy(rate, gas, Decimal("100000"))

        assert large.net_apy > small.net_apy

    def test_longer_hold_lower_gas_impact(self):
        """Longer hold periods should reduce annualized gas cost."""
        rate = make_rate(apy=Decimal("4.0"))
        gas = make_gas(Decimal("5"))

        short = calculate_net_apy(rate, gas, Decimal("10000"), hold_days=30)
        long = calculate_net_apy(rate, gas, Decimal("10000"), hold_days=365)

        assert long.net_apy > short.net_apy

    def test_zero_gas_no_impact(self):
        """Zero gas should mean net APY equals gross APY."""
        rate = make_rate(apy=Decimal("4.0"))
        gas = make_gas(Decimal("0"))
        gas.priority_fee_gwei = Decimal("0")
        result = calculate_net_apy(rate, gas, Decimal("10000"))
        assert result.net_apy == result.gross_apy

    def test_base_chain_gas_very_cheap(self):
        """On Base (0.01 gwei), gas impact should be negligible for $10k+."""
        rate = make_rate(apy=Decimal("3.5"))
        gas = make_gas(Decimal("0.01"))
        result = calculate_net_apy(rate, gas, Decimal("10000"))
        # Gas impact should be tiny on Base
        assert result.gross_apy - result.net_apy < Decimal("0.01")

    def test_zero_amount_returns_zero_net(self):
        """Zero deposit amount should return 0 net APY."""
        rate = make_rate(apy=Decimal("4.0"))
        gas = make_gas()
        result = calculate_net_apy(rate, gas, Decimal("0"))
        assert result.net_apy == Decimal("0")

    def test_gas_cost_estimate_scales_with_price(self):
        """Higher gas prices should produce higher USD costs."""
        low = estimate_gas_cost_usd(make_gas(Decimal("1")))
        high = estimate_gas_cost_usd(make_gas(Decimal("100")))
        assert high > low

    def test_gas_cost_estimate_scales_with_eth_price(self):
        """Higher ETH price should produce higher gas costs."""
        gas = make_gas(Decimal("10"))
        cheap = estimate_gas_cost_usd(gas, Decimal("2000"))
        expensive = estimate_gas_cost_usd(gas, Decimal("5000"))
        assert expensive > cheap


# ═══════════════════════════════════════════════════════════════════════════════
# Allocator Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAllocator:
    def test_basic_allocation(self):
        """Should allocate to all eligible protocols."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"), tvl=Decimal("500000000")),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("3.0"), tvl=Decimal("300000000")),
            make_rate(ProtocolName.MORPHO, apy=Decimal("4.0"), tvl=Decimal("100000000")),
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        assert len(plan.allocations) > 0
        assert plan.total_allocated_usd > 0
        assert plan.total_allocated_usd <= Decimal("10000")

    def test_higher_yield_gets_more(self):
        """Protocol with higher risk-adjusted yield should get a larger share.

        Use 3 protocols so per-protocol cap (40%) doesn't flatten everything.
        """
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("2.0"), tvl=Decimal("500000000")),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("5.0"), tvl=Decimal("300000000")),
            make_rate(ProtocolName.MORPHO, apy=Decimal("3.0"), tvl=Decimal("100000000")),
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        allocs = {a.protocol: a for a in plan.allocations}
        # Compound should get more than Aave (higher APY, both safe)
        assert allocs[ProtocolName.COMPOUND_V3].amount_usd >= allocs[ProtocolName.AAVE_V3].amount_usd

    def test_invalid_rate_excluded(self):
        """Protocol with failed cross-validation should be excluded."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"), is_valid=True),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("5.0"), is_valid=False),
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        protos = {a.protocol for a in plan.allocations}
        assert ProtocolName.COMPOUND_V3 not in protos

    def test_low_tvl_excluded(self):
        """Protocol below TVL minimum should be excluded."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"), tvl=Decimal("200000000")),
            make_rate(ProtocolName.MORPHO, apy=Decimal("6.0"), tvl=Decimal("1000000")),
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        protos = {a.protocol for a in plan.allocations}
        assert ProtocolName.MORPHO not in protos

    def test_high_utilization_excluded(self):
        """Protocol above utilization cap should be excluded."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, util=Decimal("0.60")),
            make_rate(ProtocolName.COMPOUND_V3, util=Decimal("0.95")),
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        protos = {a.protocol for a in plan.allocations}
        assert ProtocolName.COMPOUND_V3 not in protos

    def test_insane_apy_excluded(self):
        """APY above sanity cap (50%) should be excluded."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5")),
            make_rate(ProtocolName.MORPHO, apy=Decimal("60.0")),  # Suspicious
        ]
        plan = compute_allocations(
            rates, make_gas(), Decimal("10000"), default_scope()
        )
        protos = {a.protocol for a in plan.allocations}
        assert ProtocolName.MORPHO not in protos

    def test_high_gas_blocks_all(self):
        """Gas above ceiling should block all allocations."""
        rates = [make_rate(ProtocolName.AAVE_V3)]
        plan = compute_allocations(
            rates, make_gas(Decimal("200")), Decimal("10000"), default_scope()
        )
        assert len(plan.allocations) == 0

    def test_per_protocol_cap_enforced(self):
        """No single protocol should exceed max_per_protocol_pct."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("10.0"), tvl=Decimal("500000000")),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("1.0"), tvl=Decimal("300000000")),
        ]
        scope = default_scope()
        plan = compute_allocations(
            rates, make_gas(), Decimal("100000"), scope
        )
        for a in plan.allocations:
            assert a.target_pct <= scope.max_per_protocol_pct + Decimal("0.01")  # rounding tolerance

    def test_reserve_buffer_maintained(self):
        """Should keep reserve buffer liquid."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("5.0"), tvl=Decimal("500000000")),
        ]
        capital = Decimal("10000")
        scope = default_scope()
        plan = compute_allocations(rates, make_gas(), capital, scope)
        assert plan.reserve_usd > 0
        assert plan.total_allocated_usd <= capital * scope.max_total_allocation_pct

    def test_no_rates_no_allocation(self):
        """Empty rates should produce empty allocation."""
        plan = compute_allocations([], make_gas(), Decimal("10000"), default_scope())
        assert len(plan.allocations) == 0
        assert plan.total_allocated_usd == 0

    def test_allocation_plan_properties(self):
        """AllocationPlan properties should compute correctly."""
        rates = [make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"), tvl=Decimal("200000000"))]
        plan = compute_allocations(rates, make_gas(), Decimal("10000"), default_scope())
        assert plan.allocated_pct >= 0
        assert plan.eligible_count >= 0

    def test_cap_redistribution(self):
        """Capping one protocol should redistribute excess to others."""
        weights = {
            ProtocolName.AAVE_V3: Decimal("0.60"),
            ProtocolName.COMPOUND_V3: Decimal("0.25"),
            ProtocolName.MORPHO: Decimal("0.15"),
        }
        capped = _apply_caps(weights, Decimal("0.40"))
        # Aave should be capped at 40%
        assert capped[ProtocolName.AAVE_V3] <= Decimal("0.40") + Decimal("0.001")
        # Others should get the excess
        assert capped[ProtocolName.COMPOUND_V3] > Decimal("0.25")
        # Sum should still be ~1.0
        total = sum(capped.values())
        assert abs(total - Decimal("1")) < Decimal("0.01")


# ═══════════════════════════════════════════════════════════════════════════════
# Rebalancer Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRebalancer:
    def test_no_triggers_when_healthy(self):
        """No signals when everything is within bounds."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, tvl=Decimal("200000000"), util=Decimal("0.50")),
        ]
        signals = check_rebalance_triggers(rates, None, make_gas(), default_scope())
        assert len(signals) == 0

    def test_tvl_drop_triggers_critical(self):
        """TVL below minimum should trigger critical signal."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, tvl=Decimal("1000000")),  # $1M, below $50M min
        ]
        signals = check_rebalance_triggers(rates, None, make_gas(), default_scope())
        tvl_signals = [s for s in signals if s.trigger == TriggerType.TVL_DROP]
        assert len(tvl_signals) == 1
        assert tvl_signals[0].severity == "critical"
        assert tvl_signals[0].should_act is True

    def test_high_utilization_triggers_warning(self):
        """Utilization above cap should trigger warning."""
        rates = [
            make_rate(ProtocolName.AAVE_V3, util=Decimal("0.95")),
        ]
        signals = check_rebalance_triggers(rates, None, make_gas(), default_scope())
        util_signals = [s for s in signals if s.trigger == TriggerType.HIGH_UTILIZATION]
        assert len(util_signals) == 1
        assert util_signals[0].should_act is True

    def test_high_gas_defers_moves(self):
        """Gas above ceiling should signal to defer."""
        rates = [make_rate(ProtocolName.AAVE_V3)]
        signals = check_rebalance_triggers(
            rates, None, make_gas(Decimal("150")), default_scope()
        )
        gas_signals = [s for s in signals if s.trigger == TriggerType.GAS_WINDOW]
        assert len(gas_signals) == 1
        assert gas_signals[0].should_act is False

    def test_tracker_records_rates(self):
        """RebalanceTracker should record and prune rate history."""
        tracker = RebalanceTracker()
        rates = [make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"))]
        tracker.record_rates(rates)
        assert len(tracker.rate_history) == 1

    def test_tracker_prunes_old_entries(self):
        """Entries older than 24h should be pruned."""
        tracker = RebalanceTracker()
        old_snapshot = tracker.rate_history
        # Manually add an old entry
        from src.strategy.rebalancer import RateSnapshot
        tracker.rate_history.append(RateSnapshot(
            protocol_name="aave-v3",
            apy=Decimal("3.0"),
            timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=25),
        ))
        rates = [make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5"))]
        tracker.record_rates(rates)
        # Old entry should be pruned
        assert all(
            s.timestamp > datetime.now(tz=timezone.utc) - timedelta(hours=24)
            for s in tracker.rate_history
        )

    def test_rate_diff_monitoring(self):
        """Should signal monitoring when diff is large but not yet sustained."""
        tracker = RebalanceTracker(rate_diff_threshold=Decimal("0.01"))
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("5.0")),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("2.0")),
        ]
        signal = tracker.check_sustained_rate_diff(rates)
        assert signal is not None
        assert signal.trigger == TriggerType.RATE_DIFF
        assert signal.should_act is False  # Not sustained yet

    def test_single_rate_no_diff_signal(self):
        """Single rate should not trigger rate diff signal."""
        tracker = RebalanceTracker()
        rates = [make_rate(ProtocolName.AAVE_V3)]
        signal = tracker.check_sustained_rate_diff(rates)
        assert signal is None

    def test_small_rate_diff_no_signal(self):
        """Small rate difference should not trigger signal."""
        tracker = RebalanceTracker(rate_diff_threshold=Decimal("0.01"))
        rates = [
            make_rate(ProtocolName.AAVE_V3, apy=Decimal("3.5")),
            make_rate(ProtocolName.COMPOUND_V3, apy=Decimal("3.4")),  # 0.1% diff
        ]
        signal = tracker.check_sustained_rate_diff(rates)
        assert signal is None
