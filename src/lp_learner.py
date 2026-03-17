"""LP performance learning loop — tracks decisions and learns from outcomes.

Stores every range decision + actual fees earned + IL suffered in SQLite.
Computes win rates per regime/width combo and feeds adjustments back
into the optimizer.

The agent literally gets better at LP management the longer it runs.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "lp_learner.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lp_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_id INTEGER,
    action TEXT NOT NULL,
    regime TEXT NOT NULL,
    regime_confidence REAL NOT NULL,
    tick_lower INTEGER,
    tick_upper INTEGER,
    width_pct REAL,
    entry_price REAL,
    atr_pct REAL,
    rsi REAL,
    adx REAL,
    reasoning TEXT
);

CREATE TABLE IF NOT EXISTS lp_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    exit_price REAL,
    fees_weth REAL DEFAULT 0,
    fees_usdc REAL DEFAULT 0,
    fees_usd REAL DEFAULT 0,
    il_pct REAL DEFAULT 0,
    net_pnl_usd REAL DEFAULT 0,
    hold_duration_hours REAL DEFAULT 0,
    rebalance_reason TEXT,
    FOREIGN KEY (decision_id) REFERENCES lp_decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_regime ON lp_decisions(regime);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON lp_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON lp_outcomes(decision_id);
"""


@dataclass
class PerformanceStats:
    """Aggregated performance stats for a regime/width combo."""
    regime: str
    avg_width_pct: float
    total_decisions: int
    total_outcomes: int
    win_rate: float  # % of outcomes with net_pnl > 0
    avg_net_pnl_usd: float
    avg_fees_usd: float
    avg_il_pct: float
    avg_hold_hours: float
    recommended_width_adjustment: float  # Multiplier: >1 = widen, <1 = tighten


@dataclass
class WidthAdjustment:
    """Width adjustment recommendation from learning loop."""
    regime: str
    current_multiplier: float
    sample_size: int
    reasoning: str


