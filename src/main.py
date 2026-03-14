"""Synthesis Yield Agent — CLI entry point.

Autonomous DeFi yield agent that scans Aave V3, Morpho Blue,
and Compound V3 for the best USDC supply rates on Base.

Usage:
    python -m src scan              # Scan rates across protocols
    python -m src scan --json       # JSON output for piping
    python -m src allocate          # Show optimal allocation plan
    python -m src allocate --capital 50000  # Custom capital amount
    python -m src execute           # Paper-mode execution (one-shot)
    python -m src portfolio         # Show current portfolio state
    python -m src history           # Show execution history
    python -m src dashboard         # Audit trail dashboard with P&L
    python -m src health            # System health check
    python -m src emergency-withdraw  # Emergency withdraw all positions
    python -m src run               # Start the yield agent loop
"""

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
import click

from src.circuit_breakers import CircuitBreakers, BreakerAction
from src.config import load_config, load_spending_scope
from src.data.aggregator import fetch_validated_rates
from src.data.gas import fetch_gas_onchain
from src.database import Database
from src.depeg_monitor import fetch_usdc_price
from src.executor import Executor
from src.health_monitor import HealthMonitor
from src.models import (
    ActionType,
    Chain,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    GasPrice,
    ProtocolName,
    SpendingScope,
)
from src.portfolio import Portfolio
from src.strategy.allocator import AllocationPlan, Allocation, compute_allocations
from src.strategy.rebalancer import check_rebalance_triggers, RebalanceTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yield-agent")


CHAIN_MAP = {"base": Chain.BASE, "ethereum": Chain.ETHEREUM, "arbitrum": Chain.ARBITRUM}


def _parse_chain(chain_name: str) -> Chain:
    chain = CHAIN_MAP.get(chain_name.lower())
    if not chain:
        click.echo(f"Unknown chain: {chain_name}. Use: base, ethereum, arbitrum")
        sys.exit(1)
    return chain


async def _get_gas(rpc_url: str) -> GasPrice:
    """Fetch on-chain gas, fall back to Base defaults."""
    try:
        from web3 import AsyncWeb3
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        gas = await fetch_gas_onchain(w3)
        if gas:
            return gas
    except Exception:
        pass
    return GasPrice(
        base_fee_gwei=Decimal("0.01"),
        priority_fee_gwei=Decimal("0.001"),
        source="default-base",
    )


@click.group()
def cli():
    """Synthesis Yield Agent — autonomous DeFi yield optimization."""
    pass


# ── scan ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--chain", default="base", help="Chain to scan (base/ethereum/arbitrum)")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def scan(chain: str, use_json: bool):
    """Scan current USDC yield rates across protocols."""
    asyncio.run(_scan(chain, use_json))


async def _scan(chain_name: str, use_json: bool):
    """Fetch and display cross-validated yield rates."""
    chain = _parse_chain(chain_name)
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")

    async with aiohttp.ClientSession() as session:
        rates = await fetch_validated_rates(
            http_session=session,
            rpc_url=rpc_url,
            chain=chain,
        )

    if use_json:
        output = []
        for r in rates:
            output.append({
                "protocol": r.protocol.value,
                "chain": r.chain.value,
                "apy_median": float(r.apy_median),
                "tvl_usd": float(r.tvl_usd),
                "utilization": float(r.utilization),
                "is_valid": r.is_valid,
                "sources": {k.value: float(v) for k, v in r.apy_sources.items()},
                "warnings": r.warnings,
            })
        click.echo(json.dumps(output, indent=2))
        return

    # Pretty table output
    click.echo()
    click.echo(f"  USDC Yield Rates — {chain.value}")
    click.echo(f"  {'='*60}")

    if not rates:
        click.echo("  No pools found.")
        return

    rates.sort(key=lambda r: r.apy_median, reverse=True)

    for r in rates:
        status = "OK" if r.is_valid else "BLOCKED"
        sources_str = ", ".join(
            f"{k.value}: {v:.2f}%" for k, v in r.apy_sources.items()
        )
        click.echo(
            f"  [{status:>7}] {r.protocol.value:<15} "
            f"APY: {r.apy_median:>6.2f}%  |  "
            f"TVL: ${r.tvl_usd:>12,.0f}  |  "
            f"Util: {r.utilization:>5.1%}"
        )
        click.echo(f"           Sources: {sources_str}")
        for w in r.warnings:
            click.echo(f"           {w}")
        click.echo()


