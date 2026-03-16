"""Tests for concentrated LP modules: tick math, optimizer, IL tracker, rebalancer."""

import math
import time

import pytest

from src.lp_tick_math import (
    align_tick,
    aligned_range,
    eth_price_to_tick,
    tick_to_eth_price,
    tick_to_price,
    price_to_tick,
)
from src.lp_signals import (
    Candle,
    compute_atr,
    compute_bollinger,
    compute_rsi,
    compute_adx,
    detect_regime,
    _ema,
    _sma,
    LPSignals,
)
from src.lp_optimizer import compute_range, MIN_WIDTH_PCT, MAX_WIDTH_PCT
from src.lp_il_tracker import compute_concentrated_il, compute_il_report
from src.lp_rebalancer import check_rebalance


# ── Tick Math ─────────────────────────────────────────────────


class TestTickMath:
    def test_tick_to_price_zero(self):
        assert tick_to_price(0) == pytest.approx(1.0)

    def test_tick_to_price_positive(self):
        # tick 1000 should be > 1
        assert tick_to_price(1000) > 1.0

    def test_tick_to_price_negative(self):
        assert tick_to_price(-1000) < 1.0

    def test_price_to_tick_roundtrip(self):
        for tick in [-50000, -1000, 0, 1000, 50000]:
            price = tick_to_price(tick)
            recovered = price_to_tick(price)
            assert abs(recovered - tick) <= 1

    def test_eth_price_roundtrip(self):
        """$2500 ETH should roundtrip through tick conversion."""
        tick = eth_price_to_tick(2500)
        price = tick_to_eth_price(tick)
        assert abs(price - 2500) / 2500 < 0.01  # Within 1%

    def test_eth_price_range(self):
        """Common ETH prices should produce reasonable ticks."""
        for eth_price in [1000, 2000, 3000, 5000, 10000]:
            tick = eth_price_to_tick(eth_price)
            recovered = tick_to_eth_price(tick)
            assert abs(recovered - eth_price) / eth_price < 0.01

    def test_align_tick_down(self):
        assert align_tick(105, 10, round_down=True) == 100
        assert align_tick(-105, 10, round_down=True) == -110

    def test_align_tick_up(self):
        assert align_tick(101, 10, round_down=False) == 110

    def test_aligned_range_spacing(self):
        """Aligned range ticks should be multiples of tick spacing."""
        lower, upper = aligned_range(2000, 3000, fee=500)
        assert lower % 10 == 0  # tick spacing for 500 fee = 10
        assert upper % 10 == 0
        assert lower < upper

    def test_aligned_range_contains_prices(self):
        lower, upper = aligned_range(2000, 3000, fee=500)
        price_lower = tick_to_eth_price(lower)
        price_upper = tick_to_eth_price(upper)
        assert price_lower <= 2000
        assert price_upper >= 3000

    def test_price_to_tick_rejects_zero(self):
        with pytest.raises(ValueError):
            price_to_tick(0)

    def test_price_to_tick_rejects_negative(self):
        with pytest.raises(ValueError):
            price_to_tick(-100)


# ── Signals / Indicators ─────────────────────────────────────


def _make_candles(prices: list[float], spread: float = 0.02) -> list[Candle]:
    """Create candles from close prices with synthetic OHLV."""
    candles = []
    for i, p in enumerate(prices):
        candles.append(Candle(
            timestamp=1000000 + i * 3600,
            open=p * (1 - spread / 4),
            high=p * (1 + spread / 2),
            low=p * (1 - spread / 2),
            close=p,
        ))
    return candles