def _get_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def record_decision(
    action: str,
    regime: str,
    regime_confidence: float,
    tick_lower: int | None = None,
    tick_upper: int | None = None,
    width_pct: float | None = None,
    entry_price: float | None = None,
    atr_pct: float | None = None,
    rsi: float | None = None,
    adx: float | None = None,
    token_id: int | None = None,
    reasoning: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Record an LP decision. Returns the decision ID."""
    conn = _get_db(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO lp_decisions
               (timestamp, token_id, action, regime, regime_confidence,
                tick_lower, tick_upper, width_pct, entry_price, atr_pct, rsi, adx, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), token_id, action, regime, regime_confidence,
             tick_lower, tick_upper, width_pct, entry_price, atr_pct, rsi, adx, reasoning),
        )
        decision_id = cursor.lastrowid
        conn.commit()
        return decision_id
    finally:
        conn.close()


def record_outcome(
    decision_id: int,
    exit_price: float,
    fees_weth: float = 0,
    fees_usdc: float = 0,
    fees_usd: float = 0,
    il_pct: float = 0,
    net_pnl_usd: float = 0,
    hold_duration_hours: float = 0,
    rebalance_reason: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Record the outcome of an LP decision."""
    conn = _get_db(db_path)
    try:
        conn.execute(
            """INSERT INTO lp_outcomes
               (decision_id, timestamp, exit_price, fees_weth, fees_usdc,
                fees_usd, il_pct, net_pnl_usd, hold_duration_hours, rebalance_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, time.time(), exit_price, fees_weth, fees_usdc,
             fees_usd, il_pct, net_pnl_usd, hold_duration_hours, rebalance_reason),
        )
        conn.commit()
    finally:
        conn.close()


def get_performance_by_regime(db_path: Path | None = None) -> list[PerformanceStats]:
    """Get aggregated performance stats grouped by regime.

    Returns stats that can be used to adjust optimizer width multipliers.
    """
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT
            d.regime,
            AVG(d.width_pct) as avg_width,
            COUNT(DISTINCT d.id) as total_decisions,
            COUNT(o.id) as total_outcomes,
            SUM(CASE WHEN o.net_pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
            AVG(o.net_pnl_usd) as avg_pnl,
            AVG(o.fees_usd) as avg_fees,
            AVG(o.il_pct) as avg_il,
            AVG(o.hold_duration_hours) as avg_hold
        FROM lp_decisions d
        LEFT JOIN lp_outcomes o ON o.decision_id = d.id
        WHERE d.action IN ('mint', 'rebalance')
        GROUP BY d.regime
        HAVING total_outcomes > 0
    """).fetchall()
    conn.close()

    stats = []
    for row in rows:
        regime, avg_width, total_dec, total_out, wins, avg_pnl, avg_fees, avg_il, avg_hold = row
        win_rate = (wins / total_out * 100) if total_out > 0 else 0

        # Compute width adjustment recommendation
        # If win rate < 50%, we're probably too tight (getting out-of-range)
        # If win rate > 70% but fees are low, we could go tighter
        if total_out < 3:
            adjustment = 1.0  # Not enough data
        elif win_rate < 40:
            adjustment = 1.3  # Widen — too many losses (likely OOR)
        elif win_rate < 50:
            adjustment = 1.15
        elif win_rate > 70 and avg_il is not None and abs(avg_il) < 0.01:
            adjustment = 0.85  # Tighten — winning easily with low IL
        elif win_rate > 80:
            adjustment = 0.9
        else:
            adjustment = 1.0  # Performing well, don't change

        stats.append(PerformanceStats(
            regime=regime,
            avg_width_pct=avg_width or 0,
            total_decisions=total_dec,
            total_outcomes=total_out,
            win_rate=win_rate,
            avg_net_pnl_usd=avg_pnl or 0,
            avg_fees_usd=avg_fees or 0,
            avg_il_pct=avg_il or 0,
            avg_hold_hours=avg_hold or 0,
            recommended_width_adjustment=adjustment,
        ))

    return stats


def get_width_adjustments(db_path: Path | None = None) -> dict[str, WidthAdjustment]:
    """Get width adjustment multipliers per regime from historical performance.

    Returns dict mapping regime → WidthAdjustment. Pass these to the
    optimizer to modify its base width calculation.

    Example:
        adjustments = get_width_adjustments()
        if "sideways" in adjustments:
            width *= adjustments["sideways"].current_multiplier
    """
    stats = get_performance_by_regime(db_path)
    adjustments = {}

    for s in stats:
        if s.total_outcomes < 3:
            reasoning = f"Only {s.total_outcomes} outcomes — not enough data to adjust"
            multiplier = 1.0
        elif s.win_rate < 40:
            reasoning = f"Win rate {s.win_rate:.0f}% too low — widening range by 30%"
            multiplier = s.recommended_width_adjustment
        elif s.win_rate > 70 and abs(s.avg_il_pct) < 0.01:
            reasoning = f"Win rate {s.win_rate:.0f}% with low IL ({s.avg_il_pct:.2%}) — tightening range"
            multiplier = s.recommended_width_adjustment
        else:
            reasoning = f"Win rate {s.win_rate:.0f}%, avg PnL ${s.avg_net_pnl_usd:.2f} — maintaining width"
            multiplier = 1.0

        adjustments[s.regime] = WidthAdjustment(
            regime=s.regime,
            current_multiplier=multiplier,
            sample_size=s.total_outcomes,
            reasoning=reasoning,
        )

    return adjustments


def get_summary(db_path: Path | None = None) -> dict:
    """Get a summary of all LP learning data."""
    conn = _get_db(db_path)

    total_decisions = conn.execute("SELECT COUNT(*) FROM lp_decisions").fetchone()[0]
    total_outcomes = conn.execute("SELECT COUNT(*) FROM lp_outcomes").fetchone()[0]

    total_pnl = conn.execute("SELECT COALESCE(SUM(net_pnl_usd), 0) FROM lp_outcomes").fetchone()[0]
    total_fees = conn.execute("SELECT COALESCE(SUM(fees_usd), 0) FROM lp_outcomes").fetchone()[0]

    wins = conn.execute("SELECT COUNT(*) FROM lp_outcomes WHERE net_pnl_usd > 0").fetchone()[0]
    win_rate = (wins / total_outcomes * 100) if total_outcomes > 0 else 0

    conn.close()

    return {
        "total_decisions": total_decisions,
        "total_outcomes": total_outcomes,
        "total_pnl_usd": total_pnl,
        "total_fees_usd": total_fees,
        "overall_win_rate": win_rate,
        "adjustments": get_width_adjustments(db_path),
    }