# ── allocate ──────────────────────────────────────────────────────────────

@cli.command()
@click.option("--chain", default="base", help="Chain (base/ethereum/arbitrum)")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--hold-days", default=90, type=int, help="Expected hold period in days")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def allocate(chain: str, capital: float, hold_days: int, use_json: bool):
    """Compute optimal allocation across yield protocols."""
    asyncio.run(_allocate(chain, Decimal(str(capital)), hold_days, use_json))


async def _allocate(
    chain_name: str,
    capital: Decimal,
    hold_days: int,
    use_json: bool,
):
    """Fetch rates, score risk, compute allocation plan."""
    chain = _parse_chain(chain_name)
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)

    async with aiohttp.ClientSession() as session:
        rates = await fetch_validated_rates(
            http_session=session,
            rpc_url=rpc_url,
            chain=chain,
        )

    gas = await _get_gas(rpc_url)

    plan = compute_allocations(
        rates=rates,
        gas_price=gas,
        total_capital_usd=capital,
        scope=scope,
        hold_days=hold_days,
    )

    signals = check_rebalance_triggers(rates, plan, gas, scope)

    if use_json:
        output = {
            "capital_usd": float(capital),
            "allocated_usd": float(plan.total_allocated_usd),
            "reserve_usd": float(plan.reserve_usd),
            "gas_gwei": float(gas.total_gwei),
            "allocations": [
                {
                    "protocol": a.protocol.value,
                    "amount_usd": float(a.amount_usd),
                    "target_pct": float(a.target_pct),
                }
                for a in plan.allocations
            ],
            "scored_protocols": [
                {
                    "protocol": sp.rate.protocol.value,
                    "gross_apy": float(sp.net_apy.gross_apy),
                    "net_apy": float(sp.net_apy.net_apy),
                    "risk_score": float(sp.risk.total),
                    "risk_adjusted_yield": float(sp.risk_adjusted_yield),
                    "eligible": sp.eligible,
                    "rejection_reasons": sp.rejection_reasons,
                }
                for sp in plan.scored_protocols
            ],
            "rebalance_signals": [
                {
                    "trigger": s.trigger.value,
                    "severity": s.severity,
                    "message": s.message,
                    "should_act": s.should_act,
                }
                for s in signals
            ],
        }
        click.echo(json.dumps(output, indent=2))
        return

    # Pretty output
    click.echo()
    click.echo(f"  Allocation Plan — {chain.value}")
    click.echo(f"  Capital: ${capital:,.2f}  |  Hold: {hold_days}d  |  Gas: {gas.total_gwei:.4f} gwei")
    click.echo(f"  {'='*70}")

    click.echo()
    click.echo("  Protocol Analysis:")
    click.echo(f"  {'-'*70}")
    for sp in plan.scored_protocols:
        status = "ELIGIBLE" if sp.eligible else "REJECTED"
        click.echo(
            f"  [{status:>8}] {sp.rate.protocol.value:<15} "
            f"Gross: {sp.net_apy.gross_apy:>5.2f}%  "
            f"Net: {sp.net_apy.net_apy:>5.2f}%  "
            f"Risk: {sp.risk.total:.3f}  "
            f"RAY: {sp.risk_adjusted_yield:.2f}"
        )
        if not sp.eligible:
            for reason in sp.rejection_reasons:
                click.echo(f"             ! {reason}")
        else:
            for detail in sp.risk.details[:3]:
                click.echo(f"             {detail}")
    click.echo()

    if plan.allocations:
        click.echo("  Allocation:")
        click.echo(f"  {'-'*70}")
        for a in plan.allocations:
            bar_len = int(float(a.target_pct) * 40)
            bar = "#" * bar_len
            click.echo(
                f"  {a.protocol.value:<15} ${a.amount_usd:>10,.2f}  "
                f"({a.target_pct:>5.1%})  [{bar}]"
            )
        click.echo(f"  {'-'*70}")
        click.echo(
            f"  Total allocated: ${plan.total_allocated_usd:>10,.2f}  "
            f"({plan.allocated_pct:.1%})"
        )
        click.echo(f"  Reserve:         ${plan.reserve_usd:>10,.2f}")
    else:
        click.echo("  No allocations — all capital held in reserve.")

    if signals:
        click.echo()
        click.echo("  Rebalance Signals:")
        click.echo(f"  {'-'*70}")
        for s in signals:
            icon = {"critical": "!!!", "warning": " ! ", "info": " i "}[s.severity]
            action = "ACT NOW" if s.should_act else "MONITOR"
            click.echo(f"  [{icon}] [{action}] {s.message}")
    click.echo()


