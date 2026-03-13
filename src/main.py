"""Synthesis Yield Agent — CLI entry point.

Autonomous DeFi yield agent that scans Aave V3, Morpho Blue,
and Compound V3 for the best USDC supply rates on Base.

Usage:
    python -m src scan              # Scan rates across protocols
    python -m src scan --json       # JSON output for piping
    python -m src allocate          # Show optimal allocation plan
    python -m src allocate --capital 50000  # Custom capital amount
    python -m src run               # Start the yield agent loop
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
import click

from src.config import load_config, load_spending_scope
from src.data.aggregator import fetch_validated_rates
from src.data.gas import fetch_gas_onchain
from src.models import Chain, GasPrice
from src.strategy.allocator import compute_allocations
from src.strategy.rebalancer import check_rebalance_triggers, RebalanceTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yield-agent")


@click.group()
def cli():
    """Synthesis Yield Agent — autonomous DeFi yield optimization."""
    pass


@cli.command()
@click.option("--chain", default="base", help="Chain to scan (base/ethereum/arbitrum)")
@click.option("--json-output", "use_json", is_flag=True, help="Output as JSON")
def scan(chain: str, use_json: bool):
    """Scan current USDC yield rates across protocols."""
    asyncio.run(_scan(chain, use_json))


async def _scan(chain_name: str, use_json: bool):
    """Fetch and display cross-validated yield rates."""
    chain_map = {"base": Chain.BASE, "ethereum": Chain.ETHEREUM, "arbitrum": Chain.ARBITRUM}
    chain = chain_map.get(chain_name.lower())
    if not chain:
        click.echo(f"Unknown chain: {chain_name}. Use: base, ethereum, arbitrum")
        sys.exit(1)

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

    # Sort by APY descending
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
    chain_map = {"base": Chain.BASE, "ethereum": Chain.ETHEREUM, "arbitrum": Chain.ARBITRUM}
    chain = chain_map.get(chain_name.lower())
    if not chain:
        click.echo(f"Unknown chain: {chain_name}. Use: base, ethereum, arbitrum")
        sys.exit(1)

    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)

    async with aiohttp.ClientSession() as session:
        rates = await fetch_validated_rates(
            http_session=session,
            rpc_url=rpc_url,
            chain=chain,
        )

    # Try to get on-chain gas, fall back to Base defaults
    try:
        from web3 import AsyncWeb3
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        gas = await fetch_gas_onchain(w3)
    except Exception:
        gas = None

    if not gas:
        gas = GasPrice(
            base_fee_gwei=Decimal("0.01"),
            priority_fee_gwei=Decimal("0.001"),
            source="default-base",
        )

    plan = compute_allocations(
        rates=rates,
        gas_price=gas,
        total_capital_usd=capital,
        scope=scope,
        hold_days=hold_days,
    )

    # Check rebalance triggers
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

    # Protocol analysis
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

    # Allocation
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

    # Rebalance signals
    if signals:
        click.echo()
        click.echo("  Rebalance Signals:")
        click.echo(f"  {'-'*70}")
        for s in signals:
            icon = {"critical": "!!!", "warning": " ! ", "info": " i "}[s.severity]
            action = "ACT NOW" if s.should_act else "MONITOR"
            click.echo(f"  [{icon}] [{action}] {s.message}")
    click.echo()


@cli.command()
@click.option("--interval", default=900, help="Scan interval in seconds (default: 900)")
@click.option("--capital", default=10000, type=float, help="Total capital in USD")
def run(interval: int, capital: float):
    """Start the yield agent loop (scan + allocate + rebalance)."""
    asyncio.run(_run(interval, Decimal(str(capital))))


async def _run(interval: int, capital: Decimal):
    """Main agent loop — scan, score, allocate, monitor rebalancing triggers."""
    config = load_config()
    rpc_url = config.get("rpc_url", "https://mainnet.base.org")
    scope = load_spending_scope(config)
    tracker = RebalanceTracker(
        rate_diff_threshold=Decimal(str(config.get("rebalancing", {}).get("rate_diff_threshold", 0.01))),
        rate_diff_sustain_hours=config.get("rebalancing", {}).get("rate_diff_sustain_hours", 6),
    )

    click.echo(f"Yield agent starting — ${capital:,.0f} capital, {interval}s interval")
    click.echo("Press Ctrl+C to stop.\n")

    cycle = 0
    while True:
        cycle += 1
        click.echo(f"--- Cycle {cycle} ({datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')}) ---")

        try:
            async with aiohttp.ClientSession() as session:
                rates = await fetch_validated_rates(
                    http_session=session,
                    rpc_url=rpc_url,
                    chain=Chain.BASE,
                )

            # Gas
            try:
                from web3 import AsyncWeb3
                w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
                gas = await fetch_gas_onchain(w3)
            except Exception:
                gas = None

            if not gas:
                gas = GasPrice(
                    base_fee_gwei=Decimal("0.01"),
                    priority_fee_gwei=Decimal("0.001"),
                    source="default-base",
                )

            # Record rates for tracking
            tracker.record_rates(rates)

            # Compute allocation
            plan = compute_allocations(rates, gas, capital, scope)

            # Check rebalance triggers
            signals = check_rebalance_triggers(rates, plan, gas, scope, tracker)

            # Log summary
            click.echo(
                f"  {len(rates)} rates | "
                f"{plan.eligible_count} eligible | "
                f"${plan.total_allocated_usd:,.0f} allocated | "
                f"{len(signals)} signals"
            )

            for a in plan.allocations:
                click.echo(f"  -> {a.protocol.value}: ${a.amount_usd:,.0f} ({a.target_pct:.1%})")

            for s in signals:
                if s.should_act:
                    click.echo(f"  !! {s.message}")

            click.echo()

        except Exception as e:
            logger.error(f"Cycle {cycle} failed: {e}")

        await asyncio.sleep(interval)


def main():
    cli()


if __name__ == "__main__":
    main()
