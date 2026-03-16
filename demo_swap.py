#!/usr/bin/env python3
"""Autonomous Swap + Yield Demo — Full DeFi Agent Loop

Demonstrates the complete agentic finance cycle on Base mainnet:

1. CHECK  — Read wallet balances (USDC + WETH)
2. THINK  — AI analyzes yield rates and recommends action
3. SWAP   — Execute Uniswap swap if recommended (WETH -> USDC or USDC -> WETH)
4. EARN   — Deposit USDC into the highest-yielding protocol (Aave/Morpho)
5. REPORT — Show final portfolio state and transaction receipts

This script runs against REAL on-chain state. Use --dry-run to simulate.

Usage:
    python demo_swap.py              # Dry run (quote + simulate)
    python demo_swap.py --live       # Live on-chain execution
    python demo_swap.py --live --ai  # AI-powered live execution
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
from web3 import AsyncWeb3

sys.path.insert(0, ".")

from src.ai_swap import get_swap_recommendation, SwapAction
from src.config import load_config, load_spending_scope
from src.data.aggregator import fetch_validated_rates
from src.data.gas import fetch_gas_onchain
from src.models import Chain, GasPrice
from src.uniswap import (
    UniswapAdapter,
    USDC_BASE,
    WETH_BASE,
    USDC_DECIMALS,
    WETH_DECIMALS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo-swap")

ERC20_BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def banner(title: str) -> None:
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}\n")


def step(num: int, title: str) -> None:
    print(f"\n  Step {num}: {title}")
    print(f"  {'-'*55}")


async def get_balances(w3: AsyncWeb3, wallet: str) -> tuple[Decimal, Decimal]:
    """Fetch USDC and WETH balances."""
    usdc = w3.eth.contract(address=w3.to_checksum_address(USDC_BASE), abi=ERC20_BALANCE_ABI)
    weth = w3.eth.contract(address=w3.to_checksum_address(WETH_BASE), abi=ERC20_BALANCE_ABI)
    usdc_raw = await usdc.functions.balanceOf(wallet).call()
    weth_raw = await weth.functions.balanceOf(wallet).call()
    return (
        Decimal(str(usdc_raw)) / Decimal(10 ** USDC_DECIMALS),
        Decimal(str(weth_raw)) / Decimal(10 ** WETH_DECIMALS),
    )


async def get_eth_price() -> Decimal:
    """Fetch ETH/USD price from CoinGecko."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return Decimal(str(data["ethereum"]["usd"]))
    except Exception:
        pass
    return Decimal("2000")


