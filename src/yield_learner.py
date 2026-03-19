"""Yield allocation learning loop — tracks protocol picks and learns from outcomes.

Every allocation decision is recorded: which protocol, predicted APY, risk score.
After the hold period, we compare predicted vs actual yield earned.
Over time, the agent learns which protocols consistently over/under-deliver
and adjusts its risk scoring weights accordingly.

The agent literally gets better at picking where to park capital the longer it runs.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "yield_learner.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS yield_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    protocol TEXT NOT NULL,
    action TEXT NOT NULL,
    predicted_apy REAL NOT NULL,
    risk_score REAL NOT NULL,
    risk_adjusted_apy REAL NOT NULL,
    amount_usd REAL NOT NULL,
    tvl_usd REAL,
    utilization REAL,
    reasoning TEXT
);

CREATE TABLE IF NOT EXISTS yield_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    actual_apy REAL NOT NULL,
    apy_error REAL NOT NULL,
    yield_earned_usd REAL NOT NULL,
    gas_spent_usd REAL NOT NULL,
    net_profit_usd REAL NOT NULL,
    hold_hours REAL NOT NULL,
    was_profitable INTEGER NOT NULL,
    exit_reason TEXT,
    FOREIGN KEY (decision_id) REFERENCES yield_decisions(id)
);

CREATE TABLE IF NOT EXISTS protocol_accuracy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    protocol TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    avg_apy_error REAL NOT NULL,
    apy_overestimate_pct REAL NOT NULL,
    win_rate REAL NOT NULL,
    avg_net_profit_usd REAL NOT NULL,
    risk_weight_adjustment REAL NOT NULL,
    reasoning TEXT
);

CREATE INDEX IF NOT EXISTS idx_yd_protocol ON yield_decisions(protocol);
CREATE INDEX IF NOT EXISTS idx_yd_ts ON yield_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_yo_decision ON yield_outcomes(decision_id);
CREATE INDEX IF NOT EXISTS idx_pa_protocol ON protocol_accuracy(protocol);
"""


@dataclass
class ProtocolPerformance:
    """Aggregated performance stats for a protocol."""
    protocol: str
    total_decisions: int
    total_outcomes: int
    win_rate: float
    avg_predicted_apy: float
    avg_actual_apy: float
    avg_apy_error: float
    overestimate_pct: float
    avg_net_profit_usd: float
    total_yield_usd: float
    total_gas_usd: float
    risk_weight_adjustment: float
    reasoning: str


@dataclass
class LearnerSummary:
    """Overall learning system summary for dashboard display."""
    total_decisions: int
    total_outcomes: int
    overall_win_rate: float
    total_yield_usd: float
    total_gas_usd: float
    total_net_profit_usd: float
    protocols: list[ProtocolPerformance]
    improvement_score: float  # 0-100, how much better than baseline


