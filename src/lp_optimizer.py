"""Concentrated LP tick range optimizer.

Uses quant signals (ATR, Bollinger Bands, RSI, ADX, regime detection) to
compute optimal Uniswap V3 tick ranges for WETH-USDC LP positions.

The optimizer adapts tick width and center based on:
- ATR: base width from recent volatility
- Regime: BULL skews up, BEAR skews down/exits, SIDEWAYS tightens
- ADX: strong trends widen the range to stay in range longer
- RSI: extremes skew the range toward expected mean reversion
- Bollinger Bands: sanity check on range width
"""

import logging
from dataclasses import dataclass

from . import lp_tick_math as tm
from .lp_signals import LPSignals

logger = logging.getLogger(__name__)

# ── Safety bounds ─────────────────────────────────────────────

MIN_WIDTH_PCT = 0.05   # Never tighter than ±2.5% from center
MAX_WIDTH_PCT = 0.50   # At 50%, just use full range
DEFAULT_WIDTH_PCT = 0.20  # Fallback when signals are stale


@dataclass
class OptimizedRange:
    """Output of the tick range optimizer."""

    tick_lower: int
    tick_upper: int
    price_lower: float  # USDC per ETH
    price_upper: float  # USDC per ETH
    width_pct: float    # range width as % of current price
    regime: str
    confidence: float
    reasoning: str


def compute_range(signals: LPSignals, fee: int = 500) -> OptimizedRange:
    """Compute optimal tick range from quant signals.

    Args:
        signals: Bundled quant signals from lp_signals.compute_signals().
        fee: Uniswap V3 fee tier (default 500 = 0.05%).

    Returns:
        OptimizedRange with tick bounds, prices, and reasoning.
    """
    price = signals.current_price
    if price <= 0:
        raise ValueError(f"Cannot compute range: current price is {price} (must be positive)")
    reasons = []

    # ── Step 1: Base width from ATR ──────────────────────────
    # 2x ATR captures ~95% of daily price movement
    base_width = signals.atr_pct * 2.0
    reasons.append(f"ATR={signals.atr:.0f} ({signals.atr_pct:.1%} of price) → base width {base_width:.1%}")

    # ── Step 2: Regime adjustment ────────────────────────────
    center_offset = 0.0

    if signals.regime == "sideways":
        # Tightest range — max capital efficiency
        width = base_width * 0.75
        reasons.append(f"SIDEWAYS regime (conf={signals.regime_confidence:.0%}) → tighten to {width:.1%}")
    elif signals.regime == "bull":
        # Wider range, skewed upward
        width = base_width * 1.5
        center_offset = 0.02  # 2% above current
        reasons.append(f"BULL regime (conf={signals.regime_confidence:.0%}) → widen to {width:.1%}, skew +2%")
    elif signals.regime == "bear":
        if signals.regime_confidence > 0.7:
            # Strong bear — recommend exit (return very wide "safe" range)
            reasons.append(f"BEAR regime (conf={signals.regime_confidence:.0%}) → recommend EXIT LP")
            width = MAX_WIDTH_PCT
            center_offset = -0.03
        else:
            # Mild bear — widen and skew down
            width = base_width * 1.5
            center_offset = -0.02
            reasons.append(f"BEAR regime (conf={signals.regime_confidence:.0%}) → widen to {width:.1%}, skew -2%")
    else:
        width = base_width
        reasons.append(f"Unknown regime → default width {width:.1%}")

    # ── Step 3: ADX gate — strong trends need wider ranges ───
    if signals.adx > 30 and signals.regime != "sideways":
        old_width = width
        width *= 1.5
        reasons.append(f"ADX={signals.adx:.0f} (strong trend) → widen {old_width:.1%} → {width:.1%}")

    # ── Step 4: RSI extremes — expect mean reversion ─────────
    if signals.rsi > 75:
        center_offset -= 0.01
        reasons.append(f"RSI={signals.rsi:.0f} (overbought) → skew down 1%")
    elif signals.rsi < 25:
        center_offset += 0.01
        reasons.append(f"RSI={signals.rsi:.0f} (oversold) → skew up 1%")

    # ── Step 5: BB sanity check ──────────────────────────────
    bb_width_pct = signals.bb_width_pct
    if width < bb_width_pct * 0.5:
        old_width = width
        width = bb_width_pct * 0.5
        reasons.append(f"Range too tight vs BB ({old_width:.1%} < {bb_width_pct:.1%}/2) → floor at {width:.1%}")
    elif width > bb_width_pct * 2:
        old_width = width
        width = bb_width_pct * 2
        reasons.append(f"Range too wide vs BB ({old_width:.1%} > {bb_width_pct:.1%}×2) → cap at {width:.1%}")

    # ── Step 6: Clamp to safety bounds ───────────────────────
    width = max(MIN_WIDTH_PCT, min(MAX_WIDTH_PCT, width))

    # ── Step 7: Compute price bounds ─────────────────────────
    center = price * (1 + center_offset)
    half_width = width / 2
    price_lower = center * (1 - half_width)
    price_upper = center * (1 + half_width)

    reasons.append(f"Final: ${price_lower:,.0f} – ${price_upper:,.0f} (width={width:.1%}, center offset={center_offset:+.1%})")

    # ── Step 8: Convert to aligned ticks ─────────────────────
    tick_lower, tick_upper = tm.aligned_range(price_lower, price_upper, fee)

    # Verify tick-derived prices (may differ slightly due to alignment)
    actual_lower = tm.tick_to_eth_price(tick_lower)
    actual_upper = tm.tick_to_eth_price(tick_upper)

    return OptimizedRange(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        price_lower=actual_lower,
        price_upper=actual_upper,
        width_pct=width,
        regime=signals.regime,
        confidence=signals.regime_confidence,
        reasoning=" | ".join(reasons),
    )