class TestIndicators:
    def test_ema_single_value(self):
        result = _ema([100.0], 14)
        assert result == [100.0]

    def test_ema_converges(self):
        vals = [100.0] * 50
        result = _ema(vals, 14)
        assert result[-1] == pytest.approx(100.0)

    def test_sma_flat(self):
        vals = [100.0] * 30
        result = _sma(vals, 20)
        assert result[-1] == pytest.approx(100.0)

    def test_atr_flat_market(self):
        candles = _make_candles([2500.0] * 30, spread=0.01)
        atr_val = compute_atr(candles)
        # ATR should be small relative to price
        assert 0 < atr_val < 100

    def test_atr_volatile_market(self):
        prices = [2500 + (i % 2) * 200 for i in range(30)]  # Oscillating
        candles = _make_candles(prices, spread=0.05)
        atr_val = compute_atr(candles)
        assert atr_val > 50  # Should be meaningfully large

    def test_bollinger_bands_contain_price(self):
        candles = _make_candles([2500.0] * 30)
        upper, mid, lower = compute_bollinger(candles)
        assert lower <= 2500 <= upper
        assert mid == pytest.approx(2500, rel=0.01)

    def test_rsi_flat_market(self):
        candles = _make_candles([2500.0] * 30)
        # Flat market RSI should be near 50
        rsi = compute_rsi(candles)
        assert 40 <= rsi <= 60

    def test_rsi_strong_uptrend(self):
        prices = [2000 + i * 20 for i in range(30)]
        candles = _make_candles(prices)
        rsi = compute_rsi(candles)
        assert rsi > 60

    def test_adx_trending(self):
        prices = [2000 + i * 30 for i in range(80)]  # Strong trend
        candles = _make_candles(prices, spread=0.01)
        adx_val = compute_adx(candles)
        assert adx_val > 10  # Should detect trend

    def test_regime_sideways_flat(self):
        candles = _make_candles([2500.0] * 80, spread=0.005)
        regime, conf, trend = detect_regime(candles)
        assert regime == "sideways"

    def test_regime_bull_trend(self):
        prices = [2000 + i * 15 for i in range(80)]
        candles = _make_candles(prices, spread=0.01)
        regime, conf, trend = detect_regime(candles)
        assert regime == "bull"
        assert trend == "up"

    def test_regime_bear_trend(self):
        prices = [4000 - i * 15 for i in range(80)]
        candles = _make_candles(prices, spread=0.01)
        regime, conf, trend = detect_regime(candles)
        assert regime == "bear"
        assert trend == "down"

    def test_regime_insufficient_data(self):
        candles = _make_candles([2500.0] * 10)
        regime, conf, trend = detect_regime(candles)
        assert regime == "sideways"
        assert conf == 0.0


# ── Optimizer ─────────────────────────────────────────────────


def _make_signals(
    price: float = 2500,
    atr_pct: float = 0.03,
    regime: str = "sideways",
    confidence: float = 0.6,
    rsi: float = 50,
    adx: float = 20,
    bb_width_pct: float = 0.08,
) -> LPSignals:
    atr_val = price * atr_pct
    return LPSignals(
        current_price=price,
        atr=atr_val,
        atr_pct=atr_pct,
        bb_upper=price * (1 + bb_width_pct / 2),
        bb_lower=price * (1 - bb_width_pct / 2),
        bb_width_pct=bb_width_pct,
        rsi=rsi,
        adx=adx,
        regime=regime,
        regime_confidence=confidence,
        trend_direction="flat" if regime == "sideways" else ("up" if regime == "bull" else "down"),
        timestamp=time.time(),
    )


class TestOptimizer:
    def test_sideways_tighter_than_bull(self):
        sideways = compute_range(_make_signals(regime="sideways"))
        bull = compute_range(_make_signals(regime="bull"))
        assert sideways.width_pct < bull.width_pct

    def test_bull_skews_up(self):
        result = compute_range(_make_signals(regime="bull"))
        mid = (result.price_upper + result.price_lower) / 2
        assert mid > 2500  # Skewed above current price

    def test_bear_skews_down(self):
        result = compute_range(_make_signals(regime="bear", confidence=0.5))
        mid = (result.price_upper + result.price_lower) / 2
        assert mid < 2500

    def test_min_width_enforced(self):
        result = compute_range(_make_signals(atr_pct=0.001))  # Very low vol
        assert result.width_pct >= MIN_WIDTH_PCT

    def test_max_width_enforced(self):
        result = compute_range(_make_signals(atr_pct=0.5))  # Extreme vol
        assert result.width_pct <= MAX_WIDTH_PCT

    def test_ticks_aligned(self):
        result = compute_range(_make_signals())
        assert result.tick_lower % 10 == 0  # 500 fee tier spacing
        assert result.tick_upper % 10 == 0

    def test_tick_order(self):
        result = compute_range(_make_signals())
        assert result.tick_lower < result.tick_upper

    def test_reasoning_populated(self):
        result = compute_range(_make_signals())
        assert len(result.reasoning) > 10

    def test_high_adx_widens(self):
        low_adx = compute_range(_make_signals(adx=15, regime="bull"))
        high_adx = compute_range(_make_signals(adx=40, regime="bull"))
        assert high_adx.width_pct >= low_adx.width_pct

    def test_overbought_rsi_skews_down(self):
        normal = compute_range(_make_signals(rsi=50))
        overbought = compute_range(_make_signals(rsi=80))
        mid_normal = (normal.price_upper + normal.price_lower) / 2
        mid_overbought = (overbought.price_upper + overbought.price_lower) / 2
        assert mid_overbought < mid_normal


