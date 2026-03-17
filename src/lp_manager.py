"""Automated concentrated LP manager — continuous loop.

Runs the full LP lifecycle autonomously:
1. Read on-chain pool price → store snapshot
2. Compute quant signals (ATR, BB, RSI, ADX, regime)
3. If no active concentrated position → mint one with optimized range
4. If active position → check rebalance triggers
5. If rebalance needed → exit old position → mint new concentrated position
6. Track IL and fee profitability

The loop runs every `interval_minutes` and logs all decisions.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from web3 import AsyncWeb3

from .lp_il_tracker import compute_il_report
from .lp_optimizer import OptimizedRange, compute_range
from .lp_rebalancer import check_rebalance, RebalanceDecision
from .lp_signals import LPSignals, compute_signals, store_snapshot, read_pool_price
from .uniswap_lp import (
    UniswapLPAdapter, MintResult, ExitResult,
    WETH_DECIMALS, USDC_DECIMALS, DEFAULT_FEE,
)
from . import lp_tick_math as tm

logger = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    """Tracks state of the actively managed LP position."""
    token_id: int
    tick_lower: int
    tick_upper: int
    entry_price: float
    entry_regime: str
    minted_at: float  # Unix timestamp
    last_rebalance_at: float


@dataclass
class CycleResult:
    """Result of one LP management cycle."""
    action: str  # "snapshot_only", "mint", "rebalance", "hold", "exit", "error"
    details: str
    position: ManagedPosition | None = None


class LPManager:
    """Autonomous concentrated LP manager.

    Usage:
        manager = LPManager(w3, private_key)
        await manager.run_loop(interval_minutes=5)
    """

    def __init__(
        self,
        w3: AsyncWeb3,
        private_key: str,
        fee: int = DEFAULT_FEE,
        db_path: Path | None = None,
    ):
        self.adapter = UniswapLPAdapter(w3)
        self.private_key = private_key
        self.wallet = Account.from_key(private_key).address
        self.fee = fee
        self.db_path = db_path
        self.position: ManagedPosition | None = None
        self._running = False

    async def run_once(self) -> CycleResult:
        """Run a single LP management cycle.

        This is the core decision loop:
        1. Read price, store snapshot, compute signals
        2. If no position → compute range → mint
        3. If position exists → check rebalance → execute if needed
        """
        try:
            # Step 1: Compute signals (reads pool, stores snapshot)
            signals = await compute_signals(db_path=self.db_path)
            logger.info(
                "LP cycle: price=$%.2f regime=%s(%s) ATR=%.1f%% RSI=%.0f ADX=%.0f",
                signals.current_price, signals.regime, signals.trend_direction,
                signals.atr_pct * 100, signals.rsi, signals.adx,
            )

            # Step 2: No position → mint new one
            if self.position is None:
                return await self._handle_no_position(signals)

            # Step 3: Position exists → check rebalance
            return await self._handle_existing_position(signals)

        except Exception as e:
            logger.error("LP cycle error: %s", e, exc_info=True)
            # Truncate error details to avoid leaking sensitive info (RPC URLs, keys)
            err_msg = str(e)[:200] if str(e) else "Unknown error"
            return CycleResult(action="error", details=err_msg)

    async def _handle_no_position(self, signals: LPSignals) -> CycleResult:
        """No active position — compute range and mint if signals are strong enough."""
        # Need sufficient signal history for a confident range
        if signals.regime_confidence == 0:
            logger.info("Insufficient signal history — snapshot only, no mint yet")
            return CycleResult(action="snapshot_only", details="Building signal history")

        # Strong bear → don't enter LP
        if signals.regime == "bear" and signals.regime_confidence > 0.7:
            logger.info("Strong bear regime — skipping LP entry")
            return CycleResult(action="hold", details=f"Bear regime ({signals.regime_confidence:.0%} conf), holding")

        # Compute optimal range
        opt = compute_range(signals, self.fee)
        logger.info(
            "Optimal range: [%d, %d] ($%.0f–$%.0f) width=%.1f%% regime=%s",
            opt.tick_lower, opt.tick_upper,
            opt.price_lower, opt.price_upper, opt.width_pct * 100, opt.regime,
        )

        # Check wallet balances
        weth_bal, usdc_bal = await self.adapter.get_balances(self.wallet)

        # Use available balances (leave small buffer for gas)
        weth_raw = int(weth_bal * Decimal(10**WETH_DECIMALS) * Decimal("0.95"))
        usdc_raw = int(usdc_bal * Decimal(10**USDC_DECIMALS) * Decimal("0.95"))

        if weth_raw <= 0 and usdc_raw <= 0:
            return CycleResult(action="hold", details="No WETH or USDC to deploy")

        # Mint concentrated position
        result = await self.adapter.mint_concentrated(
            private_key=self.private_key,
            weth_amount=weth_raw,
            usdc_amount=usdc_raw,
            tick_lower=opt.tick_lower,
            tick_upper=opt.tick_upper,
            fee=self.fee,
        )

        now = time.time()
        self.position = ManagedPosition(
            token_id=result.token_id,
            tick_lower=opt.tick_lower,
            tick_upper=opt.tick_upper,
            entry_price=signals.current_price,
            entry_regime=signals.regime,
            minted_at=now,
            last_rebalance_at=now,
        )

        details = (
            f"Minted #{result.token_id} ticks=[{opt.tick_lower},{opt.tick_upper}] "
            f"${opt.price_lower:.0f}–${opt.price_upper:.0f} "
            f"regime={opt.regime} tx={result.tx_hash}"
        )
        logger.info("LP MINT: %s", details)
        return CycleResult(action="mint", details=details, position=self.position)

    async def _handle_existing_position(self, signals: LPSignals) -> CycleResult:
        """Position exists — check if rebalance is needed."""
        pos = self.position
        _, current_tick = await self.adapter.get_pool_slot0(self.fee)

        decision = check_rebalance(
            current_tick=current_tick,
            tick_lower=pos.tick_lower,
            tick_upper=pos.tick_upper,
            entry_regime=pos.entry_regime,
            last_rebalance_ts=pos.last_rebalance_at,
            signals=signals,
        )

        logger.info(
            "Rebalance check: urgency=%s rebalance=%s reason=%s",
            decision.urgency, decision.should_rebalance, decision.reason,
        )

        if not decision.should_rebalance:
            return CycleResult(
                action="hold",
                details=f"In range, no rebalance. Tick {current_tick} in [{pos.tick_lower},{pos.tick_upper}]",
                position=pos,
            )

        # Execute rebalance: exit → mint new
        return await self._execute_rebalance(signals, decision)

    async def _execute_rebalance(
        self, signals: LPSignals, decision: RebalanceDecision
    ) -> CycleResult:
        """Exit current position and mint a new one with updated range."""
        pos = self.position
        new_range = decision.new_range

        # Step 1: Exit old position (decrease liquidity + collect + burn)
        logger.info("REBALANCE: exiting position #%d", pos.token_id)
        old_token_id = pos.token_id
        self.position = None  # Clear immediately — if mint fails, next cycle sees "no position"
        exit_result = await self.adapter.exit_position(self.private_key, old_token_id)

        weth_out = exit_result.amount0 + exit_result.fees0
        usdc_out = exit_result.amount1 + exit_result.fees1

        logger.info(
            "Exited #%d: %s WETH + %s USDC (incl fees)",
            pos.token_id,
            Decimal(str(weth_out)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(usdc_out)) / Decimal(10**USDC_DECIMALS),
        )

        # Strong bear with high confidence → don't re-enter
        if signals.regime == "bear" and signals.regime_confidence > 0.7:
            self.position = None
            details = f"Exited #{pos.token_id}, bear regime — staying out"
            logger.info("LP EXIT (bear): %s", details)
            return CycleResult(action="exit", details=details)

        # Step 2: Mint new position with updated range
        # Use 95% of returned tokens (leave buffer for rounding)
        weth_raw = weth_out * 95 // 100
        usdc_raw = usdc_out * 95 // 100

        if weth_raw <= 0 and usdc_raw <= 0:
            self.position = None
            return CycleResult(action="exit", details=f"Exited #{pos.token_id}, no tokens to re-deploy")

        mint_result = await self.adapter.mint_concentrated(
            private_key=self.private_key,
            weth_amount=weth_raw,
            usdc_amount=usdc_raw,
            tick_lower=new_range.tick_lower,
            tick_upper=new_range.tick_upper,
            fee=self.fee,
        )

        now = time.time()
        self.position = ManagedPosition(
            token_id=mint_result.token_id,
            tick_lower=new_range.tick_lower,
            tick_upper=new_range.tick_upper,
            entry_price=signals.current_price,
            entry_regime=signals.regime,
            minted_at=now,
            last_rebalance_at=now,
        )

        details = (
            f"Rebalanced: #{pos.token_id} → #{mint_result.token_id} "
            f"ticks=[{new_range.tick_lower},{new_range.tick_upper}] "
            f"${new_range.price_lower:.0f}–${new_range.price_upper:.0f} "
            f"reason={decision.reason} tx={mint_result.tx_hash}"
        )
        logger.info("LP REBALANCE: %s", details)
        return CycleResult(action="rebalance", details=details, position=self.position)

    async def run_loop(self, interval_minutes: int = 5, max_cycles: int | None = None):
        """Run the LP manager continuously.

        Args:
            interval_minutes: Minutes between cycles (default 5).
            max_cycles: Stop after N cycles (None = run forever).
        """
        if interval_minutes < 1:
            raise ValueError(f"interval_minutes must be >= 1, got {interval_minutes}")
        self._running = True
        cycle = 0

        logger.info(
            "LP Manager starting: wallet=%s fee=%d interval=%dmin",
            self.wallet, self.fee, interval_minutes,
        )

        while self._running:
            cycle += 1
            if max_cycles and cycle > max_cycles:
                logger.info("Max cycles (%d) reached, stopping", max_cycles)
                break

            logger.info("── LP Cycle %d ──", cycle)
            result = await self.run_once()
            logger.info("Cycle %d result: %s — %s", cycle, result.action, result.details)

            if self._running:
                await asyncio.sleep(interval_minutes * 60)

    def stop(self):
        """Signal the loop to stop after current cycle."""
        self._running = False
        logger.info("LP Manager stop requested")