# ── execute ───────────────────────────────────────────────────────────────

@cli.command()
@click.option("--chain", default="base", help="Chain (base/ethereum/arbitrum)")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--hold-days", default=90, type=int, help="Expected hold period in days")
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run"]),
              help="Execution mode")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def execute(chain: str, capital: float, hold_days: int, mode: str, use_json: bool):
    """Execute allocation plan (paper mode by default)."""
    exec_mode = ExecutionMode.PAPER if mode == "paper" else ExecutionMode.DRY_RUN
    asyncio.run(_execute(chain, Decimal(str(capital)), hold_days, exec_mode, use_json))


async def _execute(
    chain_name: str,
    capital: Decimal,
    hold_days: int,
    mode: ExecutionMode,
    use_json: bool,
):
    """One-shot execution: scan -> allocate -> execute."""
    chain = _parse_chain(chain_name)
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)

    # Init database and portfolio
    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(capital, db)
        await portfolio.load_from_db()

        # Fetch rates and gas
        async with aiohttp.ClientSession() as session:
            rates = await fetch_validated_rates(
                http_session=session, rpc_url=rpc_url, chain=chain,
            )

        gas = await _get_gas(rpc_url)

        # Fetch live USDC price for depeg detection
        async with aiohttp.ClientSession() as price_session:
            usdc_price = await fetch_usdc_price(price_session)

        # Health check before execution
        breakers = CircuitBreakers(config)
        monitor = HealthMonitor(breakers, scope)
        health = monitor.check_system_health(rates, gas, usdc_price)

        if not health.is_operational:
            click.echo()
            click.echo("  EXECUTION BLOCKED — system health check failed:")
            for trip in health.breaker_trips:
                click.echo(f"    [{trip.severity.upper()}] {trip.message}")
            click.echo()
            return

        # Compute allocation
        plan = compute_allocations(rates, gas, capital, scope, hold_days)

        # Execute
        executor = Executor(
            mode=mode, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas,
        )
        records = await executor.execute_plan(plan, rates)

        if use_json:
            output = {
                "mode": mode.value,
                "executions": [
                    {
                        "action": r.action.value,
                        "protocol": r.protocol.value,
                        "amount_usd": float(r.amount_usd),
                        "status": r.status,
                        "tx_hash": r.tx_hash,
                        "gas_usd": float(r.simulated_gas_usd),
                        "reasoning": r.reasoning,
                        "error": r.error,
                    }
                    for r in records
                ],
                "portfolio": portfolio.summary(),
            }
            click.echo(json.dumps(output, indent=2))
        else:
            _print_execution_results(records, portfolio, mode)
    finally:
        await db.close()


def _print_execution_results(records, portfolio, mode):
    """Pretty-print execution results."""
    click.echo()
    click.echo(f"  Execution Results — {mode.value.upper()} mode")
    click.echo(f"  {'='*70}")

    if not records:
        click.echo("  No actions taken — portfolio matches target allocation.")
        click.echo()
        return

    status_icons = {
        ExecutionStatus.SUCCESS: "+", ExecutionStatus.SIMULATED: "~",
        ExecutionStatus.FAILED: "X", ExecutionStatus.SKIPPED: "-",
        ExecutionStatus.PENDING: "?",
    }

    for r in records:
        icon = status_icons.get(r.status, "?")
        click.echo(
            f"  [{icon}] {r.action.value:<10} {r.protocol.value:<15} "
            f"${r.amount_usd:>10,.2f}  |  gas: ${r.simulated_gas_usd:.4f}  |  {r.status.value}"
        )
        if r.error:
            click.echo(f"      Error: {r.error}")
        if r.reasoning:
            click.echo(f"      {r.reasoning}")

    click.echo()
    click.echo("  Portfolio After Execution:")
    click.echo(f"  {'-'*70}")
    click.echo(f"  Allocated:  ${portfolio.allocated_usd:>10,.2f}")
    click.echo(f"  Reserve:    ${portfolio.reserve_usd:>10,.2f}")
    click.echo(f"  Gas spent:  ${portfolio.total_gas_spent_usd:>10,.4f}")

    if portfolio.positions:
        click.echo()
        click.echo("  Positions:")
        for proto, amount in sorted(portfolio.positions.items()):
            click.echo(f"    {proto:<15} ${amount:>10,.2f}")
    click.echo()


