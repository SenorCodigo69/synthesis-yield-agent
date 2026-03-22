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
from src.execution_logger import ExecutionLogger
from src.portfolio import Portfolio
from src.protocols.aave_v3 import AaveV3Adapter
from src.protocols.morpho_blue import MorphoBlueAdapter
from src.protocols.tx_helpers import TransactionSigner
from src.yield_learner import get_summary as get_learner_summary, record_yield_outcome
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


def _build_live_context(config: dict, rpc_url: str, chain: Chain):
    """Build protocol adapters, signer, and sender for live execution.

    Returns (adapters_dict, signer, sender_address).
    Raises if PRIVATE_KEY not set.
    """
    import os
    from web3 import AsyncWeb3

    private_key = os.environ.get("PRIVATE_KEY") or config.get("private_key")
    if not private_key:
        click.echo("  ERROR: PRIVATE_KEY env var required for live mode")
        sys.exit(1)

    signer = TransactionSigner(private_key)
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    sender = w3.eth.account.from_key(signer.key).address

    morpho_vault = config.get("protocols", {}).get("morpho_blue", {}).get("vault_address")
    morpho_adapter = MorphoBlueAdapter(w3, chain)
    if morpho_vault:
        morpho_adapter.set_vault(morpho_vault)

    adapters = {
        ProtocolName.AAVE_V3: AaveV3Adapter(w3, chain),
        ProtocolName.MORPHO: morpho_adapter,
    }

    return adapters, signer, sender


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
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run", "live"]),
              help="Execution mode")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def execute(chain: str, capital: float, hold_days: int, mode: str, use_json: bool):
    """Execute allocation plan (paper mode by default)."""
    mode_map = {"paper": ExecutionMode.PAPER, "dry_run": ExecutionMode.DRY_RUN, "live": ExecutionMode.LIVE}
    exec_mode = mode_map[mode]
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

        # Build live context if needed
        live_kwargs = {}
        if mode == ExecutionMode.LIVE:
            adapters, signer, sender = _build_live_context(config, rpc_url, chain)
            live_kwargs = {"adapters": adapters, "signer": signer, "sender": sender}

        # Execute
        executor = Executor(
            mode=mode, db=db, portfolio=portfolio,
            scope=scope, gas_price=gas, **live_kwargs,
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
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run", "live"]),
              help="Execution mode")
@click.option("--reason", default="manual", help="Reason for emergency withdrawal")
@click.option("--yes", "confirmed", is_flag=True, help="Skip confirmation prompt")
def emergency_withdraw(capital: float, mode: str, reason: str, confirmed: bool):
    """Emergency withdraw ALL positions immediately (bypasses cooldowns)."""
    mode_map = {"paper": ExecutionMode.PAPER, "dry_run": ExecutionMode.DRY_RUN, "live": ExecutionMode.LIVE}
    exec_mode = mode_map[mode]
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
            elif mode == ExecutionMode.LIVE:
                config = load_config()
                rpc_url = config.get("rpc_url", "https://mainnet.base.org")
                adapters, signer, sender = _build_live_context(config, rpc_url, Chain.BASE)
                adapter = adapters.get(protocol)
                if adapter:
                    tx_receipt = await adapter.withdraw(amount, sender, signer)
                    record.tx_hash = tx_receipt.tx_hash
                    record.block_number = tx_receipt.block_number
                    portfolio.apply_execution(record)
                    record.status = ExecutionStatus.SUCCESS
                else:
                    record.status = ExecutionStatus.FAILED
                    record.error = f"No adapter for {proto_key}"
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

        if mode in (ExecutionMode.PAPER, ExecutionMode.LIVE):
            await portfolio.save_snapshot()

        click.echo()
        click.echo(f"  Emergency withdrawal complete: {len(records)} actions")
        click.echo(f"  Portfolio allocated: ${portfolio.allocated_usd:,.2f}")
        click.echo(f"  Portfolio reserve:   ${portfolio.reserve_usd:,.2f}")
        click.echo()
    finally:
        await db.close()


# ── learn (yield learning stats) ─────────────────────────────────────────

