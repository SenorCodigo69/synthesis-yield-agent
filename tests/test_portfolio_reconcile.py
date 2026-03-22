"""Tests for portfolio on-chain reconciliation."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from src.portfolio import Portfolio


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.insert_snapshot = AsyncMock()
    db.get_latest_snapshot = AsyncMock(return_value=None)
    return db


@pytest.fixture
def portfolio(mock_db):
    p = Portfolio(Decimal("100"), mock_db)
    p.positions = {"aave-v3": Decimal("40"), "morpho-v1": Decimal("30")}
    return p


def make_adapter(balance: Decimal):
    adapter = AsyncMock()
    adapter.get_balance = AsyncMock(return_value=balance)
    return adapter


class TestReconcile:
    @pytest.mark.asyncio
    async def test_no_drift(self, portfolio):
        adapters = {
            "aave-v3": make_adapter(Decimal("40.00")),
            "morpho-v1": make_adapter(Decimal("30.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert report["aave-v3"]["drift"] == pytest.approx(0.0)
        assert report["morpho-v1"]["drift"] == pytest.approx(0.0)
        # No snapshot saved if no corrections
        portfolio.db.insert_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_corrects_drift(self, portfolio):
        adapters = {
            "aave-v3": make_adapter(Decimal("35.00")),  # DB=40, chain=35
            "morpho-v1": make_adapter(Decimal("30.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert report["aave-v3"]["drift"] == pytest.approx(-5.0)
        assert portfolio.positions["aave-v3"] == Decimal("35.00")
        portfolio.db.insert_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_zero_balance(self, portfolio):
        adapters = {
            "aave-v3": make_adapter(Decimal("0")),  # fully withdrawn
            "morpho-v1": make_adapter(Decimal("30.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert "aave-v3" not in portfolio.positions
        assert report["aave-v3"]["onchain"] == 0.0
        portfolio.db.insert_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_adds_new_position(self, portfolio):
        """If on-chain has balance but DB doesn't, add it."""
        portfolio.positions = {}  # empty DB
        adapters = {
            "aave-v3": make_adapter(Decimal("15.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert portfolio.positions["aave-v3"] == Decimal("15.00")
        assert report["aave-v3"]["drift"] == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_ignores_dust_drift(self, portfolio):
        """Drift under $0.01 is ignored (rounding, yield accrual)."""
        adapters = {
            "aave-v3": make_adapter(Decimal("40.005")),  # $0.005 drift
            "morpho-v1": make_adapter(Decimal("30.003")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        # Positions unchanged
        assert portfolio.positions["aave-v3"] == Decimal("40")
        assert portfolio.positions["morpho-v1"] == Decimal("30")
        portfolio.db.insert_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_adapter_error(self, portfolio):
        error_adapter = AsyncMock()
        error_adapter.get_balance = AsyncMock(side_effect=Exception("RPC timeout"))

        adapters = {
            "aave-v3": error_adapter,
            "morpho-v1": make_adapter(Decimal("30.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert report["aave-v3"]["error"] == "RPC timeout"
        assert report["aave-v3"]["onchain"] is None
        # aave position untouched
        assert portfolio.positions["aave-v3"] == Decimal("40")

    @pytest.mark.asyncio
    async def test_orphaned_position_flagged(self, portfolio):
        """DB has a position for a protocol with no adapter."""
        adapters = {
            "aave-v3": make_adapter(Decimal("40.00")),
            # morpho-v1 has no adapter
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert report["morpho-v1"]["error"] == "no adapter"
        assert report["morpho-v1"]["onchain"] is None

    @pytest.mark.asyncio
    async def test_multiple_corrections(self, portfolio):
        """Both protocols drifted — single snapshot save."""
        adapters = {
            "aave-v3": make_adapter(Decimal("35.00")),
            "morpho-v1": make_adapter(Decimal("25.00")),
        }
        report = await portfolio.reconcile_with_chain(adapters, "0xwallet")

        assert portfolio.positions["aave-v3"] == Decimal("35.00")
        assert portfolio.positions["morpho-v1"] == Decimal("25.00")
        # Only one save for all corrections
        portfolio.db.insert_snapshot.assert_called_once()