# ── portfolio ─────────────────────────────────────────────────────────────

@cli.command(name="portfolio")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def show_portfolio(capital: float, use_json: bool):
    """Show current portfolio state from database."""
    asyncio.run(_show_portfolio(Decimal(str(capital)), use_json))


async def _show_portfolio(capital: Decimal, use_json: bool):
    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(capital, db)
        loaded = await portfolio.load_from_db()

        if use_json:
            click.echo(json.dumps(portfolio.summary(), indent=2))
            return

        click.echo()
        click.echo("  Portfolio State")
        click.echo(f"  {'='*50}")

        if not loaded:
            click.echo("  No portfolio data — run 'execute' first.")
            click.echo()
            return

        click.echo(f"  Capital:       ${portfolio.total_capital_usd:>10,.2f}")
        click.echo(f"  Allocated:     ${portfolio.allocated_usd:>10,.2f}")
        click.echo(f"  Reserve:       ${portfolio.reserve_usd:>10,.2f}")
        click.echo(f"  Yield earned:  ${portfolio.unrealized_yield_usd:>10,.4f}")
        click.echo(f"  Gas spent:     ${portfolio.total_gas_spent_usd:>10,.4f}")
        click.echo(f"  Net value:     ${portfolio.net_value_usd:>10,.2f}")

        if portfolio.positions:
            click.echo()
            click.echo("  Positions:")
            click.echo(f"  {'-'*50}")
            for proto, amount in sorted(portfolio.positions.items()):
                pct = amount / portfolio.total_capital_usd if portfolio.total_capital_usd > 0 else Decimal("0")
                click.echo(f"    {proto:<15} ${amount:>10,.2f}  ({pct:>5.1%})")

        # Show recent snapshots
        snapshots = await db.get_snapshots(limit=5)
        if len(snapshots) > 1:
            click.echo()
            click.echo("  Recent Snapshots:")
            click.echo(f"  {'-'*50}")
            for snap in snapshots[:5]:
                click.echo(
                    f"    {snap.timestamp.strftime('%Y-%m-%d %H:%M')}  "
                    f"Alloc: ${snap.allocated_usd:>10,.2f}  "
                    f"Yield: ${snap.unrealized_yield_usd:>8,.4f}"
                )
        click.echo()
    finally:
        await db.close()


# ── history ───────────────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=20, help="Number of records to show")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def history(limit: int, use_json: bool):
    """Show execution history from database."""
    asyncio.run(_history(limit, use_json))


async def _history(limit: int, use_json: bool):
    db = Database()
    await db.connect()

    try:
        records = await db.get_executions(limit=limit)
        counts = await db.get_execution_count()
        total_gas = await db.get_total_gas_spent()

        if use_json:
            click.echo(json.dumps({
                "records": records,
                "counts": counts,
                "total_gas_usd": float(total_gas),
            }, indent=2, default=str))
            return

        click.echo()
        click.echo("  Execution History")
        click.echo(f"  {'='*70}")

        if not records:
            click.echo("  No executions recorded yet.")
            click.echo()
            return

        for r in records:
            icon = {"success": "+", "failed": "X", "skipped": "-"}.get(r["status"], "?")
            ts = r["timestamp"][:16]
            click.echo(
                f"  [{icon}] {ts}  {r['mode']:<8} {r['action']:<10} "
                f"{r['protocol']:<15} ${Decimal(r['amount_usd']):>10,.2f}  "
                f"{r['status']}"
            )
            if r.get("error"):
                click.echo(f"      Error: {r['error']}")

        click.echo(f"\n  Stats: {counts}  |  Total gas: ${total_gas:.4f}")
        click.echo()
    finally:
        await db.close()


# ── dashboard ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def dashboard(capital: float, use_json: bool):
    """Audit trail dashboard with P&L summary."""
    asyncio.run(_dashboard(Decimal(str(capital)), use_json))