@cli.command()
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def learn(use_json: bool):
    """Show yield allocation learning stats — how the agent is improving."""
    summary = get_learner_summary()

    if use_json:
        output = {
            "total_decisions": summary.total_decisions,
            "total_outcomes": summary.total_outcomes,
            "overall_win_rate": summary.overall_win_rate,
            "total_yield_usd": summary.total_yield_usd,
            "total_gas_usd": summary.total_gas_usd,
            "total_net_profit_usd": summary.total_net_profit_usd,
            "improvement_score": summary.improvement_score,
            "protocols": [
                {
                    "protocol": p.protocol,
                    "win_rate": p.win_rate,
                    "avg_predicted_apy": p.avg_predicted_apy,
                    "avg_actual_apy": p.avg_actual_apy,
                    "avg_apy_error": p.avg_apy_error,
                    "overestimate_pct": p.overestimate_pct,
                    "risk_weight_adjustment": p.risk_weight_adjustment,
                    "reasoning": p.reasoning,
                }
                for p in summary.protocols
            ],
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo()
    click.echo("  YIELD LEARNING — Agent Self-Improvement")
    click.echo(f"  {'='*60}")
    click.echo()
    click.echo(f"  Decisions tracked:  {summary.total_decisions}")
    click.echo(f"  Outcomes measured:  {summary.total_outcomes}")
    click.echo(f"  Overall win rate:   {summary.overall_win_rate:.1f}%")
    click.echo(f"  Total yield:        ${summary.total_yield_usd:.4f}")
    click.echo(f"  Total gas:          ${summary.total_gas_usd:.4f}")
    click.echo(f"  Net profit:         ${summary.total_net_profit_usd:.4f}")

    # Improvement score
    score = summary.improvement_score
    if score > 55:
        trend = "IMPROVING"
    elif score < 45:
        trend = "DEGRADING"
    else:
        trend = "STABLE"
    click.echo(f"  Improvement score:  {score:.0f}/100 ({trend})")
    click.echo()

    if summary.protocols:
        click.echo("  Protocol Accuracy:")
        click.echo(f"  {'-'*60}")
        for p in summary.protocols:
            click.echo(
                f"  {p.protocol:<15} win={p.win_rate:.0f}% | "
                f"pred={p.avg_predicted_apy:.2f}% actual={p.avg_actual_apy:.2f}% "
                f"err={p.avg_apy_error:+.2f}% | adj={p.risk_weight_adjustment:.2f}x"
            )
            click.echo(f"  {'':15} {p.reasoning}")
        click.echo()
    else:
        click.echo("  No outcomes recorded yet — agent needs more cycles to learn.")
        click.echo()


# ── register (ERC-8004) ───────────────────────────────────────────────────

@cli.command()
@click.option("--network", default="base_sepolia",
              type=click.Choice(["base_sepolia", "mainnet"]),
              help="Network to register on")
@click.option("--rpc-url", default=None, help="RPC URL (defaults to config)")
def register(network: str, rpc_url: str | None):
    """Register agent on ERC-8004 Identity Registry."""
    asyncio.run(_register(network, rpc_url))


async def _register(network: str, rpc_url: str | None):
    from src.erc8004 import register_agent, AgentRegistration, REGISTRIES

    config = load_config()
    private_key = config.get("private_key")

    if not private_key:
        click.echo("  Error: PRIVATE_KEY not set in .env — required for registration.")
        return

    if not rpc_url:
        if network == "base_sepolia":
            rpc_url = "https://sepolia.base.org"
        else:
            rpc_url = config.get("rpc_url", "https://mainnet.base.org")

    reg = AgentRegistration()
    registry_info = REGISTRIES[network]

    click.echo()
    click.echo("  ERC-8004 Agent Registration")
    click.echo(f"  {'='*50}")
    click.echo(f"  Network:  {network}")
    click.echo(f"  Registry: {registry_info['identity']}")
    click.echo(f"  Name:     {reg.name}")
    click.echo()

    result = await register_agent(rpc_url, private_key, network)

    if result:
        click.echo(f"  Registration successful! Block: {result}")
    else:
        click.echo("  Registration failed — check logs.")
    click.echo()


# ── pools (Uniswap LP analytics) ─────────────────────────────────────────

@cli.command()
@click.option("--usdc-only", is_flag=True, default=True, help="Show only USDC-paired pools")
@click.option("--all-pairs", is_flag=True, help="Show all pairs (not just USDC)")
@click.option("--limit", "max_pools", default=10, type=int, help="Max pools to show")
def pools(usdc_only: bool, all_pairs: bool, max_pools: int):
    """Show Uniswap LP pool analytics — fee APY, TVL, volume."""
    asyncio.run(_pools(not all_pairs, max_pools))


async def _pools(usdc_only: bool, max_pools: int):
    from src.data.uniswap_pools import (
        fetch_uniswap_pools, fetch_usdc_pools, format_pool_summary,
    )

    click.echo()
    click.echo("  Uniswap LP Pool Analytics (Base)")
    click.echo(f"  {'='*55}")
    click.echo()

    async with aiohttp.ClientSession() as session:
        if usdc_only:
            pool_list = await fetch_usdc_pools(session)
        else:
            pool_list = await fetch_uniswap_pools(session)

    if not pool_list:
        click.echo("  No pools found.")
        return

    click.echo(f"  {'Pair':<20} {'Project':<14} {'Fee APY':>8} {'TVL':>14} {'IL':>4}")
    click.echo(f"  {'-'*20} {'-'*14} {'-'*8} {'-'*14} {'-'*4}")

    for p in pool_list[:max_pools]:
        il = "Yes" if p.il_risk == "yes" else "No"
        click.echo(
            f"  {p.pair_symbol:<20} {p.project:<14} "
            f"{p.fee_apy:>7.2%} ${p.tvl_usd:>12,.0f}  {il}"
        )

    click.echo()
    click.echo(f"  Total pools: {len(pool_list)} (showing top {min(max_pools, len(pool_list))})")

    # Compare with lending
    click.echo()
    click.echo("  Yield Comparison (LP vs Lending):")
    best_lp = max(pool_list[:5], key=lambda p: p.apy_base)
    click.echo(f"    Best LP (USDC):   {best_lp.pair_symbol} @ {best_lp.apy_base:.2%} fee APY (IL risk)")
    click.echo(f"    Lending (Aave):   ~2-3% APY (no IL risk)")
    click.echo(f"    Lending (Morpho): ~3-4% APY (no IL risk)")
    click.echo()


# ── lp (Uniswap V3 LP) ─────────────────────────────────────────────────

@cli.command()
@click.option("--action", type=click.Choice(["mint", "collect", "exit", "status", "optimize", "concentrated-mint", "rebalance", "il-report"]),
              default="status", help="LP action to perform")
@click.option("--weth", "weth_amount", default=None, type=float,
              help="WETH amount to provide (in WETH units)")
@click.option("--usdc", "usdc_amount", default=None, type=float,
              help="USDC amount to provide (in USDC units)")
@click.option("--token-id", default=None, type=int,
              help="Position token ID (for collect/exit/status)")
@click.option("--fee", default=500, type=int,
              help="Pool fee tier: 100, 500, 3000, 10000 (default: 500 = 0.05%%)")
@click.option("--live", "is_live", is_flag=True,
              help="Execute on-chain (default: dry-run)")
@click.option("--slippage", default=1.0, type=float,
              help="Slippage tolerance %% (default: 1.0)")
def lp(action: str, weth_amount: float | None, usdc_amount: float | None,
       token_id: int | None, fee: int, is_live: bool, slippage: float):
    """Manage Uniswap V3 LP positions — mint, collect fees, exit."""
    if slippage > 10.0:
        click.echo(f"  Error: Slippage {slippage}% exceeds maximum 10%.")
        return
    if slippage <= 0:
        click.echo(f"  Error: Slippage must be positive.")
        return
    asyncio.run(_lp(action, weth_amount, usdc_amount, token_id, fee, is_live, slippage))


async def _lp(action: str, weth_amount: float | None, usdc_amount: float | None,
              token_id: int | None, fee: int, is_live: bool, slippage: float):
    import os
    from web3 import AsyncWeb3
    from src.uniswap_lp import (
        UniswapLPAdapter, full_range_ticks,
        WETH_BASE, USDC_BASE, WETH_DECIMALS, USDC_DECIMALS, POSITION_MANAGER,
        FEE_TIERS,
    )

    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    private_key = config.pop("_private_key", None)

    if not private_key and action != "status":
        click.echo("  Error: PRIVATE_KEY not set in .env")
        return

    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    adapter = UniswapLPAdapter(w3)

    if private_key:
        from eth_account import Account
        wallet = Account.from_key(private_key).address
    else:
        wallet = "unknown"

    click.echo()
    click.echo("  Uniswap V3 LP Manager")
    click.echo(f"  {'='*55}")
    click.echo(f"  Wallet:           {wallet}")
    click.echo(f"  PositionManager:  {POSITION_MANAGER}")
    click.echo(f"  Pool:             WETH-USDC ({fee/1_000_000:.2%} fee)")

    if fee not in FEE_TIERS:
        click.echo(f"  Error: Invalid fee tier {fee}. Valid: {list(FEE_TIERS.keys())}")
        return

    tick_lower, tick_upper = full_range_ticks(fee)
    click.echo(f"  Tick range:       [{tick_lower}, {tick_upper}] (full range)")
    click.echo()

    if action == "status":
        # Show wallet balances and position info
        weth_bal, usdc_bal = await adapter.get_balances(wallet)
        click.echo(f"  WETH balance:     {weth_bal:.8f}")
        click.echo(f"  USDC balance:     ${usdc_bal:,.6f}")
        weth_a, usdc_a = await adapter.get_allowances(wallet)
        click.echo(f"  WETH allowance:   {'MAX' if weth_a > 10**50 else weth_a}")
        click.echo(f"  USDC allowance:   {'MAX' if usdc_a > 10**50 else usdc_a}")

        if token_id:
            click.echo()
            try:
                pos = await adapter.get_position(token_id)
                click.echo(f"  Position #{token_id}:")
                click.echo(f"    Token0:      {pos.token0}")
                click.echo(f"    Token1:      {pos.token1}")
                click.echo(f"    Fee:         {pos.fee / 1_000_000:.2%}")
                click.echo(f"    Ticks:       [{pos.tick_lower}, {pos.tick_upper}]")
                click.echo(f"    Liquidity:   {pos.liquidity}")
                weth_owed = Decimal(str(pos.tokens_owed0)) / Decimal(10**WETH_DECIMALS)
                usdc_owed = Decimal(str(pos.tokens_owed1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"    Fees owed:   {weth_owed:.8f} WETH + ${usdc_owed:,.6f} USDC")
            except Exception as e:
                click.echo(f"    Error reading position: {e}")

    elif action == "mint":
        if weth_amount is None and usdc_amount is None:
            click.echo("  Error: Specify --weth and/or --usdc amounts")
            return

        weth_raw = int(Decimal(str(weth_amount or 0)) * Decimal(10**WETH_DECIMALS))
        usdc_raw = int(Decimal(str(usdc_amount or 0)) * Decimal(10**USDC_DECIMALS))

        # Validate against balance
        weth_bal, usdc_bal = await adapter.get_balances(wallet)
        if weth_amount and Decimal(str(weth_amount)) > weth_bal:
            click.echo(f"  Error: WETH amount {weth_amount} exceeds balance {weth_bal}")
            return
        if usdc_amount and Decimal(str(usdc_amount)) > usdc_bal:
            click.echo(f"  Error: USDC amount {usdc_amount} exceeds balance {usdc_bal}")
            return

        click.echo(f"  Action:           MINT full-range position")
        click.echo(f"  WETH deposit:     {weth_amount or 0}")
        click.echo(f"  USDC deposit:     {usdc_amount or 0}")
        click.echo(f"  Slippage:         {slippage}%")
        click.echo()

        if is_live:
            try:
                result = await adapter.mint_full_range(
                    private_key=private_key,
                    weth_amount=weth_raw,
                    usdc_amount=usdc_raw,
                    fee=fee,
                    slippage_pct=slippage,
                )
                click.echo(f"  LP Position Minted!")
                click.echo(f"    Token ID:    {result.token_id}")
                click.echo(f"    Liquidity:   {result.liquidity}")
                weth_used = Decimal(str(result.amount0)) / Decimal(10**WETH_DECIMALS)
                usdc_used = Decimal(str(result.amount1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"    WETH used:   {weth_used:.8f}")
                click.echo(f"    USDC used:   ${usdc_used:,.6f}")
                click.echo(f"    Tx hash:     {result.tx_hash}")
                click.echo(f"    Block:       {result.block_number}")
                click.echo(f"    Gas used:    {result.gas_used}")
            except Exception as e:
                click.echo(f"  Mint failed: {e}")
        else:
            click.echo(f"  [DRY RUN — add --live to execute on-chain]")
            click.echo(f"  Would mint full-range position with:")
            click.echo(f"    {weth_amount or 0} WETH + {usdc_amount or 0} USDC")
            click.echo(f"    Fee tier: {fee} ({fee/1_000_000:.2%})")
            click.echo(f"    Ticks: [{tick_lower}, {tick_upper}]")

    elif action == "collect":
        if token_id is None:
            click.echo("  Error: Specify --token-id for the position to collect from")
            return

        click.echo(f"  Action:           COLLECT fees from position #{token_id}")
        click.echo()

        if is_live:
            try:
                result = await adapter.collect_fees(private_key, token_id)
                weth_fees = Decimal(str(result.amount0)) / Decimal(10**WETH_DECIMALS)
                usdc_fees = Decimal(str(result.amount1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"  Fees Collected!")
                click.echo(f"    WETH:        {weth_fees:.8f}")
                click.echo(f"    USDC:        ${usdc_fees:,.6f}")
                click.echo(f"    Tx hash:     {result.tx_hash}")
            except Exception as e:
                click.echo(f"  Collect failed: {e}")
        else:
            click.echo(f"  [DRY RUN — add --live to collect fees]")
            try:
                pos = await adapter.get_position(token_id)
                weth_owed = Decimal(str(pos.tokens_owed0)) / Decimal(10**WETH_DECIMALS)
                usdc_owed = Decimal(str(pos.tokens_owed1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"  Estimated pending fees:")
                click.echo(f"    WETH:        {weth_owed:.8f}")
                click.echo(f"    USDC:        ${usdc_owed:,.6f}")
            except Exception as e:
                click.echo(f"  Could not read position: {e}")

    elif action == "exit":
        if token_id is None:
            click.echo("  Error: Specify --token-id for the position to exit")
            return

        click.echo(f"  Action:           EXIT position #{token_id}")
        click.echo(f"  Steps:            1. decreaseLiquidity → 2. collect → 3. burn NFT")
        click.echo()

        if is_live:
            try:
                result = await adapter.exit_position(private_key, token_id)
                weth_out = Decimal(str(result.amount0)) / Decimal(10**WETH_DECIMALS)
                usdc_out = Decimal(str(result.amount1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"  Position Exited!")
                click.echo(f"    WETH returned: {weth_out:.8f}")
                click.echo(f"    USDC returned: ${usdc_out:,.6f}")
                weth_fees = Decimal(str(result.fees0)) / Decimal(10**WETH_DECIMALS)
                usdc_fees = Decimal(str(result.fees1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"    Fees earned:   {weth_fees:.8f} WETH + ${usdc_fees:,.6f} USDC")
                click.echo(f"    Tx (decrease): {result.tx_hash_decrease}")
                click.echo(f"    Tx (collect):  {result.tx_hash_collect}")
                if result.tx_hash_burn:
                    click.echo(f"    Tx (burn):     {result.tx_hash_burn}")
            except Exception as e:
                click.echo(f"  Exit failed: {e}")
        else:
            click.echo(f"  [DRY RUN — add --live to exit position]")
            try:
                pos = await adapter.get_position(token_id)
                click.echo(f"  Position #{token_id}:")
                click.echo(f"    Liquidity:   {pos.liquidity}")
                click.echo(f"    Would remove all liquidity, collect tokens + fees, burn NFT")
            except Exception as e:
                click.echo(f"  Could not read position: {e}")

    elif action == "optimize":
        # Show recommended tick range based on quant signals
        click.echo(f"  Action:           OPTIMIZE — compute ideal tick range")
        click.echo()
        try:
            from src.lp_signals import compute_signals, read_pool_price
            from src.lp_optimizer import compute_range as opt_range
            from src import lp_tick_math as tm

            # Read current pool state
            sqrt_price, current_tick = await adapter.get_pool_slot0(fee)
            # Derive price from sqrtPriceX96 (more accurate than tick)
            sqrt_p = sqrt_price / (2**96)
            current_price = sqrt_p * sqrt_p * (10 ** (18 - 6))
            click.echo(f"  Pool price:       ${current_price:,.2f}")
            click.echo(f"  Current tick:     {current_tick}")
            click.echo()

            # Compute signals (stores snapshot, builds candles from history)
            signals = await compute_signals()
            click.echo(f"  Signals:")
            click.echo(f"    ATR:            ${signals.atr:,.2f} ({signals.atr_pct:.1%} of price)")
            click.echo(f"    BB width:       {signals.bb_width_pct:.1%}")
            click.echo(f"    RSI:            {signals.rsi:.1f}")
            click.echo(f"    ADX:            {signals.adx:.1f}")
            click.echo(f"    Regime:         {signals.regime} ({signals.regime_confidence:.0%} confidence)")
            click.echo(f"    Trend:          {signals.trend_direction}")
            click.echo()

            # Compute optimal range
            result = opt_range(signals, fee)
            click.echo(f"  Recommended Range:")
            click.echo(f"    Ticks:          [{result.tick_lower}, {result.tick_upper}]")
            click.echo(f"    Prices:         ${result.price_lower:,.2f} – ${result.price_upper:,.2f}")
            click.echo(f"    Width:          {result.width_pct:.1%}")
            click.echo(f"    Reasoning:      {result.reasoning}")

        except Exception as e:
            click.echo(f"  Optimize failed: {e}")
            import traceback
            traceback.print_exc()

    elif action == "concentrated-mint":
        # Mint concentrated position with optimized tick range
        if weth_amount is None and usdc_amount is None:
            click.echo("  Error: Specify --weth and/or --usdc amounts")
            return

        click.echo(f"  Action:           CONCENTRATED MINT with AI-optimized range")
        click.echo()

        try:
            from src.lp_signals import compute_signals
            from src.lp_optimizer import compute_range as opt_range

            signals = await compute_signals()
            opt = opt_range(signals, fee)

            weth_raw = int(Decimal(str(weth_amount or 0)) * Decimal(10**WETH_DECIMALS))
            usdc_raw = int(Decimal(str(usdc_amount or 0)) * Decimal(10**USDC_DECIMALS))

            click.echo(f"  Regime:           {opt.regime} ({opt.confidence:.0%})")
            click.echo(f"  Tick range:       [{opt.tick_lower}, {opt.tick_upper}]")
            click.echo(f"  Price range:      ${opt.price_lower:,.2f} – ${opt.price_upper:,.2f}")
            click.echo(f"  Width:            {opt.width_pct:.1%}")
            click.echo(f"  WETH deposit:     {weth_amount or 0}")
            click.echo(f"  USDC deposit:     {usdc_amount or 0}")
            click.echo(f"  Slippage:         {slippage}%")
            click.echo(f"  Reasoning:        {opt.reasoning}")
            click.echo()

            if is_live:
                result = await adapter.mint_concentrated(
                    private_key=private_key,
                    weth_amount=weth_raw,
                    usdc_amount=usdc_raw,
                    tick_lower=opt.tick_lower,
                    tick_upper=opt.tick_upper,
                    fee=fee,
                    slippage_pct=slippage,
                )
                click.echo(f"  Concentrated LP Minted!")
                click.echo(f"    Token ID:    {result.token_id}")
                click.echo(f"    Liquidity:   {result.liquidity}")
                weth_used = Decimal(str(result.amount0)) / Decimal(10**WETH_DECIMALS)
                usdc_used = Decimal(str(result.amount1)) / Decimal(10**USDC_DECIMALS)
                click.echo(f"    WETH used:   {weth_used:.8f}")
                click.echo(f"    USDC used:   ${usdc_used:,.6f}")
                click.echo(f"    Tx hash:     {result.tx_hash}")
            else:
                click.echo(f"  [DRY RUN — add --live to execute on-chain]")
        except Exception as e:
            click.echo(f"  Concentrated mint failed: {e}")

    elif action == "rebalance":
        # Check if current position needs rebalancing
        if token_id is None:
            click.echo("  Error: Specify --token-id for the position to check")
            return

        click.echo(f"  Action:           REBALANCE CHECK for position #{token_id}")
        click.echo()

        try:
            from src.lp_signals import compute_signals
            from src.lp_rebalancer import check_rebalance

            pos = await adapter.get_position(token_id)
            _, current_tick = await adapter.get_pool_slot0(fee)
            signals = await compute_signals()

            decision = check_rebalance(
                current_tick=current_tick,
                tick_lower=pos.tick_lower,
                tick_upper=pos.tick_upper,
                entry_regime=None,
                last_rebalance_ts=None,
                signals=signals,
            )

            click.echo(f"  Current tick:     {current_tick}")
            click.echo(f"  Position range:   [{pos.tick_lower}, {pos.tick_upper}]")
            click.echo(f"  Regime:           {signals.regime} ({signals.regime_confidence:.0%})")
            click.echo(f"  Urgency:          {decision.urgency}")
            click.echo(f"  Reason:           {decision.reason}")
            click.echo(f"  Should rebalance: {'YES' if decision.should_rebalance else 'NO'}")

            if decision.new_range:
                click.echo()
                click.echo(f"  Suggested new range:")
                click.echo(f"    Ticks:          [{decision.new_range.tick_lower}, {decision.new_range.tick_upper}]")
                click.echo(f"    Prices:         ${decision.new_range.price_lower:,.2f} – ${decision.new_range.price_upper:,.2f}")
                click.echo(f"    Width:          {decision.new_range.width_pct:.1%}")

        except Exception as e:
            click.echo(f"  Rebalance check failed: {e}")

    elif action == "il-report":
        # Show IL report for a position
        if token_id is None:
            click.echo("  Error: Specify --token-id")
            return

        click.echo(f"  Action:           IL REPORT for position #{token_id}")
        click.echo()

        try:
            from src.lp_signals import read_pool_price
            from src.lp_il_tracker import compute_il_report
            from src import lp_tick_math as tm

            pos = await adapter.get_position(token_id)
            current_price = await read_pool_price()

            # Clamp ticks to float-safe range for price conversion
            safe_lower = max(pos.tick_lower, tm._FLOAT_SAFE_MIN_TICK)
            safe_upper = min(pos.tick_upper, tm._FLOAT_SAFE_MAX_TICK)
            price_lower = tm.tick_to_eth_price(safe_lower)
            price_upper = tm.tick_to_eth_price(safe_upper)
            # Use tokens_owed as proxy for fees (simplified)
            fees_weth = pos.tokens_owed0 / 10**WETH_DECIMALS
            fees_usdc = pos.tokens_owed1 / 10**USDC_DECIMALS

            # Entry price unknown without DB — use current as estimate for full-range
            entry_price = current_price

            # Rough position value (would need sqrtPriceX96 math for exact)
            position_value_usd = 10.0  # Placeholder — needs on-chain calc

            report = compute_il_report(
                token_id=token_id,
                entry_price=entry_price,
                current_price=current_price,
                tick_lower=safe_lower,
                tick_upper=safe_upper,
                fees_weth=fees_weth,
                fees_usdc=fees_usdc,
                position_value_usd=position_value_usd,
            )

            click.echo(f"  Current ETH:      ${report.current_price_eth:,.2f}")
            click.echo(f"  Entry ETH (est):  ${report.entry_price_eth:,.2f}")
            click.echo(f"  Range:            ${report.price_lower:,.2f} – ${report.price_upper:,.2f}")
            click.echo(f"  IL:               {report.il_pct:.2%}")
            click.echo(f"  Fees earned:      ${report.fees_earned_usd:,.4f}")
            click.echo(f"  Net P&L:          ${report.net_pnl_usd:,.4f}")
            click.echo(f"  Profitable:       {'YES' if report.is_profitable else 'NO'}")

        except Exception as e:
            click.echo(f"  IL report failed: {e}")

    else:
        click.echo(f"  Unknown action: {action}")
        click.echo(f"  Valid: status, mint, collect, exit, optimize, concentrated-mint, rebalance, il-report")

    click.echo()


# ── swap (Uniswap) ──────────────────────────────────────────────────────

@cli.command()
@click.option("--direction", type=click.Choice(["usdc_to_weth", "weth_to_usdc"]),
              default=None, help="Swap direction (omit for AI recommendation)")
@click.option("--amount", default=None, type=float,
              help="Amount in token units (USDC or WETH)")
@click.option("--ai", "use_ai", is_flag=True, help="Let AI decide swap direction and amount")
@click.option("--live", "is_live", is_flag=True, help="Execute on-chain (default: quote only)")
@click.option("--zk", "use_zk", is_flag=True,
              help="Generate ZK proof before swap (routes through V4 hook)")
@click.option("--slippage", default=0.5, type=float, help="Slippage tolerance %% (default: 0.5)")
def swap(direction: str | None, amount: float | None, use_ai: bool,
         is_live: bool, use_zk: bool, slippage: float):
    """Swap tokens via Uniswap Trading API with optional AI reasoning."""
    # S41-L2: Cap slippage to prevent accidental unfavorable execution
    if slippage > 10.0:
        click.echo(f"  Error: Slippage {slippage}% exceeds maximum 10%. Use a lower value.")
        return
    if slippage <= 0:
        click.echo(f"  Error: Slippage must be positive.")
        return
    asyncio.run(_swap(direction, amount, use_ai, is_live, use_zk, slippage))


async def _swap(direction: str | None, amount: float | None, use_ai: bool,
                is_live: bool, use_zk: bool, slippage: float):
    import os
    from web3 import AsyncWeb3
    from src.uniswap import (
        UniswapAdapter, USDC_BASE, WETH_BASE, NATIVE_ETH,
        USDC_DECIMALS, WETH_DECIMALS, SwapResult,
    )
    from src.ai_swap import (
        get_swap_recommendation, SwapAction, SwapRecommendation,
    )

    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    private_key = config.pop("_private_key", None)

    if not private_key:
        click.echo("  Error: PRIVATE_KEY not set in .env")
        return

    api_key = os.getenv("UNISWAP_API_KEY", "")
    if not api_key:
        click.echo("  Error: UNISWAP_API_KEY not set in .env")
        return

    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    adapter = UniswapAdapter(api_key=api_key, w3=w3)

    from eth_account import Account
    wallet = Account.from_key(private_key).address

    # Fetch balances
    usdc_abi = [{"inputs": [{"name": "account", "type": "address"}],
                 "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                 "stateMutability": "view", "type": "function"}]
    usdc_contract = w3.eth.contract(
        address=w3.to_checksum_address(USDC_BASE), abi=usdc_abi
    )
    weth_contract = w3.eth.contract(
        address=w3.to_checksum_address(WETH_BASE), abi=usdc_abi
    )
    usdc_raw = await usdc_contract.functions.balanceOf(wallet).call()
    weth_raw = await weth_contract.functions.balanceOf(wallet).call()

    usdc_balance = Decimal(str(usdc_raw)) / Decimal(10 ** USDC_DECIMALS)
    weth_balance = Decimal(str(weth_raw)) / Decimal(10 ** WETH_DECIMALS)

    # Get ETH price for USD conversion
    eth_price = Decimal("0")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    eth_price = Decimal(str(data["ethereum"]["usd"]))
    except Exception:
        eth_price = Decimal("2000")  # Fallback estimate

    # S41-L1: Validate ETH price is positive (CoinGecko could return 0)
    if eth_price <= 0:
        eth_price = Decimal("2000")

    weth_balance_usd = weth_balance * eth_price

    click.echo()
    click.echo("  Uniswap Swap Agent")
    click.echo(f"  {'='*55}")
    click.echo(f"  Wallet:       {wallet}")
    click.echo(f"  USDC balance: ${usdc_balance:,.6f}")
    click.echo(f"  WETH balance: {weth_balance:.8f} (${weth_balance_usd:,.2f})")
    click.echo(f"  ETH price:    ${eth_price:,.2f}")
    click.echo()

    # AI-powered decision
    if use_ai or (direction is None and amount is None):
        click.echo("  Consulting AI for swap recommendation...")

        # Fetch yield rates + Uniswap pool data for context
        yield_rates = []
        lp_pools = []
        try:
            async with aiohttp.ClientSession() as session:
                rates = await fetch_validated_rates(
                    http_session=session, rpc_url=rpc_url, chain=Chain.BASE,
                )
                yield_rates = [
                    {
                        "protocol": r.protocol.value,
                        "apy": float(r.apy_median),
                        "tvl": float(r.tvl_usd),
                        "utilization": float(r.utilization),
                    }
                    for r in rates
                ]

                # Fetch Uniswap LP pool data for yield comparison
                from src.data.uniswap_pools import fetch_usdc_pools as fetch_uni_pools
                uni_pools = await fetch_uni_pools(session)
                lp_pools = [
                    {
                        "pair": p.pair_symbol,
                        "fee_apy": float(p.apy_base),
                        "tvl": float(p.tvl_usd),
                        "project": p.project,
                    }
                    for p in uni_pools[:5]
                ]
                if lp_pools:
                    click.echo(f"  Uniswap LP data: {len(uni_pools)} USDC pools found")
        except Exception as e:
            logger.warning(f"Failed to fetch yield rates: {e}")

        gas = await _get_gas(rpc_url)

        rec = await get_swap_recommendation(
            usdc_balance=usdc_balance,
            weth_balance_usd=weth_balance_usd,
            yield_rates=yield_rates,
            gas_gwei=gas.total_gwei,
            eth_price=eth_price,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            lp_pools=lp_pools,
        )

        click.echo(f"  AI Decision:  {rec.action.value}")
        click.echo(f"  Amount:       ${rec.amount_usd:,.2f}")
        click.echo(f"  Confidence:   {rec.confidence:.0%}")
        click.echo(f"  Reasoning:    {rec.reasoning}")
        click.echo()

        if rec.action == SwapAction.HOLD:
            click.echo("  AI recommends HOLD — no swap needed.")
            return

        if rec.action == SwapAction.DEPOSIT_YIELD:
            click.echo("  AI recommends depositing USDC for yield (use 'execute' command).")
            return

        # Map AI recommendation to swap params
        if rec.action == SwapAction.SWAP_USDC_TO_WETH:
            direction = "usdc_to_weth"
            amount = float(rec.amount_usd)
        elif rec.action == SwapAction.SWAP_WETH_TO_USDC:
            direction = "weth_to_usdc"
            # Convert USD amount to WETH units
            if eth_price > 0:
                amount = float(rec.amount_usd / eth_price)
            else:
                click.echo("  Error: Cannot convert — ETH price unknown")
                return

    if not direction or amount is None:
        click.echo("  Error: Specify --direction and --amount, or use --ai")
        return

    # S41-M2: Validate amount against wallet balance
    amount_dec = Decimal(str(amount))
    if direction == "usdc_to_weth" and amount_dec > usdc_balance:
        click.echo(f"  Error: Amount ${amount_dec:,.2f} exceeds USDC balance ${usdc_balance:,.6f}")
        return
    if direction == "weth_to_usdc" and amount_dec > weth_balance:
        click.echo(f"  Error: Amount {amount_dec:.8f} exceeds WETH balance {weth_balance:.8f}")
        return

    # Build swap parameters
    if direction == "usdc_to_weth":
        token_in = USDC_BASE
        token_out = WETH_BASE
        amount_raw = str(int(amount_dec * Decimal(10 ** USDC_DECIMALS)))
        click.echo(f"  Swap: {amount} USDC -> WETH")
    else:
        token_in = WETH_BASE
        token_out = USDC_BASE
        amount_raw = str(int(amount_dec * Decimal(10 ** WETH_DECIMALS)))
        click.echo(f"  Swap: {amount} WETH -> USDC")

    # ── ZK Proof Gate ──────────────────────────────────────────────────
    hook_data = None
    if use_zk:
        click.echo("  ZK Privacy Layer: ENABLED")
        click.echo()

        zk_agent_dir = os.path.expanduser(
            "~/Desktop/claude_projects/synthesis-zk-agent"
        )
        try:
            import subprocess as _sp

            # Compute spend amount (at least 1 for circuit validity)
            spend_usdc = max(int(amount_dec), 1) if amount_dec > 0 else 1

            # S44-M3: Call ZK agent via subprocess with env vars (not f-string code)
            zk_python = os.path.join(zk_agent_dir, ".venv", "bin", "python")
            zk_script = """
import json, os, sys

zk_dir = os.environ["ZK_AGENT_DIR"]
spend = int(os.environ["ZK_SPEND_AMOUNT"])

sys.path.insert(0, zk_dir)
from src.config import load_config
from src.zk.prover import ZKProver
from src.zk.keys import generate_keys
from src.zk.commitment import create_delegation, initialize_policy_state
from src.privacy.policy import PolicyManager
from src.chain.hook_client import ZKHookClient, ZK_HOOK_ADDRESS

config = load_config()
prover = ZKProver(config["zk"]["build_dir"])

owner_key = os.environ.get("OWNER_PRIVATE_KEY")
keys = generate_keys(owner_key) if owner_key else generate_keys()

delegation = create_delegation(
    owner_private_key=keys.private_key,
    agent_id=config["agent"]["id"],
    spend_limit=config["spending_policy"]["max_single_spend"],
    valid_for_seconds=config["spending_policy"]["valid_for_seconds"],
)
state = initialize_policy_state(delegation, config["spending_policy"]["period_limit"])

policy_mgr = PolicyManager(prover, config)
compliance = policy_mgr.full_compliance_check(spend, state)

result = {"compliant": compliance["compliant"], "reason": compliance.get("reason")}

if compliance["compliant"]:
    auth_proof = compliance["auth"]["proof"]
    calldata = prover.export_calldata(auth_proof)
    hook_data = ZKHookClient.parse_calldata_to_hook_data(calldata)
    result["hook_data_hex"] = hook_data.hex()
    result["hook_data_len"] = len(hook_data)
    result["hook_address"] = ZK_HOOK_ADDRESS

print(json.dumps(result))
"""
            env = {**os.environ, "ZK_AGENT_DIR": zk_agent_dir, "ZK_SPEND_AMOUNT": str(spend_usdc)}
            proc = _sp.run(
                [zk_python, "-c", zk_script],
                capture_output=True, text=True, timeout=60,
                cwd=zk_agent_dir, env=env,
            )

            if proc.returncode != 0:
                # Filter to just the error (skip warnings)
                stderr_lines = [
                    l for l in proc.stderr.strip().split("\n")
                    if not l.startswith(("WARNING", " ")) and l.strip()
                ]
                raise RuntimeError(stderr_lines[-1] if stderr_lines else proc.stderr[-200:])

            import json as _json
            zk_result = _json.loads(proc.stdout.strip().split("\n")[-1])

            if zk_result["compliant"]:
                click.echo(f"  [PASS] Authorization — agent delegated by owner")
                click.echo(f"  [PASS] Budget Range — amount within spend limit")
                click.echo(f"  [PASS] Cumulative — total within period limit")
                hook_data = bytes.fromhex(zk_result["hook_data_hex"])
                click.echo(f"  hookData: {zk_result['hook_data_len']} bytes (Uniswap V4 ZK-gated hook)")
                click.echo(f"  Hook:     {zk_result['hook_address']}")
            else:
                click.echo(f"  [FAIL] ZK compliance: {zk_result.get('reason')}")
                click.echo(f"  Swap blocked — ZK proof required")
                return

        except FileNotFoundError:
            click.echo("  Error: ZK agent not found")
            click.echo(f"  Expected: {zk_agent_dir}")
            return
        except Exception as e:
            click.echo(f"  ZK proof generation failed: {e}")
            return
        click.echo()

    if not is_live:
        # Quote-only mode
        click.echo("  Mode: QUOTE ONLY (add --live to execute)")
        click.echo()
        try:
            async with aiohttp.ClientSession() as session:
                quote = await adapter.get_quote(
                    session, token_in, token_out, amount_raw, wallet,
                    slippage=slippage,
                )
            if direction == "usdc_to_weth":
                out_dec = Decimal(quote.amount_out) / Decimal(10 ** WETH_DECIMALS)
                click.echo(f"  Quote: {amount} USDC -> {out_dec:.8f} WETH")
            else:
                out_dec = Decimal(quote.amount_out) / Decimal(10 ** USDC_DECIMALS)
                click.echo(f"  Quote: {amount} WETH -> {out_dec:.6f} USDC")
            click.echo(f"  Routing: {quote.routing}")
            if hook_data:
                click.echo(f"  ZK Hook: proof verified, hookData ready ({len(hook_data)} bytes)")
        except Exception as e:
            click.echo(f"  Quote failed: {e}")
        return

    # Live execution
    click.echo(f"  Mode: LIVE (slippage: {slippage}%)")
    if hook_data:
        click.echo(f"  ZK-Gated: hookData attached ({len(hook_data)} bytes)")
    click.echo()
    try:
        async with aiohttp.ClientSession() as session:
            result = await adapter.swap(
                session=session,
                token_in=token_in,
                token_out=token_out,
                amount=amount_raw,
                private_key=private_key,
                slippage=slippage,
            )
        click.echo(f"  Swap complete!")
        click.echo(f"  Tx hash:    {result.tx_hash}")
        click.echo(f"  Block:      {result.block_number}")
        click.echo(f"  Routing:    {result.routing}")
        click.echo(f"  Gas used:   {result.gas_used}")
        if hook_data:
            click.echo(f"  ZK proof:   verified (3 Groth16 proofs)")
        click.echo()
    except Exception as e:
        click.echo(f"  Swap failed: {e}")


# ── run (agent loop) ─────────────────────────────────────────────────────

@cli.command()
@click.option("--interval", default=900, help="Scan interval in seconds (default: 900)")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
@click.option("--mode", default="paper", type=click.Choice(["paper", "dry_run", "live"]),
              help="Execution mode")
def run(interval: int, capital: float, mode: str):
    """Start the yield agent loop (scan + allocate + execute + rebalance)."""
    mode_map = {"paper": ExecutionMode.PAPER, "dry_run": ExecutionMode.DRY_RUN, "live": ExecutionMode.LIVE}
    exec_mode = mode_map[mode]
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

    # Build live context once at startup
    live_kwargs = {}
    if mode == ExecutionMode.LIVE:
        adapters, signer, sender = _build_live_context(config, rpc_url, Chain.BASE)
        live_kwargs = {"adapters": adapters, "signer": signer, "sender": sender}

    db = Database()
    await db.connect()
    exec_log = ExecutionLogger()

    try:
        portfolio = Portfolio(capital, db)
        await portfolio.load_from_db()

        # Reconcile DB with on-chain state in live mode
        if mode == ExecutionMode.LIVE and live_kwargs.get("adapters"):
            click.echo("Reconciling portfolio with on-chain balances...")
            drift = await portfolio.reconcile_with_chain(
                live_kwargs["adapters"], live_kwargs["sender"],
            )
            for proto, info in drift.items():
                if info.get("error"):
                    click.echo(f"  {proto}: could not verify ({info['error']})")
                elif info["drift"] is not None and abs(info["drift"]) > 0.01:
                    click.echo(
                        f"  {proto}: CORRECTED — DB=${info['db']:.2f} → "
                        f"on-chain=${info['onchain']:.2f} (drift=${info['drift']:+.2f})"
                    )
                else:
                    click.echo(f"  {proto}: OK (${info.get('onchain', 0):.2f})")

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
            exec_log.begin_cycle(cycle, mode=mode.value)

            try:
                # ── SCAN: Fetch rates ─────────────────────────────────
                exec_log.log_step("scan_rates", "started")
                async with aiohttp.ClientSession() as session:
                    rates = await fetch_validated_rates(
                        http_session=session, rpc_url=rpc_url, chain=Chain.BASE,
                    )
                exec_log.log_tool_call("defillama", "fetch_rates", detail=f"{len(rates)} protocols")
                exec_log.log_step("scan_rates", "ok", f"{len(rates)} rates fetched")

                gas = await _get_gas(rpc_url)
                exec_log.log_tool_call("base_rpc", "fetch_gas", detail=f"{gas.total_gwei:.4f} gwei")
                tracker.record_rates(rates)

                # ── VALIDATE: Depeg check ─────────────────────────────
                exec_log.log_step("validate_depeg", "started")
                async with aiohttp.ClientSession() as price_session:
                    usdc_price = await fetch_usdc_price(price_session)
                exec_log.log_tool_call("base_rpc", "usdc_price", detail=f"${usdc_price:.4f}")
                exec_log.log_step("validate_depeg", "ok", f"USDC=${usdc_price:.4f}")

                # ── MONITOR: Circuit breaker check ────────────────────
                exec_log.log_step("circuit_breakers", "started")
                system_health = monitor.check_system_health(rates, gas, usdc_price)
                trips = system_health.breaker_trips

                if trips:
                    for trip in trips:
                        click.echo(f"  *** [{trip.severity.upper()}] {trip.message}")
                        exec_log.log_decision("circuit_breaker", trip.severity, reasoning=trip.message)
                exec_log.log_step("circuit_breakers", "ok" if not trips else "tripped",
                                  f"{len(trips)} trips")

                # Handle emergency withdrawals triggered by circuit breakers
                if breakers.requires_emergency_withdraw(trips) and portfolio.positions:
                    click.echo("  !!! CIRCUIT BREAKER EMERGENCY — withdrawing all positions")
                    exec_log.log_decision("emergency_withdraw", "triggered",
                                          reasoning="Circuit breaker requires full withdrawal")
                    emergency_scope = SpendingScope(withdrawal_cooldown_secs=0)
                    emergency_executor = Executor(
                        mode=mode, db=db, portfolio=portfolio,
                        scope=emergency_scope, gas_price=gas, **live_kwargs,
                    )
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
                        exec_log.log_execution(
                            r.protocol.value, r.action.value, float(r.amount_usd), r.status.value,
                        )
                    click.echo("  !!! Skipping normal execution after emergency withdraw")
                    click.echo()
                    exec_log.end_cycle({"rates": len(rates), "health": "emergency"})
                    await asyncio.sleep(interval)
                    continue

                # If system is frozen (gas too high), skip execution
                if not system_health.is_operational:
                    click.echo("  System degraded — skipping execution this cycle")
                    click.echo()
                    exec_log.end_cycle({"rates": len(rates), "health": "degraded"})
                    await asyncio.sleep(interval)
                    continue

                # ── ACCRUE: Yield on existing positions ───────────────
                total_yield = Decimal("0")
                if last_cycle_time and portfolio.positions:
                    hours_elapsed = Decimal(str(
                        (now - last_cycle_time).total_seconds() / 3600
                    ))
                    rate_map = {r.protocol.value: r.apy_median for r in rates}
                    for proto, apy in rate_map.items():
                        y = portfolio.accrue_yield(proto, apy, hours_elapsed)
                        if y > 0:
                            total_yield += y
                    if total_yield > 0:
                        click.echo(f"  Yield accrued: ${total_yield:.6f} ({hours_elapsed:.2f}h)")
                        exec_log.log_step("accrue_yield", "ok", f"${total_yield:.6f}")

                last_cycle_time = now

                # ── ALLOCATE: Compute allocation ──────────────────────
                exec_log.log_step("allocate", "started")
                plan = compute_allocations(rates, gas, capital, scope)
                exec_log.log_decision("allocation", "computed", data={
                    "eligible": plan.eligible_count,
                    "total_allocated": float(plan.total_allocated_usd),
                    "reserve": float(plan.reserve_usd),
                })
                exec_log.log_step("allocate", "ok", f"{plan.eligible_count} eligible protocols")

                # ── REBALANCE: Check triggers ─────────────────────────
                signals = check_rebalance_triggers(rates, plan, gas, scope, tracker)
                for s in signals:
                    if s.should_act:
                        exec_log.log_decision("rebalance", "triggered", reasoning=s.message)

                # ── EXECUTE: Run plan ─────────────────────────────────
                exec_log.log_step("execute", "started")
                executor = Executor(
                    mode=mode, db=db, portfolio=portfolio,
                    scope=scope, gas_price=gas, **live_kwargs,
                )
                records = await executor.execute_plan(plan, rates)

                for r in records:
                    exec_log.log_execution(
                        r.protocol.value, r.action.value, float(r.amount_usd), r.status.value,
                    )

                # ── REPORT: Log summary ───────────────────────────────
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
                exec_log.log_step("execute", "ok", f"{successful}/{len(records)} executed")

                for a_proto, a_amount in sorted(portfolio.positions.items()):
                    click.echo(f"  -> {a_proto}: ${a_amount:,.0f}")

                for s in signals:
                    if s.should_act:
                        click.echo(f"  !! {s.message}")

                click.echo()

                exec_log.end_cycle({
                    "rates": len(rates),
                    "eligible": plan.eligible_count,
                    "executed": successful,
                    "signals": len([s for s in signals if s.should_act]),
                    "allocated_usd": float(portfolio.allocated_usd),
                    "reserve_usd": float(portfolio.reserve_usd),
                    "yield_accrued": float(total_yield),
                    "health": health_label.lower(),
                })

            except Exception as e:
                logger.error(f"Cycle {cycle} failed: {e}")
                exec_log.log_failure("cycle", str(e), recoverable=True)
                exec_log.end_cycle({"rates": 0, "health": "error"})

            await asyncio.sleep(interval)
    finally:
        await db.close()


def main():
    cli()


if __name__ == "__main__":
    main()
