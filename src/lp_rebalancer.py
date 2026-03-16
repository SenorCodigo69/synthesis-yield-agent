"""LP rebalance engine — detects when to rebalance and recommends action.

Monitors the current LP position against quant signals and triggers
rebalance when:
1. Position is out of range (earning zero fees) — URGENT
2. Price is near range boundary — PRE-EMPTIVE
3. Regime changed since last rebalance — ADAPTIVE
4. Time-based stale check — MAINTENANCE
"""

import logging
import time
from dataclasses import dataclass

from .lp_optimizer import OptimizedRange, compute_range
from .lp_signals import LPSignals

logger = logging.getLogger(__name__)

# Max gas price (gwei) for non-urgent rebalances on Base
GAS_GATE_GWEI = 5


@dataclass
class RebalanceDecision:
    should_rebalance: bool
    reason: str
    urgency: str  # "none", "low", "medium", "high"
    new_range: OptimizedRange | None
    gas_ok: bool


def check_rebalance(
    current_tick: int,
    tick_lower: int,
    tick_upper: int,
    entry_regime: str | None,
    last_rebalance_ts: float | None,
    signals: LPSignals,
    gas_gwei: float = 0.001,  # Base gas is nearly free
) -> RebalanceDecision:
    """Check if an LP position should be rebalanced.

    Args:
        current_tick: Current pool tick from slot0().
        tick_lower: Position's lower tick bound.
        tick_upper: Position's upper tick bound.
        entry_regime: Regime when position was minted/last rebalanced.
        last_rebalance_ts: Unix timestamp of last rebalance (None if never).
        signals: Current quant signals.
        gas_gwei: Current gas price in gwei.

    Returns:
        RebalanceDecision with recommendation.
    """
    if tick_upper <= tick_lower:
        raise ValueError(f"Invalid position: tick_upper ({tick_upper}) must be > tick_lower ({tick_lower})")
    gas_ok = gas_gwei <= GAS_GATE_GWEI
    tick_range = tick_upper - tick_lower

    # 1. Out of range — position earns zero fees
    if current_tick < tick_lower or current_tick > tick_upper:
        new_range = compute_range(signals)
        return RebalanceDecision(
            should_rebalance=True,
            reason=f"OUT OF RANGE: tick {current_tick} outside [{tick_lower}, {tick_upper}]",
            urgency="high",
            new_range=new_range,
            gas_ok=True,  # Always rebalance if out of range
        )

    # 2. Edge proximity — within 10% of boundary
    edge_buffer = max(1, int(tick_range * 0.1))
    if current_tick < tick_lower + edge_buffer:
        new_range = compute_range(signals)
        return RebalanceDecision(
            should_rebalance=gas_ok,
            reason=f"NEAR LOWER EDGE: tick {current_tick} within {edge_buffer} of lower bound {tick_lower}",
            urgency="medium",
            new_range=new_range,
            gas_ok=gas_ok,
        )
    if current_tick > tick_upper - edge_buffer:
        new_range = compute_range(signals)
        return RebalanceDecision(
            should_rebalance=gas_ok,
            reason=f"NEAR UPPER EDGE: tick {current_tick} within {edge_buffer} of upper bound {tick_upper}",
            urgency="medium",
            new_range=new_range,
            gas_ok=gas_ok,
        )

    # 3. Regime change
    if entry_regime and signals.regime != entry_regime:
        new_range = compute_range(signals)
        return RebalanceDecision(
            should_rebalance=gas_ok,
            reason=f"REGIME CHANGE: {entry_regime} → {signals.regime} (conf={signals.regime_confidence:.0%})",
            urgency="low",
            new_range=new_range,
            gas_ok=gas_ok,
        )

    # 4. Time-based staleness (>24h without rebalance)
    if last_rebalance_ts and (time.time() - last_rebalance_ts) > 86400:
        new_range = compute_range(signals)
        hours_ago = (time.time() - last_rebalance_ts) / 3600
        return RebalanceDecision(
            should_rebalance=gas_ok,
            reason=f"STALE: last rebalance {hours_ago:.0f}h ago, signals may have shifted",
            urgency="low",
            new_range=new_range,
            gas_ok=gas_ok,
        )

    # No rebalance needed
    return RebalanceDecision(
        should_rebalance=False,
        reason="Position in range, no regime change, not stale",
        urgency="none",
        new_range=None,
        gas_ok=gas_ok,
    )
