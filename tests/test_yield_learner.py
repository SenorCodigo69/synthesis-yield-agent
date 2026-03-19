"""Tests for the yield allocation learning loop."""

import pytest
from pathlib import Path

from src.yield_learner import (
    record_allocation,
    record_yield_outcome,
    get_protocol_performance,
    get_risk_adjustments,
    get_summary,
    _get_db,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_learner.db"


class TestRecordAllocation:

    def test_records_and_returns_id(self, db_path):
        decision_id = record_allocation(
            protocol="aave-v3",
            action="supply",
            predicted_apy=2.5,
            risk_score=0.16,
            risk_adjusted_apy=2.1,
            amount_usd=5000,
            tvl_usd=200_000_000,
            utilization=0.65,
            reasoning="Best risk-adjusted yield",
            db_path=db_path,
        )
        assert decision_id >= 1

    def test_sequential_ids(self, db_path):
        id1 = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
        id2 = record_allocation("morpho-v1", "supply", 3.6, 0.07, 3.35, 3000, db_path=db_path)
        assert id2 > id1


class TestRecordOutcome:

    def test_records_outcome(self, db_path):
        decision_id = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
        record_yield_outcome(
            decision_id=decision_id,
            actual_apy=2.3,
            yield_earned_usd=0.05,
            gas_spent_usd=0.007,
            hold_hours=24,
            exit_reason="rebalance",
            db_path=db_path,
        )
        # Verify it was stored
        conn = _get_db(db_path)
        row = conn.execute("SELECT * FROM yield_outcomes WHERE decision_id = ?", (decision_id,)).fetchone()
        conn.close()
        assert row is not None

    def test_computes_apy_error(self, db_path):
        decision_id = record_allocation("aave-v3", "supply", 3.0, 0.16, 2.5, 5000, db_path=db_path)
        record_yield_outcome(decision_id, actual_apy=2.5, yield_earned_usd=0.04,
                             gas_spent_usd=0.007, hold_hours=24, db_path=db_path)
        conn = _get_db(db_path)
        row = conn.execute("SELECT apy_error FROM yield_outcomes WHERE decision_id = ?",
                           (decision_id,)).fetchone()
        conn.close()
        assert abs(row[0] - (-0.5)) < 0.01  # 2.5 - 3.0 = -0.5

    def test_tracks_profitability(self, db_path):
        did = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
        # Profitable
        record_yield_outcome(did, actual_apy=2.3, yield_earned_usd=0.05,
                             gas_spent_usd=0.007, hold_hours=24, db_path=db_path)
        conn = _get_db(db_path)
        row = conn.execute("SELECT was_profitable FROM yield_outcomes WHERE decision_id = ?",
                           (did,)).fetchone()
        conn.close()
        assert row[0] == 1  # 0.05 - 0.007 > 0

    def test_unprofitable_outcome(self, db_path):
        did = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 100, db_path=db_path)
        record_yield_outcome(did, actual_apy=0.1, yield_earned_usd=0.001,
                             gas_spent_usd=0.007, hold_hours=1, db_path=db_path)
        conn = _get_db(db_path)
        row = conn.execute("SELECT was_profitable FROM yield_outcomes WHERE decision_id = ?",
                           (did,)).fetchone()
        conn.close()
        assert row[0] == 0

    def test_missing_decision_skipped(self, db_path):
        # Should not raise
        record_yield_outcome(
            decision_id=9999, actual_apy=2.0, yield_earned_usd=0.01,
            gas_spent_usd=0.005, hold_hours=24, db_path=db_path,
        )


class TestProtocolPerformance:

    def _seed_data(self, db_path, protocol="aave-v3", n=5, predicted=2.5, actual=2.3):
        for i in range(n):
            did = record_allocation(protocol, "supply", predicted, 0.16, predicted * 0.84,
                                    5000, db_path=db_path)
            profit = 0.05 if actual >= predicted * 0.8 else -0.01
            record_yield_outcome(did, actual_apy=actual,
                                 yield_earned_usd=max(0, profit),
                                 gas_spent_usd=0.007,
                                 hold_hours=24, db_path=db_path)

    def test_returns_stats(self, db_path):
        self._seed_data(db_path)
        stats = get_protocol_performance(db_path)
        assert len(stats) == 1
        assert stats[0].protocol == "aave-v3"
        assert stats[0].total_outcomes == 5

    def test_win_rate_calculation(self, db_path):
        self._seed_data(db_path, predicted=2.5, actual=2.3, n=5)
        stats = get_protocol_performance(db_path)
        assert stats[0].win_rate == 100.0  # All profitable

    def test_overestimate_detection(self, db_path):
        # Predicted 5%, actual 2% — big overestimate
        self._seed_data(db_path, predicted=5.0, actual=2.0, n=5)
        stats = get_protocol_performance(db_path)
        assert stats[0].avg_apy_error < 0  # Negative = overestimated
        assert stats[0].risk_weight_adjustment > 1.0  # Should penalize

    def test_underestimate_reward(self, db_path):
        # Predicted 2%, actual 3% — consistently better than expected
        self._seed_data(db_path, predicted=2.0, actual=3.0, n=5)
        stats = get_protocol_performance(db_path)
        assert stats[0].avg_apy_error > 0  # Positive = underestimated
        assert stats[0].risk_weight_adjustment <= 1.0  # Should reward

    def test_multiple_protocols(self, db_path):
        self._seed_data(db_path, protocol="aave-v3", n=4)
        self._seed_data(db_path, protocol="morpho-v1", n=3)
        stats = get_protocol_performance(db_path)
        assert len(stats) == 2
        protocols = {s.protocol for s in stats}
        assert "aave-v3" in protocols
        assert "morpho-v1" in protocols


class TestRiskAdjustments:

    def test_returns_multipliers(self, db_path):
        for i in range(5):
            did = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
            record_yield_outcome(did, actual_apy=2.3, yield_earned_usd=0.05,
                                 gas_spent_usd=0.007, hold_hours=24, db_path=db_path)
        adjustments = get_risk_adjustments(db_path)
        assert "aave-v3" in adjustments
        assert isinstance(adjustments["aave-v3"], float)

    def test_empty_db_returns_empty(self, db_path):
        adjustments = get_risk_adjustments(db_path)
        assert adjustments == {}


class TestSummary:

    def test_empty_summary(self, db_path):
        summary = get_summary(db_path)
        assert summary.total_decisions == 0
        assert summary.total_outcomes == 0
        assert summary.overall_win_rate == 0
        assert summary.improvement_score == 50.0  # Baseline

    def test_full_summary(self, db_path):
        for i in range(5):
            did = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
            record_yield_outcome(did, actual_apy=2.3, yield_earned_usd=0.05,
                                 gas_spent_usd=0.007, hold_hours=24, db_path=db_path)
        summary = get_summary(db_path)
        assert summary.total_decisions == 5
        assert summary.total_outcomes == 5
        assert summary.overall_win_rate == 100.0
        assert summary.total_yield_usd > 0
        assert summary.total_net_profit_usd > 0
        assert len(summary.protocols) == 1

    def test_improvement_score_baseline(self, db_path):
        # With < 6 outcomes, should return baseline 50
        for i in range(3):
            did = record_allocation("aave-v3", "supply", 2.5, 0.16, 2.1, 5000, db_path=db_path)
            record_yield_outcome(did, actual_apy=2.3, yield_earned_usd=0.05,
                                 gas_spent_usd=0.007, hold_hours=24, db_path=db_path)
        summary = get_summary(db_path)
        assert summary.improvement_score == 50.0