async def main(live: bool = False, use_ai: bool = False):
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    private_key = config.pop("_private_key", None)
    api_key = os.getenv("UNISWAP_API_KEY", "")

    if not private_key:
        print("  Error: PRIVATE_KEY not set in .env")
        return
    if not api_key:
        print("  Error: UNISWAP_API_KEY not set")
        return

    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    adapter = UniswapAdapter(api_key=api_key, w3=w3)

    from eth_account import Account
    wallet = Account.from_key(private_key).address

    mode_label = "LIVE" if live else "DRY RUN"

    banner(f"Autonomous DeFi Agent — Swap + Yield Loop ({mode_label})")
    print(f"  Wallet:  {wallet}")
    print(f"  Chain:   Base (8453)")
    print(f"  Time:    {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # ── Step 1: Check balances ──────────────────────────────────
    step(1, "Check Wallet Balances")

    usdc_bal, weth_bal = await get_balances(w3, wallet)
    eth_price = await get_eth_price()
    weth_usd = weth_bal * eth_price

    print(f"  USDC:     ${usdc_bal:,.6f}")
    print(f"  WETH:     {weth_bal:.8f} (${weth_usd:,.2f})")
    print(f"  ETH/USD:  ${eth_price:,.2f}")

    # ── Step 2: Fetch yield rates ──────────────────────────────
    step(2, "Scan DeFi Yield Rates")

    yield_rates = []
    rates = []
    try:
        async with aiohttp.ClientSession() as session:
            rates = await fetch_validated_rates(
                http_session=session, rpc_url=rpc_url, chain=Chain.BASE,
            )
        for r in rates:
            print(f"  {r.protocol.value:<15} {r.apy_median:>6.2%} APY  "
                  f"${r.tvl_usd:>12,.0f} TVL  {r.utilization:>5.1%} util")
            yield_rates.append({
                "protocol": r.protocol.value,
                "apy": float(r.apy_median),
                "tvl": float(r.tvl_usd),
                "utilization": float(r.utilization),
            })
    except Exception as e:
        print(f"  Warning: Rate fetch failed: {e}")

    # ── Step 3: AI Reasoning ───────────────────────────────────
    step(3, "AI Swap Reasoning")

    gas_price = GasPrice(
        base_fee_gwei=Decimal("0.008"),
        priority_fee_gwei=Decimal("0.001"),
        source="base-default",
    )
    try:
        w3_gas = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        block = await w3_gas.eth.get_block("latest")
        gas_price = GasPrice(
            base_fee_gwei=Decimal(str(block["baseFeePerGas"])) / Decimal(10**9),
            priority_fee_gwei=Decimal("0.001"),
            source="onchain",
        )
    except Exception:
        pass

    rec = await get_swap_recommendation(
        usdc_balance=usdc_bal,
        weth_balance_usd=weth_usd,
        yield_rates=yield_rates,
        gas_gwei=gas_price.total_gwei,
        eth_price=eth_price,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") if use_ai else None,
    )

    ai_label = "Claude AI" if use_ai and os.getenv("ANTHROPIC_API_KEY") else "Rule-based"
    print(f"  Engine:     {ai_label}")
    print(f"  Action:     {rec.action.value}")
    print(f"  Amount:     ${rec.amount_usd:,.2f}")
    print(f"  Confidence: {rec.confidence:.0%}")
    print(f"  Reasoning:  {rec.reasoning}")

    # ── Step 4: Execute Swap (if recommended) ──────────────────
    swap_result = None

    if rec.action in (SwapAction.SWAP_USDC_TO_WETH, SwapAction.SWAP_WETH_TO_USDC):
        step(4, f"Execute Swap via Uniswap ({rec.action.value})")

        if rec.action == SwapAction.SWAP_USDC_TO_WETH:
            token_in, token_out = USDC_BASE, WETH_BASE
            amount_raw = str(int(rec.amount_usd * Decimal(10 ** USDC_DECIMALS)))
            print(f"  Swapping ${rec.amount_usd:,.2f} USDC -> WETH")
        else:
            token_in, token_out = WETH_BASE, USDC_BASE
            weth_amount = rec.amount_usd / eth_price if eth_price > 0 else Decimal("0")
            amount_raw = str(int(weth_amount * Decimal(10 ** WETH_DECIMALS)))
            print(f"  Swapping {weth_amount:.8f} WETH -> USDC (~${rec.amount_usd:,.2f})")

        if live:
            try:
                async with aiohttp.ClientSession() as session:
                    swap_result = await adapter.swap(
                        session=session,
                        token_in=token_in,
                        token_out=token_out,
                        amount=amount_raw,
                        private_key=private_key,
                        slippage=0.5,
                    )
                print(f"  Tx hash:  {swap_result.tx_hash}")
                print(f"  Block:    {swap_result.block_number}")
                print(f"  Routing:  {swap_result.routing}")
                print(f"  Gas used: {swap_result.gas_used}")
            except Exception as e:
                print(f"  Swap failed: {e}")
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    quote = await adapter.get_quote(
                        session, token_in, token_out, amount_raw, wallet,
                    )
                if rec.action == SwapAction.SWAP_USDC_TO_WETH:
                    out = Decimal(quote.amount_out) / Decimal(10 ** WETH_DECIMALS)
                    print(f"  Quote: ${rec.amount_usd:,.2f} USDC -> {out:.8f} WETH")
                else:
                    out = Decimal(quote.amount_out) / Decimal(10 ** USDC_DECIMALS)
                    print(f"  Quote: WETH -> ${out:,.6f} USDC")
                print(f"  Routing: {quote.routing}")
                print(f"  [DRY RUN — add --live to execute]")
            except Exception as e:
                print(f"  Quote failed: {e}")

    elif rec.action == SwapAction.DEPOSIT_YIELD:
        step(4, "Skip Swap (AI recommends direct deposit)")
        print(f"  No swap needed — USDC is already optimal for yield deposit")

    else:
        step(4, "Skip Swap (AI recommends HOLD)")
        print(f"  No action — market conditions don't warrant a swap")

    # ── Step 5: Deposit for Yield ──────────────────────────────
    step(5, "Deposit USDC for Yield")

    # Re-check USDC balance after potential swap
    if swap_result:
        usdc_bal, weth_bal = await get_balances(w3, wallet)
        print(f"  Updated USDC balance: ${usdc_bal:,.6f}")

    if usdc_bal > Decimal("1") and rates:
        best_rate = max(rates, key=lambda r: r.apy_median)
        print(f"  Best protocol: {best_rate.protocol.value} ({best_rate.apy_median:.2%} APY)")
        deposit_amount = usdc_bal * Decimal("0.8")  # Keep 20% reserve
        print(f"  Deposit amount: ${deposit_amount:,.2f} (80% of balance)")
        print(f"  Reserve kept:   ${usdc_bal - deposit_amount:,.2f}")

        if live:
            print(f"  [Would deposit via {best_rate.protocol.value} adapter]")
            # In production, this calls the protocol adapter's supply()
        else:
            print(f"  [DRY RUN — add --live to deposit]")
    else:
        print(f"  Insufficient USDC for yield deposit (${usdc_bal:,.2f})")

    # ── Summary ────────────────────────────────────────────────
    banner("Agent Loop Complete")
    print(f"  Actions taken:")
    if rec.action != SwapAction.HOLD:
        status = "EXECUTED" if live and swap_result else "SIMULATED"
        print(f"    1. {rec.action.value}: ${rec.amount_usd:,.2f} [{status}]")
    else:
        print(f"    1. HOLD (no swap)")
    if usdc_bal > Decimal("1"):
        print(f"    2. Yield deposit recommended: ${usdc_bal * Decimal('0.8'):,.2f}")
    if swap_result:
        print(f"\n  On-chain receipts:")
        print(f"    Swap tx: {swap_result.tx_hash}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous DeFi swap + yield demo")
    parser.add_argument("--live", action="store_true", help="Execute on-chain")
    parser.add_argument("--ai", action="store_true", help="Use Claude AI for decisions")
    args = parser.parse_args()

    asyncio.run(main(live=args.live, use_ai=args.ai))
