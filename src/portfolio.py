"""Portfolio state tracker — tracks positions, yield, and P&L.

Paper mode: positions updated via simulated execution.
Live mode: positions verified against on-chain balances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from src.database import Database
from src.models import (
    ActionType,
    ExecutionRecord,
    PortfolioSnapshot,
    ProtocolName,
)

if TYPE_CHECKING:
    from src.protocols.base import ProtocolAdapter

logger = logging.getLogger(__name__)


class Portfolio:
    """In-memory portfolio state backed by SQLite snapshots."""

    def __init__(self, total_capital_usd: Decimal, db: Database):
        self.total_capital_usd = total_capital_usd
        self.db = db
        # protocol name (str) -> allocated amount USD
        self.positions: dict[str, Decimal] = {}
        self.total_gas_spent_usd = Decimal("0")
        self.unrealized_yield_usd = Decimal("0")

    @property
    def allocated_usd(self) -> Decimal:
        return sum(self.positions.values(), Decimal("0"))

    @property
    def reserve_usd(self) -> Decimal:
        return self.total_capital_usd - self.allocated_usd

    @property
    def net_value_usd(self) -> Decimal:
        return self.total_capital_usd + self.unrealized_yield_usd - self.total_gas_spent_usd

    async def load_from_db(self) -> bool:
        """Load latest portfolio state from database. Returns True if found.

        Validates that loaded positions don't exceed current capital.
        If they do, scales positions down proportionally to fit.
        """
        snapshot = await self.db.get_latest_snapshot()
        if not snapshot:
            return False

        self.positions = dict(snapshot.positions)
        self.total_gas_spent_usd = snapshot.total_gas_spent_usd
        self.unrealized_yield_usd = snapshot.unrealized_yield_usd

        # Guard: if capital was reduced below existing allocations, scale down
        if self.allocated_usd > self.total_capital_usd:
            scale = self.total_capital_usd / self.allocated_usd
            logger.warning(
                f"Loaded positions (${self.allocated_usd:,.2f}) exceed "
                f"capital (${self.total_capital_usd:,.2f}) — scaling down by {scale:.2%}"
            )
            self.positions = {
                k: (v * scale).quantize(Decimal("0.01"))
                for k, v in self.positions.items()
            }

        logger.info(
            f"Portfolio loaded: ${self.allocated_usd:,.2f} allocated, "
            f"${self.reserve_usd:,.2f} reserve"
        )
        return True

    async def save_snapshot(self) -> None:
        """Save current state as a portfolio snapshot."""
        snapshot = PortfolioSnapshot(
            total_capital_usd=self.total_capital_usd,
            allocated_usd=self.allocated_usd,
            reserve_usd=self.reserve_usd,
            unrealized_yield_usd=self.unrealized_yield_usd,
            total_gas_spent_usd=self.total_gas_spent_usd,
            positions=dict(self.positions),
        )
        await self.db.insert_snapshot(snapshot)

    def apply_execution(self, record: ExecutionRecord) -> None:
        """Update portfolio state after an execution."""
        proto_key = record.protocol.value

        if record.action == ActionType.SUPPLY:
            current = self.positions.get(proto_key, Decimal("0"))
            self.positions[proto_key] = current + record.amount_usd

        elif record.action == ActionType.WITHDRAW:
            current = self.positions.get(proto_key, Decimal("0"))
            new_val = current - record.amount_usd
            if new_val <= 0:
                self.positions.pop(proto_key, None)
            else:
                self.positions[proto_key] = new_val

        # Track gas costs
        gas = record.gas_cost_usd if record.gas_cost_usd else record.simulated_gas_usd
        self.total_gas_spent_usd += gas

    def accrue_yield(self, protocol: str, apy_pct: Decimal, hours: Decimal) -> Decimal:
        """Accrue yield for a position over a time period.

        Returns the yield amount accrued.
        """
        position = self.positions.get(protocol, Decimal("0"))
        if position <= 0 or apy_pct <= 0 or hours <= 0:
            return Decimal("0")

        # yield = position * (apy / 100) * (hours / 8760)
        hours_per_year = Decimal("8760")
        yield_amount = position * (apy_pct / Decimal("100")) * (hours / hours_per_year)
        self.unrealized_yield_usd += yield_amount

        return yield_amount

    def get_position(self, protocol: str) -> Decimal:
        """Get current position size for a protocol."""
        return self.positions.get(protocol, Decimal("0"))

    async def reconcile_with_chain(
        self,
        adapters: dict[str, ProtocolAdapter],
        wallet: str,
    ) -> dict[str, dict]:
        """Compare DB positions against actual on-chain balances.

        Reads each protocol's on-chain balance and corrects the DB if
        drift exceeds $0.01. Returns a drift report per protocol.
        """
        drift_report: dict[str, dict] = {}
        corrected = False

        for proto_key, adapter in adapters.items():
            db_amount = self.positions.get(proto_key, Decimal("0"))
            try:
                onchain_amount = await adapter.get_balance(wallet)
            except Exception as e:
                logger.warning(f"Reconcile: failed to read {proto_key} balance: {e}")
                drift_report[proto_key] = {
                    "db": float(db_amount),
                    "onchain": None,
                    "drift": None,
                    "error": str(e),
                }
                continue

            drift = onchain_amount - db_amount
            drift_report[proto_key] = {
                "db": float(db_amount),
                "onchain": float(onchain_amount),
                "drift": float(drift),
            }

            # Auto-correct if drift exceeds dust threshold
            if abs(drift) > Decimal("0.01"):
                logger.warning(
                    f"Reconcile: {proto_key} drift ${drift:+.2f} "
                    f"(DB=${db_amount:.2f}, on-chain=${onchain_amount:.2f}) — correcting"
                )
                if onchain_amount > Decimal("0"):
                    self.positions[proto_key] = onchain_amount
                else:
                    self.positions.pop(proto_key, None)
                corrected = True
            else:
                logger.info(
                    f"Reconcile: {proto_key} OK "
                    f"(DB=${db_amount:.2f}, on-chain=${onchain_amount:.2f})"
                )

        # Check for protocols in DB that have no adapter (orphaned positions)
        for proto_key in list(self.positions.keys()):
            if proto_key not in adapters:
                logger.warning(
                    f"Reconcile: {proto_key} in DB but no adapter — "
                    f"cannot verify (${self.positions[proto_key]:.2f})"
                )
                drift_report[proto_key] = {
                    "db": float(self.positions[proto_key]),
                    "onchain": None,
                    "drift": None,
                    "error": "no adapter",
                }

        if corrected:
            await self.save_snapshot()
            logger.info("Reconcile: portfolio snapshot saved after corrections")

        return drift_report

    def summary(self) -> dict:
        """Return portfolio summary as a dict."""
        return {
            "total_capital_usd": float(self.total_capital_usd),
            "allocated_usd": float(self.allocated_usd),
            "reserve_usd": float(self.reserve_usd),
            "unrealized_yield_usd": float(self.unrealized_yield_usd),
            "total_gas_spent_usd": float(self.total_gas_spent_usd),
            "net_value_usd": float(self.net_value_usd),
            "positions": {k: float(v) for k, v in self.positions.items()},
        }