def _get_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def record_allocation(
    protocol: str,
    action: str,
    predicted_apy: float,
    risk_score: float,
    risk_adjusted_apy: float,
    amount_usd: float,
    tvl_usd: float | None = None,
    utilization: float | None = None,
    reasoning: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Record a yield allocation decision. Returns the decision ID."""
    conn = _get_db(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO yield_decisions
               (timestamp, protocol, action, predicted_apy, risk_score,
                risk_adjusted_apy, amount_usd, tvl_usd, utilization, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), protocol, action, predicted_apy, risk_score,
             risk_adjusted_apy, amount_usd, tvl_usd, utilization, reasoning),
        )
        decision_id = cursor.lastrowid
        conn.commit()
        logger.info(
            f"Recorded allocation: {action} {protocol} ${amount_usd:.2f} "
            f"(predicted {predicted_apy:.2f}% APY, risk {risk_score:.3f})"
        )
        return decision_id
    finally:
        conn.close()


def record_yield_outcome(
    decision_id: int,
    actual_apy: float,
    yield_earned_usd: float,
    gas_spent_usd: float,
    hold_hours: float,
    exit_reason: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Record the outcome of a yield allocation decision."""
    conn = _get_db(db_path)
    try:
        # Look up the predicted APY for this decision
        row = conn.execute(
            "SELECT predicted_apy FROM yield_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if not row:
            logger.warning(f"Decision {decision_id} not found — skipping outcome")
            return

        predicted_apy = row[0]
        apy_error = actual_apy - predicted_apy
        net_profit = yield_earned_usd - gas_spent_usd
        was_profitable = 1 if net_profit > 0 else 0

        conn.execute(
            """INSERT INTO yield_outcomes
               (decision_id, timestamp, actual_apy, apy_error, yield_earned_usd,
                gas_spent_usd, net_profit_usd, hold_hours, was_profitable, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, time.time(), actual_apy, apy_error, yield_earned_usd,
             gas_spent_usd, net_profit, hold_hours, was_profitable, exit_reason),
        )
        conn.commit()
        logger.info(
            f"Recorded outcome for decision {decision_id}: "
            f"actual {actual_apy:.2f}% vs predicted {predicted_apy:.2f}% "
            f"(error {apy_error:+.2f}%), net ${net_profit:.4f}"
        )
    finally:
        conn.close()


def get_protocol_performance(db_path: Path | None = None) -> list[ProtocolPerformance]:
    """Get aggregated performance stats per protocol.

    Computes risk weight adjustments based on prediction accuracy:
    - Protocols that consistently over-promise get penalized (higher risk weight)
    - Protocols that consistently deliver get rewarded (lower risk weight)
    """
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT
            d.protocol,
            COUNT(DISTINCT d.id) as total_decisions,
            COUNT(o.id) as total_outcomes,
            SUM(CASE WHEN o.was_profitable = 1 THEN 1 ELSE 0 END) as wins,
            AVG(d.predicted_apy) as avg_predicted,
            AVG(o.actual_apy) as avg_actual,
            AVG(o.apy_error) as avg_error,
            SUM(CASE WHEN o.apy_error < 0 THEN 1 ELSE 0 END) as overestimates,
            AVG(o.net_profit_usd) as avg_profit,
            SUM(o.yield_earned_usd) as total_yield,
            SUM(o.gas_spent_usd) as total_gas
        FROM yield_decisions d
        LEFT JOIN yield_outcomes o ON o.decision_id = d.id
        WHERE d.action = 'supply'
        GROUP BY d.protocol
        HAVING total_outcomes > 0
    """).fetchall()
    conn.close()

    results = []
    for row in rows:
        (protocol, total_dec, total_out, wins, avg_pred, avg_actual,
         avg_error, overestimates, avg_profit, total_yield, total_gas) = row

        win_rate = (wins / total_out * 100) if total_out > 0 else 0
        overestimate_pct = (overestimates / total_out * 100) if total_out > 0 else 0

        # Compute risk weight adjustment
        # If protocol consistently over-promises APY, increase its risk penalty
        # If it consistently delivers or exceeds, decrease penalty
        if total_out < 3:
            adjustment = 1.0
            reasoning = f"Only {total_out} outcomes — not enough data"
        elif avg_error < -0.5:
            # APY consistently lower than predicted by >0.5%
            adjustment = 1.15  # Increase risk weight 15%
            reasoning = (
                f"APY overestimated by avg {abs(avg_error):.2f}% — "
                f"increasing risk penalty"
            )
        elif avg_error < -0.2:
            adjustment = 1.05
            reasoning = (
                f"Slight APY overestimate ({abs(avg_error):.2f}%) — "
                f"minor risk increase"
            )
        elif avg_error > 0.3 and win_rate > 60:
            # APY better than predicted and mostly profitable
            adjustment = 0.9  # Decrease risk weight 10%
            reasoning = (
                f"Consistently delivers +{avg_error:.2f}% above predicted, "
                f"{win_rate:.0f}% win rate — reducing risk penalty"
            )
        elif win_rate > 70:
            adjustment = 0.95
            reasoning = f"{win_rate:.0f}% win rate — slight risk reduction"
        else:
            adjustment = 1.0
            reasoning = (
                f"Predictions accurate (avg error {avg_error:+.2f}%), "
                f"{win_rate:.0f}% wins — maintaining current weights"
            )

        # Persist the accuracy snapshot
        _save_accuracy_snapshot(
            protocol, total_out, avg_error, overestimate_pct,
            win_rate, avg_profit or 0, adjustment, reasoning, db_path,
        )

        results.append(ProtocolPerformance(
            protocol=protocol,
            total_decisions=total_dec,
            total_outcomes=total_out,
            win_rate=win_rate,
            avg_predicted_apy=avg_pred or 0,
            avg_actual_apy=avg_actual or 0,
            avg_apy_error=avg_error or 0,
            overestimate_pct=overestimate_pct,
            avg_net_profit_usd=avg_profit or 0,
            total_yield_usd=total_yield or 0,
            total_gas_usd=total_gas or 0,
            risk_weight_adjustment=adjustment,
            reasoning=reasoning,
        ))

    return results


def _save_accuracy_snapshot(
    protocol: str,
    sample_size: int,
    avg_error: float,
    overestimate_pct: float,
    win_rate: float,
    avg_profit: float,
    adjustment: float,
    reasoning: str,
    db_path: Path | None = None,
) -> None:
    """Persist a protocol accuracy snapshot for historical tracking."""
    conn = _get_db(db_path)
    try:
        conn.execute(
            """INSERT INTO protocol_accuracy
               (timestamp, protocol, sample_size, avg_apy_error,
                apy_overestimate_pct, win_rate, avg_net_profit_usd,
                risk_weight_adjustment, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), protocol, sample_size, avg_error,
             overestimate_pct, win_rate, avg_profit, adjustment, reasoning),
        )
        conn.commit()
    finally:
        conn.close()


def get_risk_adjustments(db_path: Path | None = None) -> dict[str, float]:
    """Get learned risk weight multipliers per protocol.

    Returns dict mapping protocol name → multiplier.
    Multiply the protocol's static risk score by this to get the adjusted score.

    Example:
        adjustments = get_risk_adjustments()
        if "aave-v3" in adjustments:
            risk_score *= adjustments["aave-v3"]  # e.g. 1.15 = penalize
    """
    performance = get_protocol_performance(db_path)
    return {p.protocol: p.risk_weight_adjustment for p in performance}


def get_summary(db_path: Path | None = None) -> LearnerSummary:
    """Get full learning system summary — used by dashboard and CLI."""
    conn = _get_db(db_path)

    total_decisions = conn.execute(
        "SELECT COUNT(*) FROM yield_decisions"
    ).fetchone()[0]
    total_outcomes = conn.execute(
        "SELECT COUNT(*) FROM yield_outcomes"
    ).fetchone()[0]

    agg = conn.execute("""
        SELECT
            SUM(CASE WHEN was_profitable = 1 THEN 1 ELSE 0 END),
            SUM(yield_earned_usd),
            SUM(gas_spent_usd),
            SUM(net_profit_usd)
        FROM yield_outcomes
    """).fetchone()

    conn.close()

    wins = agg[0] or 0
    total_yield = agg[1] or 0
    total_gas = agg[2] or 0
    total_net = agg[3] or 0
    win_rate = (wins / total_outcomes * 100) if total_outcomes > 0 else 0

    protocols = get_protocol_performance(db_path)

    # Improvement score: how much better are recent decisions vs early ones?
    improvement = _compute_improvement_score(db_path)

    return LearnerSummary(
        total_decisions=total_decisions,
        total_outcomes=total_outcomes,
        overall_win_rate=win_rate,
        total_yield_usd=total_yield,
        total_gas_usd=total_gas,
        total_net_profit_usd=total_net,
        protocols=protocols,
        improvement_score=improvement,
    )


def _compute_improvement_score(db_path: Path | None = None) -> float:
    """Compare recent performance vs early performance.

    Returns 0-100 score where:
    - 50 = no change (baseline)
    - >50 = improving (recent decisions are better)
    - <50 = degrading (recent decisions are worse)
    """
    conn = _get_db(db_path)

    total = conn.execute("SELECT COUNT(*) FROM yield_outcomes").fetchone()[0]
    if total < 6:
        conn.close()
        return 50.0  # Not enough data

    midpoint = total // 2

    early = conn.execute("""
        SELECT AVG(net_profit_usd), AVG(ABS(apy_error))
        FROM yield_outcomes
        ORDER BY id ASC
        LIMIT ?
    """, (midpoint,)).fetchone()

    recent = conn.execute("""
        SELECT AVG(net_profit_usd), AVG(ABS(apy_error))
        FROM (SELECT * FROM yield_outcomes ORDER BY id DESC LIMIT ?)
    """, (midpoint,)).fetchone()

    conn.close()

    early_profit, early_error = early[0] or 0, early[1] or 0
    recent_profit, recent_error = recent[0] or 0, recent[1] or 0

    # Score based on profit improvement and prediction accuracy improvement
    profit_delta = recent_profit - early_profit
    error_delta = early_error - recent_error  # Positive = error decreased = good

    # Normalize to 0-100 scale
    score = 50.0
    if early_profit != 0:
        score += min(25, max(-25, (profit_delta / max(abs(early_profit), 0.001)) * 25))
    if early_error != 0:
        score += min(25, max(-25, (error_delta / max(abs(early_error), 0.001)) * 25))

    return max(0, min(100, score))
