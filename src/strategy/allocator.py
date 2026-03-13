"""Allocation engine — distributes capital across protocols.

Uses risk-adjusted yield (net APY * risk penalty) to determine
proportional allocation, while respecting spending scope constraints.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from src.models import Allocation, GasPrice, ProtocolName, SpendingScope, ValidatedRate
from src.strategy.risk_scorer import RiskScore, score_protocol_risk
from src.strategy.net_apy import NetAPY, calculate_net_apy

logger = logging.getLogger(__name__)


@dataclass
class ScoredProtocol:
    """Protocol with all computed metrics for allocation decisions."""
    rate: ValidatedRate
    risk: RiskScore
    net_apy: NetAPY
    risk_adjusted_yield: Decimal  # net_apy * (1 - risk_score)
    eligible: bool = True
    rejection_reasons: list[str] = field(default_factory=list)


@dataclass
class AllocationPlan:
    """Complete allocation plan with reasoning."""
    allocations: list[Allocation]
    scored_protocols: list[ScoredProtocol]
    total_allocated_usd: Decimal
    total_capital_usd: Decimal
    reserve_usd: Decimal
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def allocated_pct(self) -> Decimal:
        if self.total_capital_usd == 0:
            return Decimal("0")
        return self.total_allocated_usd / self.total_capital_usd

    @property
    def eligible_count(self) -> int:
        return sum(1 for sp in self.scored_protocols if sp.eligible)


def compute_allocations(
    rates: list[ValidatedRate],
    gas_price: GasPrice,
    total_capital_usd: Decimal,
    scope: SpendingScope,
    hold_days: int = 90,
    eth_price_usd: Decimal = Decimal("3500"),
) -> AllocationPlan:
    """Compute optimal allocation across protocols.

    Steps:
    1. Score each protocol's risk
    2. Calculate net APY (after gas)
    3. Filter by spending scope constraints
    4. Compute risk-adjusted yield
    5. Allocate proportionally to risk-adjusted yield
    6. Apply per-protocol caps
    """
    # Reserve buffer — always keep some liquid
    allocatable = total_capital_usd * scope.max_total_allocation_pct * (
        Decimal("1") - scope.reserve_buffer_pct
    )
    reserve = total_capital_usd - allocatable

    scored: list[ScoredProtocol] = []

    for rate in rates:
        risk = score_protocol_risk(rate)
        net = calculate_net_apy(rate, gas_price, allocatable, hold_days, eth_price_usd)
        ray = net.net_apy * risk.penalty  # risk-adjusted yield

        sp = ScoredProtocol(
            rate=rate,
            risk=risk,
            net_apy=net,
            risk_adjusted_yield=ray,
        )

        # ── Eligibility checks ───────────────────────────────────────────
        if not rate.is_valid:
            sp.eligible = False
            sp.rejection_reasons.append("Rate cross-validation failed (blocked)")

        if rate.tvl_usd < scope.min_protocol_tvl_usd:
            sp.eligible = False
            sp.rejection_reasons.append(
                f"TVL ${rate.tvl_usd:,.0f} below minimum ${scope.min_protocol_tvl_usd:,.0f}"
            )

        if rate.utilization > scope.max_utilization:
            sp.eligible = False
            sp.rejection_reasons.append(
                f"Utilization {rate.utilization:.1%} above {scope.max_utilization:.1%} cap"
            )

        if rate.apy_median / Decimal("100") > scope.max_apy_sanity:
            sp.eligible = False
            sp.rejection_reasons.append(
                f"APY {rate.apy_median:.1f}% exceeds sanity cap {scope.max_apy_sanity * 100:.0f}%"
            )

        if gas_price.total_gwei > scope.gas_ceiling_gwei:
            sp.eligible = False
            sp.rejection_reasons.append(
                f"Gas {gas_price.total_gwei:.0f} gwei above {scope.gas_ceiling_gwei} ceiling"
            )

        if net.net_apy <= 0:
            sp.eligible = False
            sp.rejection_reasons.append(
                f"Net APY {net.net_apy:.2f}% <= 0 after gas costs"
            )

        scored.append(sp)

    # ── Proportional allocation ──────────────────────────────────────────
    eligible = [sp for sp in scored if sp.eligible]

    if not eligible:
        logger.warning("No eligible protocols — holding all capital in reserve")
        return AllocationPlan(
            allocations=[],
            scored_protocols=scored,
            total_allocated_usd=Decimal("0"),
            total_capital_usd=total_capital_usd,
            reserve_usd=total_capital_usd,
        )

    # Proportional weights based on risk-adjusted yield
    total_ray = sum(sp.risk_adjusted_yield for sp in eligible)
    if total_ray <= 0:
        logger.warning("All risk-adjusted yields <= 0 — no allocation")
        return AllocationPlan(
            allocations=[],
            scored_protocols=scored,
            total_allocated_usd=Decimal("0"),
            total_capital_usd=total_capital_usd,
            reserve_usd=total_capital_usd,
        )

    # Calculate raw weights
    weights: dict[ProtocolName, Decimal] = {}
    for sp in eligible:
        weights[sp.rate.protocol] = sp.risk_adjusted_yield / total_ray

    # Apply per-protocol cap — redistribute excess to others
    max_pct = scope.max_per_protocol_pct
    capped = _apply_caps(weights, max_pct)

    # Build allocation objects
    allocations = []
    total_alloc = Decimal("0")

    for sp in eligible:
        pct = capped.get(sp.rate.protocol, Decimal("0"))
        amount = (allocatable * pct).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        if amount <= 0:
            continue

        allocations.append(Allocation(
            protocol=sp.rate.protocol,
            chain=sp.rate.chain,
            amount_usd=amount,
            target_pct=pct,
            actual_pct=Decimal("0"),  # Set after execution
        ))
        total_alloc += amount

    logger.info(
        f"Allocation plan: ${total_alloc:,.2f} across {len(allocations)} protocols "
        f"({total_alloc / total_capital_usd:.1%} of ${total_capital_usd:,.0f})"
    )
    for a in allocations:
        logger.info(f"  {a.protocol.value}: ${a.amount_usd:,.2f} ({a.target_pct:.1%})")

    return AllocationPlan(
        allocations=allocations,
        scored_protocols=scored,
        total_allocated_usd=total_alloc,
        total_capital_usd=total_capital_usd,
        reserve_usd=total_capital_usd - total_alloc,
    )


def _apply_caps(
    weights: dict[ProtocolName, Decimal],
    max_pct: Decimal,
) -> dict[ProtocolName, Decimal]:
    """Cap individual weights and redistribute excess proportionally.

    Iterates until no weight exceeds the cap. Each round, capped protocols
    lock in at max_pct and the excess is redistributed among uncapped ones.
    """
    capped = dict(weights)
    locked: set[ProtocolName] = set()

    for _ in range(10):  # Safety bound
        excess = Decimal("0")
        newly_locked = set()

        for proto, w in capped.items():
            if proto in locked:
                continue
            if w > max_pct:
                excess += w - max_pct
                capped[proto] = max_pct
                newly_locked.add(proto)

        if not newly_locked:
            break

        locked |= newly_locked

        # Redistribute excess to uncapped protocols proportionally
        unlocked = {p: w for p, w in capped.items() if p not in locked}
        if unlocked:
            unlocked_total = sum(unlocked.values())
            if unlocked_total > 0:
                for p in unlocked:
                    capped[p] += excess * (capped[p] / unlocked_total)

    return capped