async def _dashboard(capital: Decimal, use_json: bool):
    """Comprehensive dashboard: portfolio + P&L + activity + health."""
    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(capital, db)
        loaded = await portfolio.load_from_db()
        counts = await db.get_execution_count()
        total_gas = await db.get_total_gas_spent()
        recent = await db.get_executions(limit=10)
        snapshots = await db.get_snapshots(limit=50)

        if use_json:
            output = {
                "portfolio": portfolio.summary() if loaded else None,
                "pnl": {
                    "yield_earned_usd": float(portfolio.unrealized_yield_usd),
                    "gas_spent_usd": float(total_gas),
                    "net_profit_usd": float(portfolio.unrealized_yield_usd - total_gas),
                },
                "activity": {
                    "execution_counts": counts,
                    "total_executions": sum(counts.values()) if counts else 0,
                    "recent": recent[:5],
                },
                "snapshots": len(snapshots),
            }
            click.echo(json.dumps(output, indent=2, default=str))
            return

        click.echo()
        click.echo("  YIELD AGENT DASHBOARD")
        click.echo(f"  {'='*70}")
        click.echo(f"  {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

        # ── Portfolio section
        click.echo()
        click.echo("  Portfolio")
        click.echo(f"  {'-'*70}")
        if loaded:
            click.echo(f"  Capital:       ${portfolio.total_capital_usd:>10,.2f}")
            alloc_pct = (
                portfolio.allocated_usd / portfolio.total_capital_usd
                if portfolio.total_capital_usd > 0 else Decimal("0")
            )
            click.echo(f"  Allocated:     ${portfolio.allocated_usd:>10,.2f}  "
                        f"({alloc_pct:.1%})")
            click.echo(f"  Reserve:       ${portfolio.reserve_usd:>10,.2f}")
            if portfolio.positions:
                click.echo()
                for proto, amount in sorted(portfolio.positions.items()):
                    pct = (
                        amount / portfolio.total_capital_usd
                        if portfolio.total_capital_usd > 0 else Decimal("0")
                    )
                    bar_len = int(float(pct) * 30)
                    bar = "#" * bar_len
                    click.echo(f"    {proto:<15} ${amount:>10,.2f}  ({pct:>5.1%})  [{bar}]")
        else:
            click.echo("  No positions — agent has not executed yet.")

        # ── P&L section
        click.echo()
        click.echo("  P&L Summary")
        click.echo(f"  {'-'*70}")
        net_profit = portfolio.unrealized_yield_usd - total_gas
        click.echo(f"  Yield earned:  ${portfolio.unrealized_yield_usd:>10,.6f}")
        click.echo(f"  Gas spent:     ${total_gas:>10,.6f}")
        click.echo(f"  Net profit:    ${net_profit:>10,.6f}  "
                    f"({'positive' if net_profit >= 0 else 'NEGATIVE'})")
        if portfolio.total_capital_usd > 0 and portfolio.unrealized_yield_usd > 0:
            roi_pct = net_profit / portfolio.total_capital_usd * 100
            click.echo(f"  ROI:           {roi_pct:>10.4f}%")

        # ── Yield curve (if snapshots exist)
        if len(snapshots) >= 2:
            click.echo()
            click.echo("  Yield Curve (last 10 snapshots)")
            click.echo(f"  {'-'*70}")
            for snap in snapshots[:10]:
                ts = snap.timestamp.strftime('%m-%d %H:%M')
                yield_bar_len = min(int(float(snap.unrealized_yield_usd) * 10), 40)
                yield_bar = "#" * max(yield_bar_len, 0)
                click.echo(
                    f"    {ts}  Alloc: ${snap.allocated_usd:>8,.0f}  "
                    f"Yield: ${snap.unrealized_yield_usd:>8,.4f}  [{yield_bar}]"
                )

        # ── Activity section
        click.echo()
        click.echo("  Activity")
        click.echo(f"  {'-'*70}")
        total_exec = sum(counts.values()) if counts else 0
        click.echo(f"  Total executions: {total_exec}")
        if counts:
            parts = [f"{status}: {count}" for status, count in sorted(counts.items())]
            click.echo(f"  Breakdown: {', '.join(parts)}")

        if recent:
            click.echo()
            click.echo("  Recent Activity:")
            for r in recent[:5]:
                icon = {"success": "+", "failed": "X", "skipped": "-", "simulated": "~"}.get(r["status"], "?")
                ts = r["timestamp"][:16]
                click.echo(
                    f"    [{icon}] {ts}  {r['action']:<10} "
                    f"{r['protocol']:<15} ${Decimal(r['amount_usd']):>8,.2f}"
                )

        click.echo()
    finally:
        await db.close()


# ── health ────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--chain", default="base", help="Chain (base/ethereum/arbitrum)")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def health(chain: str, use_json: bool):
    """Run system health check (circuit breakers + protocol health)."""
    asyncio.run(_health(chain, use_json))


async def _health(chain_name: str, use_json: bool):
    chain = _parse_chain(chain_name)
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)

    async with aiohttp.ClientSession() as session:
        rates = await fetch_validated_rates(
            http_session=session, rpc_url=rpc_url, chain=chain,
        )

    gas = await _get_gas(rpc_url)
    breakers = CircuitBreakers(config)
    monitor = HealthMonitor(breakers, scope)
    system = monitor.check_system_health(rates, gas)

    if use_json:
        output = {
            "operational": system.is_operational,
            "gas_ok": system.gas_ok,
            "depeg_ok": system.depeg_ok,
            "breaker_trips": [
                {
                    "type": t.breaker.value,
                    "action": t.action.value,
                    "severity": t.severity,
                    "message": t.message,
                    "protocol": t.protocol,
                }
                for t in system.breaker_trips
            ],
            "protocols": {
                name: {
                    "status": h.status.value,
                    "checks_passed": h.checks_passed,
                    "checks_total": h.checks_total,
                    "issues": h.issues,
                }
                for name, h in system.protocols.items()
            },
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo()
    status_label = "OPERATIONAL" if system.is_operational else "DEGRADED"
    click.echo(f"  System Health: {status_label}")
    click.echo(f"  {'='*60}")

    click.echo(f"  Gas:     {'OK' if system.gas_ok else 'FROZEN'}")
    click.echo(f"  Depeg:   {'OK' if system.depeg_ok else 'ALERT'}")

    if system.breaker_trips:
        click.echo()
        click.echo("  Circuit Breaker Trips:")
        click.echo(f"  {'-'*60}")
        for trip in system.breaker_trips:
            icon = "!!!" if trip.severity == "critical" else " ! "
            click.echo(f"  [{icon}] {trip.message}")

    click.echo()
    click.echo("  Protocol Health:")
    click.echo(f"  {'-'*60}")
    for name, h in system.protocols.items():
        status_icon = {
            "healthy": "OK",
            "warning": "!?",
            "critical": "XX",
        }[h.status.value]
        click.echo(
            f"  [{status_icon}] {name:<15} "
            f"{h.checks_passed}/{h.checks_total} checks passed  "
            f"({h.status.value})"
        )
        for issue in h.issues:
            click.echo(f"       {issue}")

    safe = system.safe_protocols
    critical = system.critical_protocols
    click.echo()
    click.echo(f"  Safe for deposits: {', '.join(safe) if safe else 'NONE'}")
    if critical:
        click.echo(f"  WITHDRAW NOW:      {', '.join(critical)}")
    click.echo()


# ── emergency-withdraw ───────────────────────────────────────────────────

@cli.command(name="emergency-withdraw")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run"]),
              help="Execution mode")
