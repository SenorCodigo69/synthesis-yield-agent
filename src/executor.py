"""Execution engine — paper, dry-run, and live modes.

Paper mode: simulates deposit/withdraw, tracks portfolio in SQLite.
Dry-run mode: builds real transactions but doesn't sign/send.
Live mode: full on-chain execution via protocol adapters.

All modes enforce:
- Health checks before execution
- Spending scope constraints
- Withdrawal cooldowns
- Full audit trail logging
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from src.database import Database
from src.models import (
    ActionType,
    Chain,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    GasPrice,
    ProtocolName,
    SpendingScope,
    TxReceipt,
    ValidatedRate,
)
from src.portfolio import Portfolio
from src.protocols.base import ProtocolAdapter
from src.protocols.tx_helpers import TransactionSigner
from src.strategy.allocator import AllocationPlan

logger = logging.getLogger(__name__)

# Gas units per operation (used for paper-mode cost simulation)
GAS_UNITS_APPROVE = 50_000
GAS_UNITS_SUPPLY = 150_000
GAS_UNITS_WITHDRAW = 120_000


class ExecutionError(Exception):
    """Raised when an execution fails."""
    pass


class CooldownError(Exception):
    """Raised when withdrawal cooldown hasn't expired."""
    pass


class HealthCheckError(Exception):
    """Raised when a protocol fails pre-execution health check."""
    pass


class InsufficientReserveError(Exception):
    """Raised when a supply would exceed available reserve capital."""
    pass