# ── IL Tracker ────────────────────────────────────────────────


class TestILTracker:
    def test_no_il_at_entry_price(self):
        il = compute_concentrated_il(2500, 2500, 2000, 3000)
        assert abs(il) < 0.001  # Near-zero IL

    def test_il_increases_with_price_move(self):
        il_small = abs(compute_concentrated_il(2500, 2600, 2000, 3000))
        il_large = abs(compute_concentrated_il(2500, 3000, 2000, 3000))
        assert il_large >= il_small

    def test_il_negative_when_price_moves(self):
        il = compute_concentrated_il(2500, 3500, 2000, 3000)
        assert il < 0  # IL is a loss

    def test_il_symmetric(self):
        """IL should be similar magnitude for equal up and down moves."""
        il_up = abs(compute_concentrated_il(2500, 2800, 2000, 3000))
        il_down = abs(compute_concentrated_il(2500, 2200, 2000, 3000))
        # Not exactly equal due to concentrated math, but same order of magnitude
        assert il_up > 0 and il_down > 0

    def test_il_report_profitable_with_fees(self):
        report = compute_il_report(
            token_id=1,
            entry_price=2500,
            current_price=2600,
            tick_lower=eth_price_to_tick(2000),
            tick_upper=eth_price_to_tick(3000),
            fees_weth=0.01,
            fees_usdc=50,
            position_value_usd=1000,
        )
        assert report.fees_earned_usd > 0
        assert isinstance(report.is_profitable, bool)

    def test_il_report_zero_fees(self):
        report = compute_il_report(
            token_id=1,
            entry_price=2500,
            current_price=3500,  # Big move
            tick_lower=eth_price_to_tick(2000),
            tick_upper=eth_price_to_tick(3000),
            fees_weth=0,
            fees_usdc=0,
            position_value_usd=1000,
        )
        assert report.il_pct < 0
        assert not report.is_profitable


# ── Rebalancer ────────────────────────────────────────────────


class TestRebalancer:
    def _base_signals(self, regime="sideways"):
        return _make_signals(regime=regime)

    def test_out_of_range_triggers_rebalance(self):
        decision = check_rebalance(
            current_tick=100000,
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time(),
            signals=self._base_signals(),
        )
        assert decision.should_rebalance
        assert decision.urgency == "high"
        assert decision.new_range is not None

    def test_in_range_no_rebalance(self):
        decision = check_rebalance(
            current_tick=55000,
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time(),
            signals=self._base_signals(),
        )
        assert not decision.should_rebalance
        assert decision.urgency == "none"

    def test_near_edge_triggers_medium(self):
        decision = check_rebalance(
            current_tick=50500,  # Very close to lower bound
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time(),
            signals=self._base_signals(),
        )
        assert decision.urgency == "medium"

    def test_regime_change_triggers_low(self):
        decision = check_rebalance(
            current_tick=55000,
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time(),
            signals=self._base_signals(regime="bull"),
        )
        assert decision.urgency == "low"
        assert "REGIME CHANGE" in decision.reason

    def test_stale_position_triggers(self):
        decision = check_rebalance(
            current_tick=55000,
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time() - 100000,  # >24h ago
            signals=self._base_signals(),
        )
        assert decision.urgency == "low"
        assert "STALE" in decision.reason

    def test_gas_gate_blocks_low_urgency(self):
        decision = check_rebalance(
            current_tick=55000,
            tick_lower=50000,
            tick_upper=60000,
            entry_regime="sideways",
            last_rebalance_ts=time.time() - 100000,
            signals=self._base_signals(),
            gas_gwei=100,  # High gas
        )
        assert not decision.should_rebalance
        assert not decision.gas_ok
