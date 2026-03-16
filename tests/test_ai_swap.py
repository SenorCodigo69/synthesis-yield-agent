"""Tests for AI swap reasoning module."""

import json
from decimal import Decimal

import pytest
import pytest_asyncio

from src.ai_swap import (
    MAX_SWAP_FRACTION,
    MIN_SWAP_USD,
    SwapAction,
    SwapRecommendation,
    build_analysis_prompt,
    get_swap_recommendation,
    parse_recommendation,
    _rule_based_recommendation,
    _best_apy,
)


# ── build_analysis_prompt ─────────────────────────────────────────────────


class TestBuildPrompt:
    def test_prompt_includes_balances(self):
        prompt = build_analysis_prompt(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("50"),
            yield_rates=[
                {"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8}
            ],
            gas_gwei=Decimal("0.01"),
            eth_price=Decimal("2000"),
        )
        assert "$100.00" in prompt
        assert "$50.00" in prompt
        assert "$2,000.00" in prompt
        assert "aave-v3" in prompt

    def test_prompt_includes_rates(self):
        prompt = build_analysis_prompt(
            usdc_balance=Decimal("0"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[
                {"protocol": "morpho-v1", "apy": 0.036, "tvl": 400_000_000, "utilization": 0.7},
                {"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8},
            ],
            gas_gwei=Decimal("0.008"),
            eth_price=Decimal("2500"),
        )
        assert "morpho-v1" in prompt
        assert "aave-v3" in prompt

    def test_prompt_with_empty_rates(self):
        prompt = build_analysis_prompt(
            usdc_balance=Decimal("1000"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[],
            gas_gwei=Decimal("0.01"),
            eth_price=Decimal("2000"),
        )
        assert "USDC balance" in prompt


# ── parse_recommendation ──────────────────────────────────────────────────


class TestParseRecommendation:
    def test_valid_hold(self):
        resp = json.dumps({
            "action": "hold",
            "amount_usd": 0,
            "reasoning": "No profitable action",
            "confidence": 0.9,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert rec.action == SwapAction.HOLD
        assert rec.confidence == 0.9

    def test_valid_swap_usdc_to_weth(self):
        resp = json.dumps({
            "action": "swap_usdc_to_weth",
            "amount_usd": 30,
            "reasoning": "ETH momentum is strong",
            "confidence": 0.7,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("0"))
        assert rec.action == SwapAction.SWAP_USDC_TO_WETH
        assert rec.amount_usd == Decimal("30")

    def test_valid_swap_weth_to_usdc(self):
        resp = json.dumps({
            "action": "swap_weth_to_usdc",
            "amount_usd": 20,
            "reasoning": "Lock in gains",
            "confidence": 0.8,
        })
        rec = parse_recommendation(resp, Decimal("0"), Decimal("50"))
        assert rec.action == SwapAction.SWAP_WETH_TO_USDC
        assert rec.amount_usd == Decimal("20")

    def test_valid_deposit_yield(self):
        resp = json.dumps({
            "action": "deposit_yield",
            "amount_usd": 80,
            "reasoning": "Idle USDC should earn",
            "confidence": 0.85,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("0"))
        assert rec.action == SwapAction.DEPOSIT_YIELD
        assert rec.amount_usd == Decimal("80")

    def test_caps_swap_at_50pct_usdc(self):
        """AI can't recommend swapping more than 50% of USDC balance."""
        resp = json.dumps({
            "action": "swap_usdc_to_weth",
            "amount_usd": 90,
            "reasoning": "All in on ETH",
            "confidence": 1.0,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("0"))
        assert rec.amount_usd == Decimal("50")  # 50% of 100

    def test_caps_swap_at_50pct_weth(self):
        """AI can't recommend swapping more than 50% of WETH balance."""
        resp = json.dumps({
            "action": "swap_weth_to_usdc",
            "amount_usd": 80,
            "reasoning": "Sell all WETH",
            "confidence": 1.0,
        })
        rec = parse_recommendation(resp, Decimal("0"), Decimal("100"))
        assert rec.amount_usd == Decimal("50")  # 50% of 100

    def test_caps_deposit_at_full_balance(self):
        resp = json.dumps({
            "action": "deposit_yield",
            "amount_usd": 200,
            "reasoning": "Deposit everything",
            "confidence": 0.9,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("0"))
        assert rec.amount_usd == Decimal("100")  # Can't deposit more than balance

    def test_rejects_dust_amount(self):
        """Amounts below MIN_SWAP_USD get converted to HOLD."""
        resp = json.dumps({
            "action": "swap_usdc_to_weth",
            "amount_usd": 0.5,
            "reasoning": "Tiny swap",
            "confidence": 0.5,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("0"))
        assert rec.action == SwapAction.HOLD

    def test_invalid_json_returns_hold(self):
        rec = parse_recommendation("not json at all", Decimal("100"), Decimal("50"))
        assert rec.action == SwapAction.HOLD
        assert rec.confidence == 0.0

    def test_unknown_action_returns_hold(self):
        resp = json.dumps({
            "action": "yolo_all_in",
            "amount_usd": 100,
            "reasoning": "????",
            "confidence": 1.0,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert rec.action == SwapAction.HOLD

    def test_strips_markdown_fences(self):
        resp = "```json\n" + json.dumps({
            "action": "hold",
            "amount_usd": 0,
            "reasoning": "Just hold",
            "confidence": 0.9,
        }) + "\n```"
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert rec.action == SwapAction.HOLD
        assert rec.confidence == 0.9

    def test_negative_amount_floored_to_zero(self):
        resp = json.dumps({
            "action": "swap_usdc_to_weth",
            "amount_usd": -50,
            "reasoning": "Negative swap?",
            "confidence": 0.5,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        # Negative amount -> 0 -> below MIN_SWAP_USD -> HOLD
        assert rec.action == SwapAction.HOLD

    def test_confidence_clamped_to_0_1(self):
        resp = json.dumps({
            "action": "hold",
            "amount_usd": 0,
            "reasoning": "Test",
            "confidence": 5.0,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert rec.confidence == 1.0

    def test_confidence_negative_clamped(self):
        resp = json.dumps({
            "action": "hold",
            "amount_usd": 0,
            "reasoning": "Test",
            "confidence": -1.0,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert rec.confidence == 0.0

    def test_reasoning_truncated(self):
        resp = json.dumps({
            "action": "hold",
            "amount_usd": 0,
            "reasoning": "x" * 1000,
            "confidence": 0.5,
        })
        rec = parse_recommendation(resp, Decimal("100"), Decimal("50"))
        assert len(rec.reasoning) <= 500


# ── rule-based recommendation ─────────────────────────────────────────────


class TestRuleBasedRecommendation:
    def test_swaps_weth_back_to_usdc(self):
        """If we have WETH, swap it back for yield."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("0"),
            weth_balance_usd=Decimal("100"),
            yield_rates=[{"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8}],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.SWAP_WETH_TO_USDC
        assert rec.amount_usd == Decimal("50")  # 50% of 100

    def test_deposits_idle_usdc(self):
        """If we have USDC and good rates, deposit."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[{"protocol": "morpho-v1", "apy": 0.036, "tvl": 400_000_000, "utilization": 0.7}],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.DEPOSIT_YIELD
        assert rec.amount_usd == Decimal("80")  # 80% of 100

    def test_holds_with_no_rates(self):
        """If no yield rates available, hold."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.HOLD

    def test_holds_with_low_rates(self):
        """If rates are below 1%, hold."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[{"protocol": "aave-v3", "apy": 0.005, "tvl": 100_000_000, "utilization": 0.8}],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.HOLD

    def test_holds_with_dust(self):
        """Dust amounts = hold."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("0.5"),
            weth_balance_usd=Decimal("0.3"),
            yield_rates=[{"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8}],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.HOLD

    def test_weth_prioritized_over_deposit(self):
        """If we have both WETH and USDC, swap WETH first."""
        rec = _rule_based_recommendation(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("50"),
            yield_rates=[{"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8}],
            eth_price=Decimal("2000"),
        )
        assert rec.action == SwapAction.SWAP_WETH_TO_USDC


# ── get_swap_recommendation (no API key) ──────────────────────────────────


class TestGetSwapRecommendation:
    @pytest.mark.asyncio
    async def test_falls_back_to_rules_without_api_key(self):
        rec = await get_swap_recommendation(
            usdc_balance=Decimal("100"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[{"protocol": "aave-v3", "apy": 0.025, "tvl": 100_000_000, "utilization": 0.8}],
            gas_gwei=Decimal("0.01"),
            eth_price=Decimal("2000"),
            anthropic_api_key=None,
        )
        assert rec.action == SwapAction.DEPOSIT_YIELD

    @pytest.mark.asyncio
    async def test_returns_hold_for_empty_state(self):
        rec = await get_swap_recommendation(
            usdc_balance=Decimal("0"),
            weth_balance_usd=Decimal("0"),
            yield_rates=[],
            gas_gwei=Decimal("0.01"),
            eth_price=Decimal("2000"),
            anthropic_api_key=None,
        )
        assert rec.action == SwapAction.HOLD


# ── _best_apy helper ─────────────────────────────────────────────────────


class TestBestApy:
    def test_returns_max(self):
        rates = [
            {"protocol": "a", "apy": 0.025},
            {"protocol": "b", "apy": 0.036},
        ]
        assert _best_apy(rates) == Decimal("0.036")

    def test_empty_returns_zero(self):
        assert _best_apy([]) == Decimal("0")


# ── SwapAction enum ───────────────────────────────────────────────────────


class TestSwapAction:
    def test_all_values(self):
        assert SwapAction.SWAP_USDC_TO_WETH.value == "swap_usdc_to_weth"
        assert SwapAction.SWAP_WETH_TO_USDC.value == "swap_weth_to_usdc"
        assert SwapAction.DEPOSIT_YIELD.value == "deposit_yield"
        assert SwapAction.HOLD.value == "hold"
