"""Rebalancing engine — decides when to move capital between protocols.

Rebalance triggers:
- Rate difference > 1% sustained for 6h
- TVL drops > 10% in 24h
- Utilization crosses 90%
- Gas drops below threshold (execute pending moves)
- Governance changes (pause and reassess)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum

from src.models import Allocation, GasPrice, SpendingScope, ValidatedRate
from src.strategy.allocator import AllocationPlan

logger = logging.getLogger(__name__)


class TriggerType(str, Enum):
    RATE_DIFF = "rate_diff"
    TVL_DROP = "tvl_drop"
    HIGH_UTILIZATION = "high_utilization"
    GAS_WINDOW = "gas_window"
    NEGATIVE_YIELD = "negative_yield"


@dataclass
class RebalanceSignal:
    """A signal that rebalancing should occur."""
    trigger: TriggerType
    severity: str  # "info", "warning", "critical"
    message: str
    should_act: bool  # True = execute now, False = monitor
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class RateSnapshot:
    """Historical rate snapshot for tracking sustained differences."""
    protocol_name: str
    apy: Decimal
    timestamp: datetime


class RebalanceTracker:
    """Tracks rate history and determines when rebalancing triggers fire."""

    def __init__(
        self,
        rate_diff_threshold: Decimal = Decimal("0.01"),
        rate_diff_sustain_hours: int = 6,
        min_move_amount_usd: Decimal = Decimal("100"),
    ):
        self.rate_diff_threshold = rate_diff_threshold
        self.rate_diff_sustain_hours = rate_diff_sustain_hours
        self.min_move_amount_usd = min_move_amount_usd
        self.rate_history: list[RateSnapshot] = []

    def record_rates(self, rates: list[ValidatedRate]) -> None:
        """Record current rates for sustained-difference tracking."""
        now = datetime.now(tz=timezone.utc)
        for r in rates:
            self.rate_history.append(RateSnapshot(
                protocol_name=r.protocol.value,
                apy=r.apy_median,
                timestamp=now,
            ))

        # Prune old entries (keep 24h)
        cutoff = now - timedelta(hours=24)
        self.rate_history = [s for s in self.rate_history if s.timestamp >= cutoff]

    def check_sustained_rate_diff(self, rates: list[ValidatedRate]) -> RebalanceSignal | None:
        """Check if rate differences have been sustained long enough to act."""
        if len(rates) < 2:
            return None

        sorted_rates = sorted(rates, key=lambda r: r.apy_median, reverse=True)
        best = sorted_rates[0]
        worst = sorted_rates[-1]

        diff_pct = (best.apy_median - worst.apy_median) / Decimal("100")

        if diff_pct < self.rate_diff_threshold:
            return None

        # Check if this difference has been sustained
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=self.rate_diff_sustain_hours)
        history_for_best = [
            s for s in self.rate_history
            if s.protocol_name == best.protocol.value and s.timestamp >= cutoff
        ]
        history_for_worst = [
            s for s in self.rate_history
            if s.protocol_name == worst.protocol.value and s.timestamp >= cutoff
        ]

        if not history_for_best or not history_for_worst:
            return RebalanceSignal(
                trigger=TriggerType.RATE_DIFF,
                severity="info",
                message=(
                    f"Rate diff {diff_pct:.2%} ({best.protocol.value} {best.apy_median:.2f}% "
                    f"vs {worst.protocol.value} {worst.apy_median:.2f}%) — "
                    f"monitoring, need {self.rate_diff_sustain_hours}h sustained"
                ),
                should_act=False,
            )

        # Check if best has consistently been higher
        all_higher = all(
            b.apy > w.apy
            for b, w in zip(
                sorted(history_for_best, key=lambda s: s.timestamp),
                sorted(history_for_worst, key=lambda s: s.timestamp),
            )
        )

        if all_higher and len(history_for_best) >= 2:
            return RebalanceSignal(
                trigger=TriggerType.RATE_DIFF,
                severity="warning",
                message=(
                    f"Sustained rate diff {diff_pct:.2%} for {self.rate_diff_sustain_hours}h: "
                    f"move from {worst.protocol.value} to {best.protocol.value}"
                ),
                should_act=True,
            )

        return None


def check_rebalance_triggers(
    current_rates: list[ValidatedRate],
    current_plan: AllocationPlan | None,
    gas_price: GasPrice,
    scope: SpendingScope,
    tracker: RebalanceTracker | None = None,
) -> list[RebalanceSignal]:
    """Check all rebalancing triggers and return signals.

    Returns a list of signals — the caller decides whether to execute.
    """
    signals: list[RebalanceSignal] = []

    # ── TVL drop check ───────────────────────────────────────────────────
    for rate in current_rates:
        if rate.tvl_usd < scope.min_protocol_tvl_usd:
            signals.append(RebalanceSignal(
                trigger=TriggerType.TVL_DROP,
                severity="critical",
                message=(
                    f"{rate.protocol.value} TVL ${rate.tvl_usd:,.0f} below "
                    f"minimum ${scope.min_protocol_tvl_usd:,.0f} — withdraw"
                ),
                should_act=True,
            ))

    # ── High utilization check ───────────────────────────────────────────
    for rate in current_rates:
        if rate.utilization > scope.max_utilization:
            signals.append(RebalanceSignal(
                trigger=TriggerType.HIGH_UTILIZATION,
                severity="warning",
                message=(
                    f"{rate.protocol.value} utilization {rate.utilization:.1%} "
                    f"above {scope.max_utilization:.1%} cap — reduce position"
                ),
                should_act=True,
            ))

    # ── Gas window check ─────────────────────────────────────────────────
    if gas_price.total_gwei > scope.gas_ceiling_gwei:
        signals.append(RebalanceSignal(
            trigger=TriggerType.GAS_WINDOW,
            severity="info",
            message=(
                f"Gas {gas_price.total_gwei:.0f} gwei above "
                f"{scope.gas_ceiling_gwei} ceiling — defer non-urgent moves"
            ),
            should_act=False,
        ))

    # ── Negative net yield check ─────────────────────────────────────────
    if current_plan:
        for sp in current_plan.scored_protocols:
            if sp.eligible and sp.net_apy.net_apy < 0:
                signals.append(RebalanceSignal(
                    trigger=TriggerType.NEGATIVE_YIELD,
                    severity="critical",
                    message=(
                        f"{sp.rate.protocol.value} net APY {sp.net_apy.net_apy:.2f}% "
                        f"is negative — withdraw immediately"
                    ),
                    should_act=True,
                ))

    # ── Sustained rate difference check ──────────────────────────────────
    if tracker:
        rate_signal = tracker.check_sustained_rate_diff(current_rates)
        if rate_signal:
            signals.append(rate_signal)

    if signals:
        critical = sum(1 for s in signals if s.severity == "critical")
        actionable = sum(1 for s in signals if s.should_act)
        logger.info(
            f"Rebalance check: {len(signals)} signals "
            f"({critical} critical, {actionable} actionable)"
        )
    else:
        logger.info("Rebalance check: no triggers fired")

    return signals
