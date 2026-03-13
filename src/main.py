"""Synthesis Yield Agent — CLI entry point.

Autonomous DeFi yield agent that scans Aave V3, Morpho Blue,
and Compound V3 for the best USDC supply rates on Base.

Usage:
    python -m src.main scan          # Scan rates across protocols
    python -m src.main scan --json   # JSON output for piping
    python -m src.main run           # Start the yield agent loop
"""

import asyncio
import json
import logging
import sys

import click

from src.config import load_config
from src.data.aggregator import fetch_validated_rates
from src.models import Chain

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
    import aiohttp

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
@click.option("--interval", default=900, help="Scan interval in seconds (default: 900)")
def run(interval: int):
    """Start the yield agent loop (scan + allocate)."""
    click.echo("Yield agent loop — not yet implemented (Day 2: strategy engine)")
    click.echo(f"Would scan every {interval}s and rebalance based on rates.")


def main():
    cli()


if __name__ == "__main__":
    main()
