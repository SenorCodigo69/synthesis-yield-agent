#!/usr/bin/env python3
"""Synthesis Yield Agent — Demo Script

Demonstrates the full agent lifecycle:
1. Scan live rates from Base chain
2. Run health check (circuit breakers + protocol health)
3. Compute optimal allocation
4. Execute in paper mode
5. Show portfolio + P&L dashboard

Run: python demo.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

# Ensure src is importable
sys.path.insert(0, ".")

from src.config import load_config, load_spending_scope
from src.circuit_breakers import CircuitBreakers
from src.data.aggregator import fetch_validated_rates
from src.data.gas import fetch_gas_onchain
from src.database import Database
from src.depeg_monitor import fetch_usdc_price
from src.executor import Executor
from src.health_monitor import HealthMonitor
from src.models import Chain, ExecutionMode, ExecutionStatus, GasPrice
from src.portfolio import Portfolio
from src.strategy.allocator import compute_allocations
from src.strategy.rebalancer import check_rebalance_triggers


CAPITAL = Decimal("10000")
DIVIDER = "=" * 70
SUBDIV = "-" * 70


def header(title: str) -> None:
    print(f"\n  {'=' * 70}")
    print(f"  {title}")
    print(f"  {'=' * 70}\n")


def step(num: int, title: str) -> None:
    print(f"\n  Step {num}: {title}")
    print(f"  {SUBDIV}")


async def run_demo():
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)

    header("SYNTHESIS YIELD AGENT — DEMO")
    print(f"  Capital: ${CAPITAL:,.0f}  |  Chain: Base  |  Mode: Paper")
    print(f"  Time: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Step 1: Scan rates ────────────────────────────────────────────
    step(1, "SCAN LIVE RATES")
    print("  Fetching from DeFi Llama + on-chain contracts...\n")

    async with aiohttp.ClientSession() as session:
        rates = await fetch_validated_rates(
            http_session=session, rpc_url=rpc_url, chain=Chain.BASE,
        )

    rates.sort(key=lambda r: r.apy_median, reverse=True)
    for r in rates:
        status = "OK" if r.is_valid else "!!"
        sources = len(r.apy_sources)
        print(
            f"  [{status}] {r.protocol.value:<15} "
            f"APY: {r.apy_median:>5.2f}%  |  "
            f"TVL: ${r.tvl_usd:>12,.0f}  |  "
            f"Sources: {sources}"
        )
    print(f"\n  {len(rates)} protocols scanned, "
          f"{sum(1 for r in rates if r.is_valid)} valid")

    # ── Step 2: Health check ──────────────────────────────────────────
    step(2, "HEALTH CHECK")

    gas = GasPrice(
        base_fee_gwei=Decimal("0.01"),
        priority_fee_gwei=Decimal("0.001"),
        source="default-base",
    )
    try:
        from web3 import AsyncWeb3
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        onchain_gas = await fetch_gas_onchain(w3)
        if onchain_gas:
            gas = onchain_gas
    except Exception:
        pass

    async with aiohttp.ClientSession() as session:
        usdc_price = await fetch_usdc_price(session)

    breakers = CircuitBreakers(config)
    monitor = HealthMonitor(breakers, scope)
    health = monitor.check_system_health(rates, gas, usdc_price)

    status_label = "OPERATIONAL" if health.is_operational else "DEGRADED"
    print(f"  System: {status_label}")
    print(f"  USDC:   ${usdc_price:.4f}")
    print(f"  Gas:    {gas.total_gwei:.4f} gwei")
    print()

    for name, h in health.protocols.items():
        icon = {"healthy": "OK", "warning": "!?", "critical": "XX"}[h.status.value]
        print(f"  [{icon}] {name:<15} {h.checks_passed}/{h.checks_total} checks")
        for issue in h.issues:
            print(f"       {issue}")

    if not health.is_operational:
        print("\n  System not operational — demo stopping here.")
        return

    # ── Step 3: Compute allocation ────────────────────────────────────
    step(3, "COMPUTE ALLOCATION")

    plan = compute_allocations(rates, gas, CAPITAL, scope)

    for sp in plan.scored_protocols:
        tag = "ELIGIBLE" if sp.eligible else "REJECTED"
        print(
            f"  [{tag:>8}] {sp.rate.protocol.value:<15} "
            f"Net APY: {sp.net_apy.net_apy:>5.2f}%  "
            f"Risk: {sp.risk.total:.3f}  "
            f"RAY: {sp.risk_adjusted_yield:.2f}"
        )
        if not sp.eligible:
            for reason in sp.rejection_reasons:
                print(f"             ! {reason}")

    print()
    if plan.allocations:
        for a in plan.allocations:
            bar_len = int(float(a.target_pct) * 30)
            bar = "#" * bar_len
            print(f"  {a.protocol.value:<15} ${a.amount_usd:>10,.2f}  ({a.target_pct:>5.1%})  [{bar}]")
        print(f"\n  Total: ${plan.total_allocated_usd:,.2f}  |  Reserve: ${plan.reserve_usd:,.2f}")
    else:
        print("  No allocations — all capital in reserve.")

    # ── Step 4: Execute (paper mode) ──────────────────────────────────
    step(4, "EXECUTE (PAPER MODE)")

    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(CAPITAL, db)
        await portfolio.load_from_db()

        executor = Executor(
            mode=ExecutionMode.PAPER, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, rates)

        if not records:
            print("  Portfolio already matches target — no actions needed.")
        else:
            for r in records:
                icon = {"success": "+", "failed": "X", "skipped": "-"}.get(r.status.value, "?")
                print(
                    f"  [{icon}] {r.action.value:<10} {r.protocol.value:<15} "
                    f"${r.amount_usd:>10,.2f}  ({r.status.value})"
                )

        # ── Step 5: Dashboard ─────────────────────────────────────────
        step(5, "PORTFOLIO DASHBOARD")

        counts = await db.get_execution_count()
        total_gas = await db.get_total_gas_spent()
        net_profit = portfolio.unrealized_yield_usd - total_gas

        print(f"  Capital:       ${portfolio.total_capital_usd:>10,.2f}")
        alloc_pct = (
            portfolio.allocated_usd / portfolio.total_capital_usd
            if portfolio.total_capital_usd > 0 else Decimal("0")
        )
        print(f"  Allocated:     ${portfolio.allocated_usd:>10,.2f}  ({alloc_pct:.1%})")
        print(f"  Reserve:       ${portfolio.reserve_usd:>10,.2f}")
        print(f"  Yield earned:  ${portfolio.unrealized_yield_usd:>10,.6f}")
        print(f"  Gas spent:     ${total_gas:>10,.6f}")
        print(f"  Net profit:    ${net_profit:>10,.6f}")

        if portfolio.positions:
            print()
            for proto, amount in sorted(portfolio.positions.items()):
                pct = (
                    amount / portfolio.total_capital_usd
                    if portfolio.total_capital_usd > 0 else Decimal("0")
                )
                bar_len = int(float(pct) * 30)
                bar = "#" * bar_len
                print(f"    {proto:<15} ${amount:>10,.2f}  ({pct:>5.1%})  [{bar}]")

        total_exec = sum(counts.values()) if counts else 0
        print(f"\n  Total executions: {total_exec}")

    finally:
        await db.close()

    # ── Done ──────────────────────────────────────────────────────────
    header("DEMO COMPLETE")
    print("  The agent scanned live data, checked health, computed an")
    print("  allocation plan, and executed in paper mode — all autonomously.")
    print()
    print("  Next steps:")
    print("    python -m src run          # Continuous agent loop")
    print("    python -m src dashboard    # Full P&L dashboard")
    print("    python -m src health       # System health check")
    print()


if __name__ == "__main__":
    asyncio.run(run_demo())
