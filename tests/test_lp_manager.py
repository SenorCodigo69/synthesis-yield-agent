"""Tests for LPManager — automated concentrated LP management loop."""

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.lp_manager import LPManager, ManagedPosition, CycleResult
from src.lp_signals import LPSignals
from src.lp_optimizer import OptimizedRange
from src.lp_rebalancer import RebalanceDecision
from src.uniswap_lp import MintResult, ExitResult


def _make_signals(
    price=2500, regime="sideways", confidence=0.6, atr_pct=0.03,
    rsi=50, adx=20, trend="flat",
) -> LPSignals:
    return LPSignals(
        current_price=price,
        atr=price * atr_pct,
        atr_pct=atr_pct,
        bb_upper=price * 1.04,
        bb_lower=price * 0.96,
        bb_width_pct=0.08,
        rsi=rsi,
        adx=adx,
        regime=regime,
        regime_confidence=confidence,
        trend_direction=trend,
        timestamp=time.time(),
    )


def _make_mint_result(token_id=100) -> MintResult:
    return MintResult(
        token_id=token_id, liquidity=1000000,
        amount0=10**15, amount1=10**6,
        tx_hash="0xabc", block_number=1, gas_used=100000,
    )


def _make_exit_result() -> ExitResult:
    return ExitResult(
        amount0=10**15, amount1=10**6,
        fees0=10**12, fees1=5000,
        tx_hash_decrease="0xdec", tx_hash_collect="0xcol", tx_hash_burn="0xbrn",
    )


@pytest.fixture
def manager():
    w3 = AsyncMock()
    mgr = LPManager(w3, private_key="0x" + "ab" * 32)
    mgr.adapter = AsyncMock()
    return mgr


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_snapshot_only_when_no_confidence(self, manager):
        """No position + zero confidence → snapshot only, don't mint."""
        signals = _make_signals(confidence=0)
        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()
        assert result.action == "snapshot_only"

    @pytest.mark.asyncio
    async def test_hold_on_strong_bear(self, manager):
        """No position + strong bear → don't enter LP."""
        signals = _make_signals(regime="bear", confidence=0.8)
        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()
        assert result.action == "hold"
        assert "bear" in result.details.lower()

    @pytest.mark.asyncio
    async def test_mint_when_no_position(self, manager):
        """No position + good signals → mint concentrated."""
        signals = _make_signals(regime="sideways", confidence=0.6)
        manager.adapter.get_balances.return_value = (Decimal("0.01"), Decimal("25"))
        manager.adapter.mint_concentrated.return_value = _make_mint_result(token_id=42)

        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()

        assert result.action == "mint"
        assert manager.position is not None
        assert manager.position.token_id == 42
        manager.adapter.mint_concentrated.assert_called_once()

    @pytest.mark.asyncio
    async def test_hold_when_no_balance(self, manager):
        """No position + no balance → hold."""
        signals = _make_signals(confidence=0.6)
        manager.adapter.get_balances.return_value = (Decimal("0"), Decimal("0"))

        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()

        assert result.action == "hold"
        assert "no weth" in result.details.lower()

    @pytest.mark.asyncio
    async def test_hold_when_in_range(self, manager):
        """Position in range → hold."""
        manager.position = ManagedPosition(
            token_id=42, tick_lower=50000, tick_upper=60000,
            entry_price=2500, entry_regime="sideways",
            minted_at=time.time(), last_rebalance_at=time.time(),
        )
        signals = _make_signals()
        manager.adapter.get_pool_slot0.return_value = (0, 55000)  # In range

        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()

        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_rebalance_when_out_of_range(self, manager):
        """Position out of range → rebalance."""
        manager.position = ManagedPosition(
            token_id=42, tick_lower=50000, tick_upper=60000,
            entry_price=2500, entry_regime="sideways",
            minted_at=time.time(), last_rebalance_at=time.time(),
        )
        signals = _make_signals()
        manager.adapter.get_pool_slot0.return_value = (0, 70000)  # Out of range
        manager.adapter.exit_position.return_value = _make_exit_result()
        manager.adapter.mint_concentrated.return_value = _make_mint_result(token_id=43)

        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()

        assert result.action == "rebalance"
        assert manager.position.token_id == 43  # New position
        manager.adapter.exit_position.assert_called_once()
        manager.adapter.mint_concentrated.assert_called_once()

    @pytest.mark.asyncio
    async def test_exit_on_bear_during_rebalance(self, manager):
        """Rebalance triggered + strong bear → exit and stay out."""
        manager.position = ManagedPosition(
            token_id=42, tick_lower=50000, tick_upper=60000,
            entry_price=2500, entry_regime="sideways",
            minted_at=time.time(), last_rebalance_at=time.time(),
        )
        signals = _make_signals(regime="bear", confidence=0.8)
        manager.adapter.get_pool_slot0.return_value = (0, 70000)  # Out of range
        manager.adapter.exit_position.return_value = _make_exit_result()

        with patch("src.lp_manager.compute_signals", return_value=signals):
            result = await manager.run_once()

        assert result.action == "exit"
        assert manager.position is None
        manager.adapter.mint_concentrated.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_handling(self, manager):
        """Errors produce error result, don't crash."""
        with patch("src.lp_manager.compute_signals", side_effect=RuntimeError("RPC down")):
            result = await manager.run_once()
        assert result.action == "error"
        assert "RPC down" in result.details


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_max_cycles(self, manager):
        """Loop stops after max_cycles."""
        signals = _make_signals(confidence=0)
        with patch("src.lp_manager.compute_signals", return_value=signals):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await manager.run_loop(interval_minutes=1, max_cycles=3)
        # Should have run 3 cycles without error

    @pytest.mark.asyncio
    async def test_stop(self, manager):
        """stop() halts the loop."""
        signals = _make_signals(confidence=0)

        async def stop_after_delay():
            await asyncio.sleep(0.01)
            manager.stop()

        with patch("src.lp_manager.compute_signals", return_value=signals):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                # Run loop and stop concurrently
                await asyncio.gather(
                    manager.run_loop(interval_minutes=1, max_cycles=100),
                    stop_after_delay(),
                )
        assert not manager._running
