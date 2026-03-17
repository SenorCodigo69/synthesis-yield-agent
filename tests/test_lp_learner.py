"""Tests for LP learner — performance tracking + width adjustment feedback."""

import time
import pytest

from src.lp_learner import (
    record_decision,
    record_outcome,
    get_performance_by_regime,
    get_width_adjustments,
    get_summary,
)


class TestRecordDecision:
    def test_basic_record(self, tmp_path):
        db = tmp_path / "test.db"
        did = record_decision(
            action="mint", regime="sideways", regime_confidence=0.6,
            tick_lower=50000, tick_upper=60000, width_pct=0.10,
            entry_price=2500, atr_pct=0.03, rsi=50, adx=20,
            db_path=db,
        )
        assert did > 0

    def test_multiple_records(self, tmp_path):
        db = tmp_path / "test.db"
        d1 = record_decision(action="mint", regime="bull", regime_confidence=0.7, db_path=db)
        d2 = record_decision(action="rebalance", regime="sideways", regime_confidence=0.5, db_path=db)
        assert d2 > d1


class TestRecordOutcome:
    def test_basic_outcome(self, tmp_path):
        db = tmp_path / "test.db"
        did = record_decision(action="mint", regime="sideways", regime_confidence=0.6, db_path=db)
        record_outcome(
            decision_id=did, exit_price=2600,
            fees_usd=5.0, il_pct=-0.01, net_pnl_usd=3.5,
            hold_duration_hours=12, db_path=db,
        )
        # No exception = success


class TestPerformanceByRegime:
    def _seed_data(self, db, regime, win_count, loss_count, avg_il=-0.005):
        for i in range(win_count + loss_count):
            did = record_decision(
                action="mint", regime=regime, regime_confidence=0.6,
                width_pct=0.10, entry_price=2500, db_path=db,
            )
            is_win = i < win_count
            record_outcome(
                decision_id=did, exit_price=2550 if is_win else 2400,
                fees_usd=10 if is_win else 2, il_pct=avg_il,
                net_pnl_usd=5 if is_win else -3,
                hold_duration_hours=6, db_path=db,
            )

    def test_sideways_stats(self, tmp_path):
        db = tmp_path / "test.db"
        self._seed_data(db, "sideways", win_count=7, loss_count=3)
        stats = get_performance_by_regime(db)
        assert len(stats) == 1
        assert stats[0].regime == "sideways"
        assert stats[0].total_outcomes == 10
        assert stats[0].win_rate == 70.0

    def test_multiple_regimes(self, tmp_path):
        db = tmp_path / "test.db"
        self._seed_data(db, "sideways", 5, 5)
        self._seed_data(db, "bull", 8, 2)
        stats = get_performance_by_regime(db)
        assert len(stats) == 2
        regimes = {s.regime for s in stats}
        assert regimes == {"sideways", "bull"}

    def test_low_win_rate_recommends_widen(self, tmp_path):
        db = tmp_path / "test.db"
        self._seed_data(db, "bull", win_count=2, loss_count=8)
        stats = get_performance_by_regime(db)
        bull = [s for s in stats if s.regime == "bull"][0]
        assert bull.recommended_width_adjustment > 1.0  # Widen

    def test_high_win_rate_low_il_recommends_tighten(self, tmp_path):
        db = tmp_path / "test.db"
        self._seed_data(db, "sideways", win_count=9, loss_count=1, avg_il=-0.002)
        stats = get_performance_by_regime(db)
        sw = [s for s in stats if s.regime == "sideways"][0]
        assert sw.recommended_width_adjustment < 1.0  # Tighten


class TestWidthAdjustments:
    def test_adjustments_dict(self, tmp_path):
        db = tmp_path / "test.db"
        for i in range(5):
            did = record_decision(action="mint", regime="sideways", regime_confidence=0.6, db_path=db)
            record_outcome(decision_id=did, exit_price=2500, fees_usd=5, net_pnl_usd=3, db_path=db)
        adj = get_width_adjustments(db)
        assert "sideways" in adj
        assert adj["sideways"].sample_size == 5

    def test_empty_db_returns_empty(self, tmp_path):
        db = tmp_path / "test.db"
        adj = get_width_adjustments(db)
        assert adj == {}


class TestSummary:
    def test_summary(self, tmp_path):
        db = tmp_path / "test.db"
        did = record_decision(action="mint", regime="bull", regime_confidence=0.7, db_path=db)
        record_outcome(decision_id=did, exit_price=2600, fees_usd=10, net_pnl_usd=8, db_path=db)
        s = get_summary(db)
        assert s["total_decisions"] == 1
        assert s["total_outcomes"] == 1
        assert s["total_pnl_usd"] == 8
        assert s["overall_win_rate"] == 100

    def test_empty_summary(self, tmp_path):
        db = tmp_path / "test.db"
        s = get_summary(db)
        assert s["total_decisions"] == 0
        assert s["overall_win_rate"] == 0
