"""Tests for the execution engine, portfolio tracker, and database."""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.database import Database
from src.executor import (
    Executor,
    CooldownError,
    HealthCheckError,
    InsufficientReserveError,
    GAS_UNITS_APPROVE,
    GAS_UNITS_SUPPLY,
    GAS_UNITS_WITHDRAW,
)
from src.models import (
    ActionType,
    Allocation,
    Chain,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    GasPrice,
    PortfolioSnapshot,
    ProtocolName,
    SpendingScope,
    ValidatedRate,
    DataSource,
)
from src.portfolio import Portfolio
from src.protocols.tx_helpers import TransactionSigner
from src.strategy.allocator import AllocationPlan, ScoredProtocol, compute_allocations
from src.strategy.net_apy import NetAPY
from src.strategy.risk_scorer import RiskScore


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a temporary test database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def portfolio(db):
    """Create a test portfolio."""
    return Portfolio(Decimal("10000"), db)


@pytest.fixture
def gas():
    return GasPrice(
        base_fee_gwei=Decimal("0.008"),
        priority_fee_gwei=Decimal("0.001"),
        source="test",
    )


@pytest.fixture
def scope():
    return SpendingScope()


@pytest.fixture
def sample_rates():
    """Sample validated rates for testing."""
    return [
        ValidatedRate(
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            apy_median=Decimal("3.50"),
            apy_sources={DataSource.DEFILLAMA: Decimal("3.50")},
            tvl_usd=Decimal("200000000"),
            utilization=Decimal("0.65"),
            is_valid=True,
        ),
        ValidatedRate(
            protocol=ProtocolName.MORPHO,
            chain=Chain.BASE,
            apy_median=Decimal("4.20"),
            apy_sources={DataSource.DEFILLAMA: Decimal("4.20")},
            tvl_usd=Decimal("400000000"),
            utilization=Decimal("0.55"),
            is_valid=True,
        ),
    ]


@pytest.fixture
def sample_plan(sample_rates, gas, scope):
    """A pre-computed allocation plan."""
    return compute_allocations(
        rates=sample_rates,
        gas_price=gas,
        total_capital_usd=Decimal("10000"),
        scope=scope,
    )


# ── Database tests ────────────────────────────────────────────────────────