class Executor:
    """Executes allocation plans against protocols.

    Supports paper, dry-run, and live modes. All modes produce
    ExecutionRecords logged to the database with full audit trail.
    """

    def __init__(
        self,
        mode: ExecutionMode,
        db: Database,
        portfolio: Portfolio,
        scope: SpendingScope,
        gas_price: GasPrice,
        eth_price_usd: Decimal = Decimal("3500"),
        adapters: dict[ProtocolName, ProtocolAdapter] | None = None,
        signer: TransactionSigner | None = None,
        sender: str | None = None,
    ):
        if mode == ExecutionMode.LIVE and (not adapters or not signer or not sender):
            raise ValueError(
                "Live mode requires adapters, signer, and sender address"
            )
        self.mode = mode
        self.db = db
        self.portfolio = portfolio
        self.scope = scope
        self.gas_price = gas_price
        self.eth_price_usd = eth_price_usd
        self._adapters = adapters or {}
        self._signer = signer
        self._sender = sender

    async def execute_plan(
        self,
        plan: AllocationPlan,
        current_rates: list[ValidatedRate],
    ) -> list[ExecutionRecord]:
        """Execute an allocation plan — deposit/withdraw to match targets.

        Returns list of execution records for all actions taken.
        """
        records: list[ExecutionRecord] = []

        if not plan.allocations:
            logger.info("No allocations in plan — nothing to execute")
            return records

        # Build a rate lookup for health checks
        rate_map = {r.protocol: r for r in current_rates}

        # Calculate deltas: what needs to change from current positions
        actions = self._compute_deltas(plan)

        if not actions:
            logger.info("Portfolio already matches target allocation — no actions needed")
            return records

        # Execute withdrawals first (free up capital before depositing)
        withdrawals = [a for a in actions if a[0] == ActionType.WITHDRAW]
        supplies = [a for a in actions if a[0] == ActionType.SUPPLY]

        for action_type, protocol, chain, amount, reasoning in withdrawals:
            record = await self._execute_single(
                action_type, protocol, chain, amount, reasoning, rate_map,
            )
            records.append(record)

        for action_type, protocol, chain, amount, reasoning in supplies:
            record = await self._execute_single(
                action_type, protocol, chain, amount, reasoning, rate_map,
            )
            records.append(record)

        # Save portfolio snapshot after all executions
        await self.portfolio.save_snapshot()

        # Summary
        successful = sum(1 for r in records if r.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED))
        failed = sum(1 for r in records if r.status == ExecutionStatus.FAILED)
        skipped = sum(1 for r in records if r.status == ExecutionStatus.SKIPPED)
        logger.info(
            f"Execution complete: {successful} success, {failed} failed, "
            f"{skipped} skipped (mode={self.mode.value})"
        )

        return records

    def _compute_deltas(
        self, plan: AllocationPlan,
    ) -> list[tuple[ActionType, ProtocolName, Chain, Decimal, str]]:
        """Compute the list of supply/withdraw actions needed to match the plan.

        Returns list of (action, protocol, chain, amount_usd, reasoning).
        """
        actions = []
        min_move = Decimal("1")  # Don't bother with < $1 moves

        # Current positions
        target_by_proto: dict[str, tuple[Decimal, Chain]] = {}
        for alloc in plan.allocations:
            target_by_proto[alloc.protocol.value] = (alloc.amount_usd, alloc.chain)

        # Withdrawals: positions we have but shouldn't, or need to reduce
        for proto_key, current_amount in self.portfolio.positions.items():
            if proto_key not in target_by_proto:
                # Full withdrawal — protocol no longer in plan
                if current_amount > min_move:
                    try:
                        protocol = ProtocolName(proto_key)
                    except ValueError:
                        continue
                    actions.append((
                        ActionType.WITHDRAW, protocol, Chain.BASE,
                        current_amount,
                        f"Full withdrawal: {proto_key} no longer in allocation plan",
                    ))
            else:
                target_amount, chain = target_by_proto[proto_key]
                delta = current_amount - target_amount
                if delta > min_move:
                    try:
                        protocol = ProtocolName(proto_key)
                    except ValueError:
                        continue
                    actions.append((
                        ActionType.WITHDRAW, protocol, chain,
                        delta,
                        f"Reduce position: {proto_key} from ${current_amount:,.2f} to ${target_amount:,.2f}",
                    ))

        # Supplies: new positions or increases
        for alloc in plan.allocations:
            current = self.portfolio.get_position(alloc.protocol.value)
            delta = alloc.amount_usd - current
            if delta > min_move:
                actions.append((
                    ActionType.SUPPLY, alloc.protocol, alloc.chain,
                    delta,
                    f"{'New position' if current == 0 else 'Increase position'}: "
                    f"{alloc.protocol.value} to ${alloc.amount_usd:,.2f} "
                    f"(+${delta:,.2f})",
                ))

        return actions

    async def _execute_single(
        self,
        action: ActionType,
        protocol: ProtocolName,
        chain: Chain,
        amount: Decimal,
        reasoning: str,
        rate_map: dict[ProtocolName, ValidatedRate],
    ) -> ExecutionRecord:
        """Execute a single supply or withdraw action."""
        record = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=action,
            protocol=protocol,
            chain=chain,
            amount_usd=amount,
            mode=self.mode,
            reasoning=reasoning,
        )

        try:
            # Pre-execution checks
            await self._pre_execution_checks(action, protocol, amount, rate_map)

            if self.mode == ExecutionMode.PAPER:
                await self._execute_paper(record)
            elif self.mode == ExecutionMode.DRY_RUN:
                await self._execute_dry_run(record)
            elif self.mode == ExecutionMode.LIVE:
                await self._execute_live(record)

        except (CooldownError, HealthCheckError, InsufficientReserveError) as e:
            record.status = ExecutionStatus.SKIPPED
            record.error = str(e)
            logger.warning(f"Execution skipped: {e}")
        except Exception as e:
            record.status = ExecutionStatus.FAILED
            record.error = str(e)
            logger.error(f"Execution failed: {e}")

        # Always log to database
        await self.db.insert_execution(record)
        return record

    async def _pre_execution_checks(
        self,
        action: ActionType,
        protocol: ProtocolName,
        amount: Decimal,
        rate_map: dict[ProtocolName, ValidatedRate],
    ) -> None:
        """Run pre-execution safety checks."""
        # Check rate validity
        rate = rate_map.get(protocol)
        if rate and not rate.is_valid:
            raise HealthCheckError(
                f"{protocol.value}: rate cross-validation failed — blocking execution"
            )

        # Check utilization for supply actions
        if rate and action == ActionType.SUPPLY:
            if rate.utilization > self.scope.max_utilization:
                raise HealthCheckError(
                    f"{protocol.value}: utilization {rate.utilization:.1%} "
                    f"above {self.scope.max_utilization:.1%} — blocking supply"
                )

        # Check withdrawal cooldown (only for withdrawals — supplies are free to execute)
        if action == ActionType.WITHDRAW:
            last_exec = await self.db.get_last_execution_time(protocol.value)
            if last_exec:
                cooldown = timedelta(seconds=self.scope.withdrawal_cooldown_secs)
                time_since = datetime.now(tz=timezone.utc) - last_exec
                if time_since < cooldown:
                    remaining = cooldown - time_since
                    raise CooldownError(
                        f"{protocol.value}: cooldown active, {remaining.seconds}s remaining"
                    )

        # Over-allocation guard: supply cannot exceed available reserve
        if action == ActionType.SUPPLY:
            if amount > self.portfolio.reserve_usd:
                raise InsufficientReserveError(
                    f"{protocol.value}: supply ${amount:,.2f} exceeds "
                    f"reserve ${self.portfolio.reserve_usd:,.2f}"
                )

    async def _execute_paper(self, record: ExecutionRecord) -> None:
        """Paper-mode execution — simulate the trade."""
        # Calculate simulated gas cost
        if record.action == ActionType.SUPPLY:
            gas_units = GAS_UNITS_APPROVE + GAS_UNITS_SUPPLY
        else:
            gas_units = GAS_UNITS_WITHDRAW

        gas_cost = self._estimate_gas_usd(gas_units)
        record.simulated_gas_usd = gas_cost

        # Generate a fake tx hash for tracking
        record.tx_hash = f"paper-{record.id[:8]}"
        record.block_number = 0

        # Update portfolio
        self.portfolio.apply_execution(record)
        record.status = ExecutionStatus.SUCCESS

        logger.info(
            f"[PAPER] {record.action.value} {record.protocol.value}: "
            f"${record.amount_usd:,.2f} | gas: ${gas_cost:.4f} | "
            f"{record.reasoning}"
        )

    async def _execute_dry_run(self, record: ExecutionRecord) -> None:
        """Dry-run mode — log what would happen, don't execute."""
        if record.action == ActionType.SUPPLY:
            gas_units = GAS_UNITS_APPROVE + GAS_UNITS_SUPPLY
        else:
            gas_units = GAS_UNITS_WITHDRAW

        gas_cost = self._estimate_gas_usd(gas_units)
        record.simulated_gas_usd = gas_cost
        record.tx_hash = f"dryrun-{record.id[:8]}"
        record.block_number = 0
        record.status = ExecutionStatus.SIMULATED

        logger.info(
            f"[DRY-RUN] Would {record.action.value} {record.protocol.value}: "
            f"${record.amount_usd:,.2f} | est gas: ${gas_cost:.4f} | "
            f"{record.reasoning}"
        )

    async def _execute_live(self, record: ExecutionRecord) -> None:
        """Live on-chain execution via protocol adapters."""
        adapter = self._adapters.get(record.protocol)
        if not adapter:
            raise ExecutionError(
                f"No adapter for {record.protocol.value} — cannot execute live"
            )

        assert self._signer is not None
        assert self._sender is not None

        if record.action == ActionType.SUPPLY:
            # Approve first, then supply
            logger.info(
                f"[LIVE] Approving {record.protocol.value}: "
                f"${record.amount_usd:,.2f} USDC"
            )
            await adapter.approve(record.amount_usd, self._sender, self._signer)

            logger.info(
                f"[LIVE] Supplying {record.protocol.value}: "
                f"${record.amount_usd:,.2f} USDC"
            )
            tx_receipt: TxReceipt = await adapter.supply(
                record.amount_usd, self._sender, self._signer,
            )
        elif record.action == ActionType.WITHDRAW:
            logger.info(
                f"[LIVE] Withdrawing {record.protocol.value}: "
                f"${record.amount_usd:,.2f} USDC"
            )
            tx_receipt = await adapter.withdraw(
                record.amount_usd, self._sender, self._signer,
            )
        else:
            raise ExecutionError(f"Unsupported action for live mode: {record.action}")

        record.tx_hash = tx_receipt.tx_hash
        record.block_number = tx_receipt.block_number

        # Update portfolio to match on-chain state
        self.portfolio.apply_execution(record)
        record.status = ExecutionStatus.SUCCESS

        logger.info(
            f"[LIVE] {record.action.value} {record.protocol.value}: "
            f"${record.amount_usd:,.2f} | tx: {record.tx_hash} | "
            f"block: {record.block_number}"
        )

    def _estimate_gas_usd(self, gas_units: int) -> Decimal:
        """Estimate gas cost in USD for paper/dry-run modes."""
        gas_gwei = self.gas_price.total_gwei
        gas_eth = Decimal(gas_units) * gas_gwei / Decimal("1_000_000_000")
        return gas_eth * self.eth_price_usd
