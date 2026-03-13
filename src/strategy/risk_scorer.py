"""Protocol risk scoring — TVL, age, audit status, utilization, bad debt history.

Each protocol gets a risk score from 0.0 (safest) to 1.0 (riskiest).
The score is used as a penalty in the allocation engine:
  risk_adjusted_yield = net_apy * (1 - risk_score)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from src.models import ProtocolName, ValidatedRate

logger = logging.getLogger(__name__)

# ── Static protocol metadata ────────────────────────────────────────────────
# These are facts about each protocol that don't change per-cycle.
# Updated manually when protocols get new audits, suffer incidents, etc.

PROTOCOL_METADATA: dict[ProtocolName, dict] = {
    ProtocolName.AAVE_V3: {
        "launch_date": datetime(2023, 1, 27, tzinfo=timezone.utc),  # Aave V3 Ethereum launch
        "audit_count": 8,
        "last_audit_date": datetime(2024, 6, 1, tzinfo=timezone.utc),
        "bad_debt_events": 1,  # CRV Nov 2022 incident (~$1.6M, covered by safety module)
        "governance_active": True,
        "bug_bounty_usd": 10_000_000,  # $10M via Immunefi
    },
    ProtocolName.MORPHO: {
        "launch_date": datetime(2024, 1, 10, tzinfo=timezone.utc),  # Morpho Blue mainnet
        "audit_count": 4,
        "last_audit_date": datetime(2024, 3, 1, tzinfo=timezone.utc),
        "bad_debt_events": 0,
        "governance_active": True,
        "bug_bounty_usd": 2_000_000,
    },
    ProtocolName.COMPOUND_V3: {
        "launch_date": datetime(2022, 8, 26, tzinfo=timezone.utc),  # Comet launch
        "audit_count": 6,
        "last_audit_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "bad_debt_events": 0,
        "governance_active": True,
        "bug_bounty_usd": 5_000_000,
    },
}


@dataclass
class RiskScore:
    """Breakdown of protocol risk scoring."""
    protocol: ProtocolName
    total: Decimal  # 0.0 (safest) to 1.0 (riskiest)
    tvl_score: Decimal = Decimal("0")
    age_score: Decimal = Decimal("0")
    audit_score: Decimal = Decimal("0")
    utilization_score: Decimal = Decimal("0")
    bad_debt_score: Decimal = Decimal("0")
    details: list[str] = field(default_factory=list)

    @property
    def penalty(self) -> Decimal:
        """Risk penalty for yield adjustment: 1 - total."""
        return Decimal("1") - self.total


def score_protocol_risk(
    rate: ValidatedRate,
    now: datetime | None = None,
) -> RiskScore:
    """Score a protocol's risk based on multiple factors.

    Weights (must sum to 1.0):
    - TVL:         25% — larger TVL = safer (more liquidity, more battle-tested)
    - Age:         20% — older protocols have survived more market events
    - Audits:      20% — more audits + recent audits = lower risk
    - Utilization:  20% — higher utilization = harder to withdraw
    - Bad debt:    15% — past incidents indicate systemic risk
    """
    now = now or datetime.now(tz=timezone.utc)
    meta = PROTOCOL_METADATA.get(rate.protocol, {})
    details: list[str] = []

    # ── TVL score (25%) ──────────────────────────────────────────────────
    tvl = float(rate.tvl_usd)
    if tvl >= 500_000_000:
        tvl_score = Decimal("0.0")
        details.append(f"TVL ${tvl:,.0f} — excellent")
    elif tvl >= 100_000_000:
        tvl_score = Decimal("0.1")
        details.append(f"TVL ${tvl:,.0f} — good")
    elif tvl >= 50_000_000:
        tvl_score = Decimal("0.3")
        details.append(f"TVL ${tvl:,.0f} — acceptable")
    elif tvl >= 10_000_000:
        tvl_score = Decimal("0.6")
        details.append(f"TVL ${tvl:,.0f} — low")
    else:
        tvl_score = Decimal("0.9")
        details.append(f"TVL ${tvl:,.0f} — very low, high risk")

    # ── Age score (20%) ──────────────────────────────────────────────────
    launch = meta.get("launch_date")
    if launch:
        age_days = (now - launch).days
        if age_days >= 730:  # 2+ years
            age_score = Decimal("0.0")
            details.append(f"Age {age_days}d — battle-tested")
        elif age_days >= 365:
            age_score = Decimal("0.2")
            details.append(f"Age {age_days}d — established")
        elif age_days >= 180:
            age_score = Decimal("0.4")
            details.append(f"Age {age_days}d — moderate")
        else:
            age_score = Decimal("0.7")
            details.append(f"Age {age_days}d — young, higher risk")
    else:
        age_score = Decimal("0.5")
        details.append("Age unknown — moderate risk assumed")

    # ── Audit score (20%) ────────────────────────────────────────────────
    audit_count = meta.get("audit_count", 0)
    last_audit = meta.get("last_audit_date")

    if audit_count >= 5 and last_audit and (now - last_audit).days < 365:
        audit_score = Decimal("0.0")
        details.append(f"{audit_count} audits, last <1y — excellent")
    elif audit_count >= 3:
        audit_score = Decimal("0.2")
        details.append(f"{audit_count} audits — good")
    elif audit_count >= 1:
        audit_score = Decimal("0.5")
        details.append(f"{audit_count} audit(s) — limited")
    else:
        audit_score = Decimal("0.9")
        details.append("No audits — very high risk")

    # ── Utilization score (20%) ──────────────────────────────────────────
    util = float(rate.utilization)
    if util <= 0.5:
        util_score = Decimal("0.0")
        details.append(f"Utilization {util:.0%} — plenty of liquidity")
    elif util <= 0.7:
        util_score = Decimal("0.1")
        details.append(f"Utilization {util:.0%} — healthy")
    elif util <= 0.85:
        util_score = Decimal("0.3")
        details.append(f"Utilization {util:.0%} — elevated")
    elif util <= 0.95:
        util_score = Decimal("0.6")
        details.append(f"Utilization {util:.0%} — high, withdrawal risk")
    else:
        util_score = Decimal("0.9")
        details.append(f"Utilization {util:.0%} — critical, likely withdrawal issues")

    # ── Bad debt score (15%) ─────────────────────────────────────────────
    bad_debt = meta.get("bad_debt_events", 0)
    if bad_debt == 0:
        bd_score = Decimal("0.0")
        details.append("No bad debt events")
    elif bad_debt == 1:
        bd_score = Decimal("0.3")
        details.append(f"{bad_debt} bad debt event — recovered")
    else:
        bd_score = Decimal("0.7")
        details.append(f"{bad_debt} bad debt events — pattern of risk")

    # ── Weighted total ───────────────────────────────────────────────────
    total = (
        tvl_score * Decimal("0.25")
        + age_score * Decimal("0.20")
        + audit_score * Decimal("0.20")
        + util_score * Decimal("0.20")
        + bd_score * Decimal("0.15")
    )

    # Clamp to [0, 1]
    total = max(Decimal("0"), min(Decimal("1"), total))

    score = RiskScore(
        protocol=rate.protocol,
        total=total,
        tvl_score=tvl_score,
        age_score=age_score,
        audit_score=audit_score,
        utilization_score=util_score,
        bad_debt_score=bd_score,
        details=details,
    )

    logger.info(
        f"Risk score {rate.protocol.value}: {total:.3f} "
        f"(TVL={tvl_score}, Age={age_score}, Audit={audit_score}, "
        f"Util={util_score}, BadDebt={bd_score})"
    )
    return score
