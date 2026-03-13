"""SQLite persistence for execution log, portfolio, and audit trail.

Uses aiosqlite for async access. Schema auto-migrates on first use.
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiosqlite

from src.models import (
    ExecutionRecord,
    PortfolioSnapshot,
)

logger = logging.getLogger(__name__)


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse an ISO timestamp string, ensuring timezone-aware result.

    Handles both Python 3.10 (no tz suffix parsing) and 3.11+ gracefully.
    """
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "yield_agent.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS execution_log (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    protocol TEXT NOT NULL,
    chain TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    mode TEXT NOT NULL,
    tx_hash TEXT,
    block_number INTEGER,
    gas_cost_usd TEXT NOT NULL DEFAULT '0',
    simulated_gas_usd TEXT NOT NULL DEFAULT '0',
    reasoning TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_capital_usd TEXT NOT NULL,
    allocated_usd TEXT NOT NULL,
    reserve_usd TEXT NOT NULL,
    unrealized_yield_usd TEXT NOT NULL,
    total_gas_spent_usd TEXT NOT NULL,
    positions_json TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exec_timestamp ON execution_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_exec_protocol ON execution_log(protocol);
CREATE INDEX IF NOT EXISTS idx_snap_timestamp ON portfolio_snapshots(timestamp);
"""


class Database:
    """Async SQLite database for yield agent persistence."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open connection and initialize schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"Database connected: {self.db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    def _require_connection(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("Database not connected — call connect() first")
        return self._db

    # ── Execution log ─────────────────────────────────────────────────────

    async def insert_execution(self, record: ExecutionRecord) -> None:
        """Insert an execution record into the log."""
        db = self._require_connection()
        await db.execute(
            """INSERT INTO execution_log
               (id, action, protocol, chain, amount_usd, mode, tx_hash,
                block_number, gas_cost_usd, simulated_gas_usd, reasoning,
                status, error, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id,
                record.action.value,
                record.protocol.value,
                record.chain.value,
                str(record.amount_usd),
                record.mode.value,
                record.tx_hash,
                record.block_number,
                str(record.gas_cost_usd),
                str(record.simulated_gas_usd),
                record.reasoning,
                record.status.value if hasattr(record.status, 'value') else record.status,
                record.error,
                record.timestamp.isoformat(),
            ),
        )
        await db.commit()

    async def update_execution_status(
        self, record_id: str, status: str, error: str | None = None,
        tx_hash: str | None = None, block_number: int | None = None,
    ) -> None:
        """Update an execution record's status after completion."""
        db = self._require_connection()
        await db.execute(
            """UPDATE execution_log
               SET status = ?, error = ?, tx_hash = COALESCE(?, tx_hash),
                   block_number = COALESCE(?, block_number)
               WHERE id = ?""",
            (status, error, tx_hash, block_number, record_id),
        )
        await db.commit()

    async def get_executions(
        self, limit: int = 50, protocol: str | None = None,
    ) -> list[dict]:
        """Fetch recent execution records."""
        db = self._require_connection()
        if protocol:
            cursor = await db.execute(
                "SELECT * FROM execution_log WHERE protocol = ? ORDER BY timestamp DESC LIMIT ?",
                (protocol, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM execution_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_last_execution_time(self, protocol: str) -> datetime | None:
        """Get the timestamp of the last real (non-simulated) successful execution.

        Used for withdrawal cooldown enforcement.
        Excludes dry-run/simulated records so they don't poison the cooldown.
        """
        db = self._require_connection()
        cursor = await db.execute(
            """SELECT timestamp FROM execution_log
               WHERE protocol = ? AND status = 'success'
               AND mode != 'dry_run'
               ORDER BY timestamp DESC LIMIT 1""",
            (protocol,),
        )
        row = await cursor.fetchone()
        if row:
            return _parse_timestamp(row["timestamp"])
        return None

    # ── Portfolio snapshots ───────────────────────────────────────────────

    async def insert_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Save a portfolio snapshot."""
        db = self._require_connection()
        positions_json = json.dumps(
            {k: str(v) for k, v in snapshot.positions.items()}
        )
        await db.execute(
            """INSERT INTO portfolio_snapshots
               (total_capital_usd, allocated_usd, reserve_usd,
                unrealized_yield_usd, total_gas_spent_usd,
                positions_json, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(snapshot.total_capital_usd),
                str(snapshot.allocated_usd),
                str(snapshot.reserve_usd),
                str(snapshot.unrealized_yield_usd),
                str(snapshot.total_gas_spent_usd),
                positions_json,
                snapshot.timestamp.isoformat(),
            ),
        )
        await db.commit()

    async def get_latest_snapshot(self) -> PortfolioSnapshot | None:
        """Get the most recent non-corrupt portfolio snapshot."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 5"
        )
        rows = await cursor.fetchall()
        for row in rows:
            snap = self._row_to_snapshot(row)
            if snap is not None:
                return snap
        return None

    async def get_snapshots(self, limit: int = 100) -> list[PortfolioSnapshot]:
        """Get recent portfolio snapshots for P&L tracking."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [s for s in (self._row_to_snapshot(row) for row in rows) if s is not None]

    def _row_to_snapshot(self, row) -> PortfolioSnapshot | None:
        """Convert a database row to PortfolioSnapshot.

        Returns None if the row contains corrupt data (graceful degradation).
        """
        try:
            positions_raw = json.loads(row["positions_json"])
            positions = {k: Decimal(v) for k, v in positions_raw.items()}
            return PortfolioSnapshot(
                total_capital_usd=Decimal(row["total_capital_usd"]),
                allocated_usd=Decimal(row["allocated_usd"]),
                reserve_usd=Decimal(row["reserve_usd"]),
                unrealized_yield_usd=Decimal(row["unrealized_yield_usd"]),
                total_gas_spent_usd=Decimal(row["total_gas_spent_usd"]),
                positions=positions,
                timestamp=_parse_timestamp(row["timestamp"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Corrupt snapshot row skipped: {e}")
            return None

    # ── Stats / queries ───────────────────────────────────────────────────

    async def get_total_gas_spent(self) -> Decimal:
        """Sum of all gas spent (real + simulated) across successful executions.

        Uses Python Decimal arithmetic instead of SQL REAL to avoid
        floating-point precision loss on sub-cent gas costs.
        """
        db = self._require_connection()
        cursor = await db.execute(
            """SELECT gas_cost_usd, simulated_gas_usd FROM execution_log
               WHERE status IN ('success', 'simulated')"""
        )
        rows = await cursor.fetchall()
        total = Decimal("0")
        for row in rows:
            total += Decimal(row["gas_cost_usd"]) + Decimal(row["simulated_gas_usd"])
        return total

    async def get_execution_count(self) -> dict:
        """Count executions by status."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT status, COUNT(*) as count FROM execution_log GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {row["status"]: row["count"] for row in rows}