class TestDatabase:

    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, db):
        """Database connects and creates schema."""
        cursor = await db._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        assert "execution_log" in tables
        assert "portfolio_snapshots" in tables

    @pytest.mark.asyncio
    async def test_insert_and_read_execution(self, db):
        """Can insert and retrieve execution records."""
        record = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("5000"),
            mode=ExecutionMode.PAPER,
            tx_hash="paper-test123",
            block_number=0,
            status=ExecutionStatus.SUCCESS,
            reasoning="Test supply",
        )
        await db.insert_execution(record)

        records = await db.get_executions(limit=10)
        assert len(records) == 1
        assert records[0]["protocol"] == "aave-v3"
        assert records[0]["amount_usd"] == "5000"
        assert records[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_update_execution_status(self, db):
        """Can update an execution record's status."""
        rec_id = str(uuid.uuid4())
        record = ExecutionRecord(
            id=rec_id,
            action=ActionType.SUPPLY,
            protocol=ProtocolName.MORPHO,
            chain=Chain.BASE,
            amount_usd=Decimal("3000"),
            mode=ExecutionMode.PAPER,
            status=ExecutionStatus.PENDING,
        )
        await db.insert_execution(record)
        await db.update_execution_status(rec_id, "success", tx_hash="0xabc")

        records = await db.get_executions()
        assert records[0]["status"] == "success"
        assert records[0]["tx_hash"] == "0xabc"

    @pytest.mark.asyncio
    async def test_insert_and_read_snapshot(self, db):
        """Can insert and retrieve portfolio snapshots."""
        snap = PortfolioSnapshot(
            total_capital_usd=Decimal("10000"),
            allocated_usd=Decimal("6400"),
            reserve_usd=Decimal("3600"),
            unrealized_yield_usd=Decimal("1.25"),
            total_gas_spent_usd=Decimal("0.01"),
            positions={"aave-v3": Decimal("3500"), "morpho-v1": Decimal("2900")},
        )
        await db.insert_snapshot(snap)

        latest = await db.get_latest_snapshot()
        assert latest is not None
        assert latest.total_capital_usd == Decimal("10000")
        assert latest.positions["aave-v3"] == Decimal("3500")
        assert latest.positions["morpho-v1"] == Decimal("2900")
        assert latest.unrealized_yield_usd == Decimal("1.25")

    @pytest.mark.asyncio
    async def test_get_snapshots_ordering(self, db):
        """Snapshots returned in reverse chronological order."""
        for i in range(5):
            snap = PortfolioSnapshot(
                total_capital_usd=Decimal("10000"),
                allocated_usd=Decimal(str(i * 1000)),
                reserve_usd=Decimal(str(10000 - i * 1000)),
                unrealized_yield_usd=Decimal("0"),
                total_gas_spent_usd=Decimal("0"),
                positions={},
            )
            await db.insert_snapshot(snap)

        snapshots = await db.get_snapshots(limit=3)
        assert len(snapshots) == 3
        # Latest first (highest allocated)
        assert snapshots[0].allocated_usd == Decimal("4000")

    @pytest.mark.asyncio
    async def test_last_execution_time(self, db):
        """Can get last successful execution time for cooldown."""
        record = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("5000"),
            mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        await db.insert_execution(record)

        last_time = await db.get_last_execution_time("aave-v3")
        assert last_time is not None
        assert (datetime.now(tz=timezone.utc) - last_time).total_seconds() < 5

        # No executions for morpho
        no_time = await db.get_last_execution_time("morpho-v1")
        assert no_time is None

    @pytest.mark.asyncio
    async def test_total_gas_spent(self, db):
        """Correctly sums gas across executions."""
        for i in range(3):
            record = ExecutionRecord(
                id=str(uuid.uuid4()),
                action=ActionType.SUPPLY,
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                amount_usd=Decimal("1000"),
                mode=ExecutionMode.PAPER,
                simulated_gas_usd=Decimal("0.005"),
                status=ExecutionStatus.SUCCESS,
            )
            await db.insert_execution(record)

        total = await db.get_total_gas_spent()
        assert total == pytest.approx(Decimal("0.015"), abs=Decimal("0.001"))

    @pytest.mark.asyncio
    async def test_execution_count(self, db):
        """Counts executions by status."""
        for status in [ExecutionStatus.SUCCESS, ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.SKIPPED]:
            record = ExecutionRecord(
                id=str(uuid.uuid4()),
                action=ActionType.SUPPLY,
                protocol=ProtocolName.AAVE_V3,
                chain=Chain.BASE,
                amount_usd=Decimal("1000"),
                mode=ExecutionMode.PAPER,
                status=status,
            )
            await db.insert_execution(record)

        counts = await db.get_execution_count()
        assert counts["success"] == 2
        assert counts["failed"] == 1
        assert counts["skipped"] == 1

    @pytest.mark.asyncio
    async def test_filter_executions_by_protocol(self, db):
        """Can filter execution history by protocol."""
        for proto in [ProtocolName.AAVE_V3, ProtocolName.MORPHO, ProtocolName.AAVE_V3]:
            record = ExecutionRecord(
                id=str(uuid.uuid4()),
                action=ActionType.SUPPLY,
                protocol=proto,
                chain=Chain.BASE,
                amount_usd=Decimal("1000"),
                mode=ExecutionMode.PAPER,
                status=ExecutionStatus.SUCCESS,
            )
            await db.insert_execution(record)

        aave_records = await db.get_executions(protocol="aave-v3")
        assert len(aave_records) == 2


# ── Portfolio tests ───────────────────────────────────────────────────────

class TestPortfolio:

    def test_initial_state(self, portfolio):
        """Portfolio starts empty."""
        assert portfolio.allocated_usd == Decimal("0")
        assert portfolio.reserve_usd == Decimal("10000")
        assert portfolio.positions == {}

    def test_apply_supply(self, portfolio):
        """Supply increases position and allocated amount."""
        record = ExecutionRecord(
            id="test-1",
            action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("5000"),
            mode=ExecutionMode.PAPER,
            simulated_gas_usd=Decimal("0.01"),
            status=ExecutionStatus.SUCCESS,
        )
        portfolio.apply_execution(record)

        assert portfolio.positions["aave-v3"] == Decimal("5000")
        assert portfolio.allocated_usd == Decimal("5000")
        assert portfolio.reserve_usd == Decimal("5000")
        assert portfolio.total_gas_spent_usd == Decimal("0.01")

    def test_apply_withdraw(self, portfolio):
        """Withdraw decreases position."""
        # First supply
        supply = ExecutionRecord(
            id="test-1", action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
            amount_usd=Decimal("5000"), mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        portfolio.apply_execution(supply)

        # Then partial withdraw
        withdraw = ExecutionRecord(
            id="test-2", action=ActionType.WITHDRAW,
            protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
            amount_usd=Decimal("2000"), mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        portfolio.apply_execution(withdraw)

        assert portfolio.positions["aave-v3"] == Decimal("3000")
        assert portfolio.allocated_usd == Decimal("3000")

    def test_full_withdraw_removes_position(self, portfolio):
        """Full withdrawal removes the position entry."""
        supply = ExecutionRecord(
            id="test-1", action=ActionType.SUPPLY,
            protocol=ProtocolName.MORPHO, chain=Chain.BASE,
            amount_usd=Decimal("3000"), mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        portfolio.apply_execution(supply)

        withdraw = ExecutionRecord(
            id="test-2", action=ActionType.WITHDRAW,
            protocol=ProtocolName.MORPHO, chain=Chain.BASE,
            amount_usd=Decimal("3000"), mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        portfolio.apply_execution(withdraw)

        assert "morpho-v1" not in portfolio.positions
        assert portfolio.allocated_usd == Decimal("0")

    def test_accrue_yield(self, portfolio):
        """Yield accrual calculates correctly."""
        # Set up a position
        portfolio.positions["aave-v3"] = Decimal("10000")

        # Accrue 1 hour at 3.65% APY (easy math: 3.65% / 8760h = ~0.000417% per hour)
        yield_amount = portfolio.accrue_yield("aave-v3", Decimal("3.65"), Decimal("1"))

        # Expected: 10000 * 0.0365 * (1/8760) ≈ 0.04166
        assert yield_amount > Decimal("0.04")
        assert yield_amount < Decimal("0.05")
        assert portfolio.unrealized_yield_usd == yield_amount

    def test_accrue_yield_no_position(self, portfolio):
        """No yield if no position exists."""
        y = portfolio.accrue_yield("aave-v3", Decimal("5.0"), Decimal("24"))
        assert y == Decimal("0")

    def test_multiple_positions(self, portfolio):
        """Handles multiple protocol positions correctly."""
        for proto in [ProtocolName.AAVE_V3, ProtocolName.MORPHO]:
            record = ExecutionRecord(
                id=f"test-{proto.value}",
                action=ActionType.SUPPLY,
                protocol=proto, chain=Chain.BASE,
                amount_usd=Decimal("3000"), mode=ExecutionMode.PAPER,
                status=ExecutionStatus.SUCCESS,
            )
            portfolio.apply_execution(record)

        assert portfolio.allocated_usd == Decimal("6000")
        assert portfolio.reserve_usd == Decimal("4000")
        assert len(portfolio.positions) == 2

    @pytest.mark.asyncio
    async def test_save_and_load(self, portfolio, db):
        """Portfolio state survives save/load cycle."""
        portfolio.positions["aave-v3"] = Decimal("5000")
        portfolio.positions["morpho-v1"] = Decimal("3000")
        portfolio.unrealized_yield_usd = Decimal("12.50")
        portfolio.total_gas_spent_usd = Decimal("0.05")

        await portfolio.save_snapshot()

        # Load into fresh portfolio
        new_portfolio = Portfolio(Decimal("10000"), db)
        loaded = await new_portfolio.load_from_db()

        assert loaded is True
        assert new_portfolio.positions["aave-v3"] == Decimal("5000")
        assert new_portfolio.positions["morpho-v1"] == Decimal("3000")
        assert new_portfolio.unrealized_yield_usd == Decimal("12.50")
        assert new_portfolio.total_gas_spent_usd == Decimal("0.05")

    def test_net_value(self, portfolio):
        """Net value accounts for yield and gas."""
        portfolio.unrealized_yield_usd = Decimal("50")
        portfolio.total_gas_spent_usd = Decimal("0.10")

        assert portfolio.net_value_usd == Decimal("10000") + Decimal("50") - Decimal("0.10")

    def test_summary_dict(self, portfolio):
        """Summary returns correct dict format."""
        portfolio.positions["aave-v3"] = Decimal("5000")
        summary = portfolio.summary()

        assert summary["total_capital_usd"] == 10000.0
        assert summary["allocated_usd"] == 5000.0
        assert summary["reserve_usd"] == 5000.0
        assert "aave-v3" in summary["positions"]


# ── Executor tests ────────────────────────────────────────────────────────

class TestExecutor:

    @pytest.mark.asyncio
    async def test_paper_execution_basic(self, db, gas, scope, sample_rates, sample_plan):
        """Paper execution creates records and updates portfolio."""
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )

        records = await executor.execute_plan(sample_plan, sample_rates)

        # Should have supply records
        assert len(records) > 0
        assert all(r.status == ExecutionStatus.SUCCESS for r in records)
        assert all(r.mode == ExecutionMode.PAPER for r in records)

        # Portfolio should have positions
        assert portfolio.allocated_usd > 0
        assert portfolio.reserve_usd > 0

    @pytest.mark.asyncio
    async def test_dry_run_no_portfolio_change(self, db, gas, scope, sample_rates, sample_plan):
        """Dry-run doesn't modify portfolio state."""
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.DRY_RUN, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )

        records = await executor.execute_plan(sample_plan, sample_rates)

        assert len(records) > 0
        assert all(r.status == ExecutionStatus.SIMULATED for r in records)
        # Dry-run should NOT update portfolio positions
        assert portfolio.allocated_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_no_action_when_portfolio_matches(self, db, gas, scope, sample_rates, sample_plan):
        """No execution when portfolio already matches plan."""
        portfolio = Portfolio(Decimal("10000"), db)

        # Execute once to set positions
        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        first_records = await executor.execute_plan(sample_plan, sample_rates)
        assert len(first_records) > 0

        # Execute again with same plan — should produce no actions
        # Need to bypass cooldown for this test
        scope_no_cooldown = SpendingScope(withdrawal_cooldown_secs=0)
        executor2 = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope_no_cooldown, gas_price=gas,
        )
        second_records = await executor2.execute_plan(sample_plan, sample_rates)
        assert len(second_records) == 0

    @pytest.mark.asyncio
    async def test_paper_tx_hash_format(self, db, gas, scope, sample_rates, sample_plan):
        """Paper mode generates recognizable tx hashes."""
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(sample_plan, sample_rates)

        for r in records:
            assert r.tx_hash.startswith("paper-")

    @pytest.mark.asyncio
    async def test_gas_estimation_paper(self, db, scope, sample_rates, sample_plan):
        """Paper mode estimates gas costs correctly."""
        gas = GasPrice(
            base_fee_gwei=Decimal("10"),
            priority_fee_gwei=Decimal("2"),
            source="test",
        )
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas, eth_price_usd=Decimal("3000"),
        )
        records = await executor.execute_plan(sample_plan, sample_rates)

        for r in records:
            assert r.simulated_gas_usd > 0
            # Supply = approve (50k) + supply (150k) = 200k gas
            # 200k * 12 gwei / 1e9 * $3000 = $0.0072
            if r.action == ActionType.SUPPLY:
                expected_gas_units = GAS_UNITS_APPROVE + GAS_UNITS_SUPPLY
                expected_cost = Decimal(expected_gas_units) * Decimal("12") / Decimal("1e9") * Decimal("3000")
                assert abs(r.simulated_gas_usd - expected_cost) < Decimal("0.001")

    @pytest.mark.asyncio
    async def test_invalid_rate_blocks_execution(self, db, gas, scope):
        """Invalid rate (cross-validation failed) blocks execution."""
        rates = [
            ValidatedRate(
                protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                apy_median=Decimal("3.50"),
                tvl_usd=Decimal("200000000"), utilization=Decimal("0.65"),
                is_valid=False,  # Blocked
            ),
        ]
        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("5000"), target_pct=Decimal("0.50"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("5000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("5000"),
        )

        portfolio = Portfolio(Decimal("10000"), db)
        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, rates)

        assert len(records) == 1
        assert records[0].status == ExecutionStatus.SKIPPED
        assert "cross-validation" in records[0].error

    @pytest.mark.asyncio
    async def test_withdrawal_cooldown_enforcement(self, db, gas, scope, sample_rates):
        """Withdrawal cooldown prevents rapid re-withdrawal."""
        portfolio = Portfolio(Decimal("10000"), db)
        # Set existing position
        portfolio.positions["aave-v3"] = Decimal("8000")

        # Create a recent successful execution for Aave
        recent = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=ActionType.WITHDRAW,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("2000"),
            mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        await db.insert_execution(recent)

        # Plan that requires withdrawal from Aave (reduce position)
        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("3000"), target_pct=Decimal("0.30"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("3000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("7000"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, sample_rates)

        skipped = [r for r in records if r.status == ExecutionStatus.SKIPPED]
        assert len(skipped) > 0
        assert "cooldown" in skipped[0].error

    @pytest.mark.asyncio
    async def test_supply_not_blocked_by_cooldown(self, db, gas, scope, sample_rates, sample_plan):
        """Supply is NOT blocked by cooldown (only withdrawals are)."""
        portfolio = Portfolio(Decimal("10000"), db)

        # Create a recent successful supply
        recent = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("3000"),
            mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        await db.insert_execution(recent)

        # Execute should succeed — supplies are not cooldown-gated
        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(sample_plan, sample_rates)

        successful = [r for r in records if r.status == ExecutionStatus.SUCCESS]
        assert len(successful) > 0

    @pytest.mark.asyncio
    async def test_withdrawals_before_supplies(self, db, gas, sample_rates):
        """Withdrawals execute before supplies to free capital."""
        scope = SpendingScope(withdrawal_cooldown_secs=0)
        portfolio = Portfolio(Decimal("10000"), db)

        # Set up initial position in Aave
        portfolio.positions["aave-v3"] = Decimal("8000")

        # Plan: reduce Aave, add Morpho
        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("4000"), target_pct=Decimal("0.40"),
                    actual_pct=Decimal("0"),
                ),
                Allocation(
                    protocol=ProtocolName.MORPHO, chain=Chain.BASE,
                    amount_usd=Decimal("4000"), target_pct=Decimal("0.40"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("8000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("2000"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, sample_rates)

        # Should have 2 records: withdraw from Aave, supply to Morpho
        assert len(records) == 2
        assert records[0].action == ActionType.WITHDRAW
        assert records[0].protocol == ProtocolName.AAVE_V3
        assert records[1].action == ActionType.SUPPLY
        assert records[1].protocol == ProtocolName.MORPHO

    @pytest.mark.asyncio
    async def test_live_execution_calls_adapter(self, db, gas, scope, sample_rates, sample_plan):
        """Live mode calls protocol adapter supply/approve and records real tx hash."""
        portfolio = Portfolio(Decimal("10000"), db)

        mock_receipt = MagicMock()
        mock_receipt.tx_hash = "0xabc123"
        mock_receipt.block_number = 99999

        mock_adapter = AsyncMock()
        mock_adapter.supply.return_value = mock_receipt
        mock_adapter.approve.return_value = mock_receipt

        adapters = {ProtocolName.AAVE_V3: mock_adapter, ProtocolName.MORPHO: mock_adapter}
        signer = MagicMock(spec=TransactionSigner)
        signer.key = "0x" + "ab" * 32

        executor = Executor(
            mode=ExecutionMode.LIVE, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
            adapters=adapters, signer=signer, sender="0x1234",
        )
        records = await executor.execute_plan(sample_plan, sample_rates)

        assert len(records) > 0
        assert all(r.status == ExecutionStatus.SUCCESS for r in records)
        assert all(r.tx_hash == "0xabc123" for r in records)
        assert portfolio.allocated_usd > 0
        # Verify adapters were called
        assert mock_adapter.approve.call_count > 0
        assert mock_adapter.supply.call_count > 0

    @pytest.mark.asyncio
    async def test_live_requires_adapters(self, db, gas, scope):
        """Live mode raises if adapters/signer/sender not provided."""
        portfolio = Portfolio(Decimal("10000"), db)
        with pytest.raises(ValueError, match="Live mode requires"):
            Executor(
                mode=ExecutionMode.LIVE, db=db, portfolio=portfolio,
                scope=scope, gas_price=gas,
            )

    @pytest.mark.asyncio
    async def test_live_withdraw_calls_adapter(self, db, gas, sample_rates):
        """Live mode calls adapter.withdraw for position reductions."""
        scope = SpendingScope(withdrawal_cooldown_secs=0)
        portfolio = Portfolio(Decimal("10000"), db)
        portfolio.positions["aave-v3"] = Decimal("8000")

        mock_receipt = MagicMock()
        mock_receipt.tx_hash = "0xwithdraw"
        mock_receipt.block_number = 100000

        mock_adapter = AsyncMock()
        mock_adapter.withdraw.return_value = mock_receipt
        mock_adapter.supply.return_value = mock_receipt
        mock_adapter.approve.return_value = mock_receipt

        adapters = {ProtocolName.AAVE_V3: mock_adapter, ProtocolName.MORPHO: mock_adapter}
        signer = MagicMock(spec=TransactionSigner)
        signer.key = "0x" + "ab" * 32

        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("4000"), target_pct=Decimal("0.40"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("4000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("6000"),
        )

        executor = Executor(
            mode=ExecutionMode.LIVE, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
            adapters=adapters, signer=signer, sender="0x1234",
        )
        records = await executor.execute_plan(plan, sample_rates)

        withdrawals = [r for r in records if r.action == ActionType.WITHDRAW]
        assert len(withdrawals) == 1
        assert withdrawals[0].tx_hash == "0xwithdraw"
        assert mock_adapter.withdraw.call_count == 1

    @pytest.mark.asyncio
    async def test_execution_persisted_to_db(self, db, gas, scope, sample_rates, sample_plan):
        """Execution records are saved to database."""
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(sample_plan, sample_rates)

        # Check database
        db_records = await db.get_executions()
        assert len(db_records) == len(records)
        assert all(r["mode"] == "paper" for r in db_records)

    @pytest.mark.asyncio
    async def test_portfolio_snapshot_saved(self, db, gas, scope, sample_rates, sample_plan):
        """Portfolio snapshot saved after execution."""
        portfolio = Portfolio(Decimal("10000"), db)

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        await executor.execute_plan(sample_plan, sample_rates)

        snapshot = await db.get_latest_snapshot()
        assert snapshot is not None
        assert snapshot.allocated_usd > 0

    @pytest.mark.asyncio
    async def test_empty_plan_no_action(self, db, gas, scope, sample_rates):
        """Empty allocation plan produces no actions."""
        portfolio = Portfolio(Decimal("10000"), db)

        empty_plan = AllocationPlan(
            allocations=[],
            scored_protocols=[],
            total_allocated_usd=Decimal("0"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("10000"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(empty_plan, sample_rates)
        assert len(records) == 0


# ── Delta computation tests ──────────────────────────────────────────────

class TestDeltaComputation:

    @pytest.mark.asyncio
    async def test_new_positions(self, db, gas, scope, sample_rates, sample_plan):
        """Computes supply actions for new positions."""
        portfolio = Portfolio(Decimal("10000"), db)
        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )

        deltas = executor._compute_deltas(sample_plan)

        # All should be supplies (no existing positions)
        assert len(deltas) > 0
        assert all(d[0] == ActionType.SUPPLY for d in deltas)

    @pytest.mark.asyncio
    async def test_full_withdrawal_when_removed(self, db, gas, scope, sample_rates):
        """Computes full withdrawal when protocol removed from plan."""
        portfolio = Portfolio(Decimal("10000"), db)
        portfolio.positions["compound-v3"] = Decimal("3000")

        # Plan doesn't include compound
        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("5000"), target_pct=Decimal("0.50"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("5000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("5000"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )

        deltas = executor._compute_deltas(plan)

        withdrawals = [d for d in deltas if d[0] == ActionType.WITHDRAW]
        assert len(withdrawals) == 1
        assert withdrawals[0][1] == ProtocolName.COMPOUND_V3
        assert withdrawals[0][3] == Decimal("3000")

    @pytest.mark.asyncio
    async def test_min_move_threshold(self, db, gas, scope, sample_rates):
        """Ignores tiny deltas below $1."""
        portfolio = Portfolio(Decimal("10000"), db)
        portfolio.positions["aave-v3"] = Decimal("4999.50")

        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("5000"), target_pct=Decimal("0.50"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("5000"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("5000"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        deltas = executor._compute_deltas(plan)

        # Delta of $0.50 should be ignored
        assert len(deltas) == 0


# ── Model tests ──────────────────────────────────────────────────────────

class TestNewModels:

    def test_execution_mode_values(self):
        assert ExecutionMode.PAPER.value == "paper"
        assert ExecutionMode.DRY_RUN.value == "dry_run"
        assert ExecutionMode.LIVE.value == "live"

    def test_execution_record_defaults(self):
        record = ExecutionRecord(
            id="test",
            action=ActionType.SUPPLY,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("1000"),
            mode=ExecutionMode.PAPER,
        )
        assert record.status == ExecutionStatus.PENDING
        assert record.tx_hash is None
        assert record.gas_cost_usd == Decimal("0")
        assert record.error is None

    def test_portfolio_snapshot_net_value(self):
        snap = PortfolioSnapshot(
            total_capital_usd=Decimal("10000"),
            allocated_usd=Decimal("6000"),
            reserve_usd=Decimal("4000"),
            unrealized_yield_usd=Decimal("50"),
            total_gas_spent_usd=Decimal("0.50"),
            positions={"aave-v3": Decimal("6000")},
        )
        assert snap.net_value_usd == Decimal("10049.50")

    def test_execution_status_enum_values(self):
        assert ExecutionStatus.PENDING.value == "pending"
        assert ExecutionStatus.SUCCESS.value == "success"
        assert ExecutionStatus.FAILED.value == "failed"
        assert ExecutionStatus.SKIPPED.value == "skipped"
        assert ExecutionStatus.SIMULATED.value == "simulated"


# ── Security fix tests ───────────────────────────────────────────────────

class TestSecurityFixes:

    @pytest.mark.asyncio
    async def test_over_allocation_guard(self, db, gas, scope, sample_rates):
        """Supply cannot exceed available reserve capital (CRIT-02 fix)."""
        portfolio = Portfolio(Decimal("10000"), db)
        # Already allocated most of capital
        portfolio.positions["aave-v3"] = Decimal("9500")

        plan = AllocationPlan(
            allocations=[
                Allocation(
                    protocol=ProtocolName.AAVE_V3, chain=Chain.BASE,
                    amount_usd=Decimal("9500"), target_pct=Decimal("0.50"),
                    actual_pct=Decimal("0"),
                ),
                Allocation(
                    protocol=ProtocolName.MORPHO, chain=Chain.BASE,
                    amount_usd=Decimal("2000"), target_pct=Decimal("0.50"),
                    actual_pct=Decimal("0"),
                ),
            ],
            scored_protocols=[],
            total_allocated_usd=Decimal("11500"),
            total_capital_usd=Decimal("10000"),
            reserve_usd=Decimal("-1500"),
        )

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, sample_rates)

        # Morpho supply should be skipped due to insufficient reserve
        morpho_records = [r for r in records if r.protocol == ProtocolName.MORPHO]
        assert len(morpho_records) == 1
        assert morpho_records[0].status == ExecutionStatus.SKIPPED
        assert "reserve" in morpho_records[0].error.lower()

    @pytest.mark.asyncio
    async def test_dryrun_doesnt_poison_cooldown(self, db, gas, scope, sample_rates, sample_plan):
        """Dry-run records don't block subsequent real executions (HIGH-01 fix)."""
        portfolio = Portfolio(Decimal("10000"), db)

        # First: dry-run
        executor_dry = Executor(
            mode=ExecutionMode.DRY_RUN, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        dry_records = await executor_dry.execute_plan(sample_plan, sample_rates)
        assert len(dry_records) > 0
        assert all(r.status == ExecutionStatus.SIMULATED for r in dry_records)

        # Second: real paper execution should NOT be blocked by dry-run cooldown
        executor_paper = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        paper_records = await executor_paper.execute_plan(sample_plan, sample_rates)
        successful = [r for r in paper_records if r.status == ExecutionStatus.SUCCESS]
        assert len(successful) > 0

    @pytest.mark.asyncio
    async def test_capital_scaling_on_load(self, db):
        """Portfolio positions scale down when capital is reduced (HIGH-02 fix)."""
        # Save snapshot with $10k capital, $8k allocated
        portfolio = Portfolio(Decimal("10000"), db)
        portfolio.positions = {"aave-v3": Decimal("5000"), "morpho-v1": Decimal("3000")}
        await portfolio.save_snapshot()

        # Load with reduced capital ($5k)
        small_portfolio = Portfolio(Decimal("5000"), db)
        loaded = await small_portfolio.load_from_db()

        assert loaded is True
        assert small_portfolio.allocated_usd <= Decimal("5000")
        assert small_portfolio.reserve_usd >= Decimal("0")

    @pytest.mark.asyncio
    async def test_timezone_safe_cooldown(self, db, gas, scope, sample_rates):
        """Cooldown works regardless of timezone parsing (MED-04 fix)."""
        portfolio = Portfolio(Decimal("10000"), db)
        portfolio.positions["aave-v3"] = Decimal("5000")

        # Insert record with explicit UTC timezone string
        record = ExecutionRecord(
            id=str(uuid.uuid4()),
            action=ActionType.WITHDRAW,
            protocol=ProtocolName.AAVE_V3,
            chain=Chain.BASE,
            amount_usd=Decimal("1000"),
            mode=ExecutionMode.PAPER,
            status=ExecutionStatus.SUCCESS,
        )
        await db.insert_execution(record)

        # Should not crash on timezone parsing
        last_time = await db.get_last_execution_time("aave-v3")
        assert last_time is not None
        assert last_time.tzinfo is not None  # Must be timezone-aware