@click.option("--reason", default="manual", help="Reason for emergency withdrawal")
@click.option("--yes", "confirmed", is_flag=True, help="Skip confirmation prompt")
def emergency_withdraw(capital: float, mode: str, reason: str, confirmed: bool):
    """Emergency withdraw ALL positions immediately (bypasses cooldowns)."""
    exec_mode = ExecutionMode.PAPER if mode == "paper" else ExecutionMode.DRY_RUN
    asyncio.run(_emergency_withdraw(Decimal(str(capital)), exec_mode, reason, confirmed))


async def _emergency_withdraw(
    capital: Decimal,
    mode: ExecutionMode,
    reason: str,
    confirmed: bool,
):
    """Withdraw everything — bypasses cooldowns and normal flow."""
    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(capital, db)
        loaded = await portfolio.load_from_db()

        if not loaded or not portfolio.positions:
            click.echo("  No positions to withdraw.")
            return

        click.echo()
        click.echo("  EMERGENCY WITHDRAWAL")
        click.echo(f"  {'='*60}")
        click.echo(f"  Mode:   {mode.value}")
        click.echo(f"  Reason: {reason}")
        click.echo()
        click.echo("  Positions to withdraw:")
        for proto, amount in sorted(portfolio.positions.items()):
            click.echo(f"    {proto:<15} ${amount:>10,.2f}")
        click.echo(f"  Total:  ${portfolio.allocated_usd:>10,.2f}")
        click.echo()

        if not confirmed:
            click.echo("  Add --yes to confirm emergency withdrawal.")
            return

        # Use a scope with zero cooldown for emergency
        emergency_scope = SpendingScope(withdrawal_cooldown_secs=0)
        gas = GasPrice(
            base_fee_gwei=Decimal("0.01"),
            priority_fee_gwei=Decimal("0.001"),
            source="emergency-default",
        )

        # Build withdrawal plan for all positions
        records = []
        for proto_key, amount in list(portfolio.positions.items()):
            try:
                protocol = ProtocolName(proto_key)
            except ValueError:
                click.echo(f"  Skipping unknown protocol: {proto_key}")
                continue

            record = ExecutionRecord(
                id=str(uuid.uuid4()),
                action=ActionType.WITHDRAW,
                protocol=protocol,
                chain=Chain.BASE,
                amount_usd=amount,
                mode=mode,
                reasoning=f"EMERGENCY WITHDRAW: {reason}",
            )

            if mode == ExecutionMode.PAPER:
                record.tx_hash = f"emergency-{record.id[:8]}"
                record.block_number = 0
                record.simulated_gas_usd = Decimal("0.001")
                portfolio.apply_execution(record)
                record.status = ExecutionStatus.SUCCESS
            else:
                record.tx_hash = f"emergency-dryrun-{record.id[:8]}"
                record.block_number = 0
                record.simulated_gas_usd = Decimal("0.001")
                record.status = ExecutionStatus.SIMULATED

            await db.insert_execution(record)
            records.append(record)

            click.echo(
                f"  [{record.status.value.upper()}] Withdraw {proto_key}: "
                f"${amount:,.2f}"
            )

        if mode == ExecutionMode.PAPER:
            await portfolio.save_snapshot()

        click.echo()
        click.echo(f"  Emergency withdrawal complete: {len(records)} actions")
        click.echo(f"  Portfolio allocated: ${portfolio.allocated_usd:,.2f}")
        click.echo(f"  Portfolio reserve:   ${portfolio.reserve_usd:,.2f}")
        click.echo()
    finally:
        await db.close()


