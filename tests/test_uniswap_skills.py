"""Tests for Uniswap AI Skills integration."""

import time
import pytest

from src.uniswap_skills import (
    plan_swap, plan_liquidity, plan_optimal_lp_with_signals,
    SwapPlan, LPPlan, PoolRecommendation,
    TOKENS, CHAIN_IDS,
)
from src.lp_signals import LPSignals
from src.lp_optimizer import OptimizedRange


class TestSwapPlanner:
    def test_basic_swap(self):
        plan = plan_swap("USDC", "ETH", 100, "base")
        assert plan.chain_id == 8453
        assert "app.uniswap.org/swap" in plan.deep_link
        assert "USDC" in plan.summary
        assert plan.amount == 100

    def test_deep_link_has_tokens(self):
        plan = plan_swap("USDC", "WETH", 50)
        assert TOKENS["USDC"] in plan.deep_link
        assert TOKENS["WETH"] in plan.deep_link

    def test_native_eth(self):
        plan = plan_swap("ETH", "USDC", 1)
        assert "NATIVE" in plan.deep_link

    def test_large_swap_warning(self):
        plan = plan_swap("USDC", "ETH", 50000)
        assert any("Large swap" in w for w in plan.warnings)

    def test_same_token_warning(self):
        plan = plan_swap("ETH", "ETH", 1)
        assert any("same" in w.lower() for w in plan.warnings)

    def test_ethereum_gas_warning(self):
        plan = plan_swap("USDC", "ETH", 100, "ethereum")
        assert any("gas" in w.lower() for w in plan.warnings)

    def test_unknown_chain_raises(self):
        with pytest.raises(ValueError, match="Unknown chain"):
            plan_swap("USDC", "ETH", 100, "solana")

    def test_custom_token_address(self):
        plan = plan_swap("0xabc123", "USDC", 10)
        assert "0xabc123" in plan.deep_link

    def test_all_chains(self):
        for chain in CHAIN_IDS:
            plan = plan_swap("USDC", "ETH", 1, chain)
            assert plan.chain_id == CHAIN_IDS[chain]


class TestLiquidityPlanner:
    def test_basic_v3(self):
        plan = plan_liquidity("WETH", "USDC")
        assert plan.version == "v3"
        assert plan.fee_tier == 500
        assert "app.uniswap.org/add" in plan.deep_link

    def test_v4_with_hook(self):
        hook = "0x45eC09fB08B83f104F15f3709F4677736112c080"
        plan = plan_liquidity("WETH", "USDC", version="v4", hook_address=hook)
        assert plan.version == "v4"
        assert hook in plan.deep_link
        assert any("hook" in w.lower() for w in plan.warnings)

    def test_v4_without_hook_warning(self):
        plan = plan_liquidity("WETH", "USDC", version="v4")
        assert any("no hook" in w.lower() for w in plan.warnings)

    def test_nonstandard_fee_warning(self):
        plan = plan_liquidity("WETH", "USDC", fee_tier=250)
        assert any("non-standard" in w.lower() for w in plan.warnings)

    def test_fee_tiers(self):
        for fee in [100, 500, 3000, 10000]:
            plan = plan_liquidity("WETH", "USDC", fee_tier=fee)
            assert str(fee) in plan.deep_link

    def test_unknown_chain_raises(self):
        with pytest.raises(ValueError):
            plan_liquidity("WETH", "USDC", chain="solana")


class TestAIEnhancedPlanning:
    def _signals(self):
        return LPSignals(
            current_price=2500, atr=75, atr_pct=0.03,
            bb_upper=2600, bb_lower=2400, bb_width_pct=0.08,
            rsi=55, adx=22, regime="sideways",
            regime_confidence=0.65, trend_direction="flat",
            timestamp=time.time(),
        )

    def _range(self):
        return OptimizedRange(
            tick_lower=353000, tick_upper=354000,
            price_lower=2375, price_upper=2625,
            width_pct=0.10, regime="sideways",
            confidence=0.65, reasoning="test",
        )

    def test_enriched_plan(self):
        plan = plan_optimal_lp_with_signals(self._signals(), self._range())
        assert "AI-optimized" in plan.summary
        assert "sideways" in plan.summary
        assert any("Regime" in w for w in plan.warnings)
        assert any("ATR" in w for w in plan.warnings)

    def test_with_zk_hook(self):
        hook = "0x45eC09fB08B83f104F15f3709F4677736112c080"
        plan = plan_optimal_lp_with_signals(self._signals(), self._range(), hook_address=hook)
        assert plan.version == "v4"
        assert hook in plan.deep_link