# ── run (agent loop) ─────────────────────────────────────────────────────

@cli.command()
@click.option("--interval", default=900, help="Scan interval in seconds (default: 900)")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run"]),
              help="Execution mode")
def run(interval: int, capital: float, mode: str):
    """Start the yield agent loop (scan + allocate + execute + rebalance)."""
    exec_mode = ExecutionMode.PAPER if mode == "paper" else ExecutionMode.DRY_RUN
    asyncio.run(_run(interval, Decimal(str(capital)), exec_mode))


async def _run(interval: int, capital: Decimal, mode: ExecutionMode):
    """Main agent loop — scan, score, allocate, execute, monitor.

    Now with circuit breakers and health monitoring per cycle.
    """
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)
    tracker = RebalanceTracker(
        rate_diff_threshold=Decimal(str(config.get("rebalancing", {}).get("rate_diff_threshold", 0.01))),
        rate_diff_sustain_hours=config.get("rebalancing", {}).get("rate_diff_sustain_hours", 6),
    )
    breakers = CircuitBreakers(config)
    monitor = HealthMonitor(breakers, scope)

    db = Database()
    await db.connect()

    try:
        portfolio = Portfolio(capital, db)
        await portfolio.load_from_db()

        click.echo(
            f"Yield agent starting — ${capital:,.0f} capital, "
            f"{interval}s interval, {mode.value} mode"
        )
        click.echo(f"Portfolio: ${portfolio.allocated_usd:,.0f} allocated, "
                    f"${portfolio.reserve_usd:,.0f} reserve")
        click.echo("Circuit breakers: depeg, TVL crash, gas freeze, rate divergence")
        click.echo("Press Ctrl+C to stop.\n")

        cycle = 0
        last_cycle_time: datetime | None = None

        while True:
            cycle += 1
            now = datetime.now(tz=timezone.utc)
            click.echo(f"--- Cycle {cycle} ({now.strftime('%H:%M:%S UTC')}) ---")

            try:
                # Fetch rates
                async with aiohttp.ClientSession() as session:
                    rates = await fetch_validated_rates(
                        http_session=session, rpc_url=rpc_url, chain=Chain.BASE,
                    )

                gas = await _get_gas(rpc_url)
                tracker.record_rates(rates)

                # Fetch live USDC price for depeg detection
                async with aiohttp.ClientSession() as price_session:
                    usdc_price = await fetch_usdc_price(price_session)

                # ── Circuit breaker check ─────────────────────────────
                system_health = monitor.check_system_health(rates, gas, usdc_price)
                trips = system_health.breaker_trips

                if trips:
                    for trip in trips:
                        click.echo(f"  *** [{trip.severity.upper()}] {trip.message}")

                # Handle emergency withdrawals triggered by circuit breakers
                if breakers.requires_emergency_withdraw(trips) and portfolio.positions:
                    click.echo("  !!! CIRCUIT BREAKER EMERGENCY — withdrawing all positions")
                    emergency_scope = SpendingScope(withdrawal_cooldown_secs=0)
                    emergency_executor = Executor(
                        mode=mode, db=db, portfolio=portfolio,
                        scope=emergency_scope, gas_price=gas,
                    )
                    # Empty plan → executor computes full withdrawal for all positions
                    empty_plan = AllocationPlan(
                        allocations=[],
                        scored_protocols=[],
                        total_allocated_usd=Decimal("0"),
                        total_capital_usd=capital,
                        reserve_usd=capital,
                    )
                    emergency_records = await emergency_executor.execute_plan(empty_plan, rates)
                    for r in emergency_records:
                        click.echo(
                            f"  !!! {r.action.value} {r.protocol.value}: "
                            f"${r.amount_usd:,.0f} ({r.status.value})"
                        )

                # If system is frozen (gas too high), skip execution
                if not system_health.is_operational:
                    click.echo("  System degraded — skipping execution this cycle")
                    click.echo()
                    await asyncio.sleep(interval)
                    continue

                # ── Normal cycle ──────────────────────────────────────

                # Accrue yield on existing positions
                if last_cycle_time and portfolio.positions:
                    hours_elapsed = Decimal(str(
                        (now - last_cycle_time).total_seconds() / 3600
                    ))
                    rate_map = {r.protocol.value: r.apy_median for r in rates}
                    total_yield = Decimal("0")
                    for proto, apy in rate_map.items():
                        y = portfolio.accrue_yield(proto, apy, hours_elapsed)
                        if y > 0:
                            total_yield += y
                    if total_yield > 0:
                        click.echo(f"  Yield accrued: ${total_yield:.6f} ({hours_elapsed:.2f}h)")

                last_cycle_time = now

                # Compute allocation
                plan = compute_allocations(rates, gas, capital, scope)

                # Check rebalance triggers
                signals = check_rebalance_triggers(rates, plan, gas, scope, tracker)

                # Execute plan
                executor = Executor(
                    mode=mode, db=db, portfolio=portfolio,
                    scope=scope, gas_price=gas,
                )
                records = await executor.execute_plan(plan, rates)

                # Log summary
                successful = sum(1 for r in records if r.status in (ExecutionStatus.SUCCESS, ExecutionStatus.SIMULATED))
                health_label = "OK" if system_health.is_operational else "DEGRADED"
                click.echo(
                    f"  {len(rates)} rates | "
                    f"{plan.eligible_count} eligible | "
                    f"${portfolio.allocated_usd:,.0f} allocated | "
                    f"{successful}/{len(records)} executed | "
                    f"{len(signals)} signals | "
                    f"health: {health_label}"
                )

                for a_proto, a_amount in sorted(portfolio.positions.items()):
                    click.echo(f"  -> {a_proto}: ${a_amount:,.0f}")

                for s in signals:
                    if s.should_act:
                        click.echo(f"  !! {s.message}")

                click.echo()

            except Exception as e:
                logger.error(f"Cycle {cycle} failed: {e}")

            await asyncio.sleep(interval)
    finally:
        await db.close()


def main():
    cli()


if __name__ == "__main__":
    main()
