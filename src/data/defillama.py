"""DeFi Llama API client — yield rates, TVL, protocol data.

Free API, no auth required. Rate limit: be reasonable (~1 req/sec).
Docs: https://defillama.com/docs/api
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from src.models import Chain, DataSource, ProtocolName, YieldPool

logger = logging.getLogger(__name__)

BASE_URL = "https://yields.llama.fi"

# Verified slugs (March 12, 2026) — morpho is morpho-v1, NOT morpho-blue
PROTOCOL_SLUGS = {
    ProtocolName.AAVE_V3: "aave-v3",
    ProtocolName.MORPHO: "morpho-v1",
    ProtocolName.COMPOUND_V3: "compound-v3",
}

# Monitored AMM protocols — read-only, not eligible for allocation
# Aerodrome is Base-native (Velodrome fork), largest DEX on Base
MONITORED_AMM_SLUGS = {"aerodrome"}

# DeFi Llama uses title-case chain names
CHAIN_NAMES = {
    Chain.BASE: "Base",
    Chain.ETHEREUM: "Ethereum",
    Chain.ARBITRUM: "Arbitrum",
}


HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def fetch_all_pools(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all yield pools from DeFi Llama."""
    url = f"{BASE_URL}/pools"
    async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("data", [])


async def fetch_usdc_pools(
    session: aiohttp.ClientSession,
    chain: Chain = Chain.BASE,
    protocols: list[ProtocolName] | None = None,
) -> list[YieldPool]:
    """Fetch USDC yield pools for specified protocols on a chain.

    Returns validated YieldPool objects with rates, TVL, and utilization.
    """
    if protocols is None:
        protocols = list(ProtocolName)

    target_slugs = {PROTOCOL_SLUGS[p] for p in protocols}
    target_chain = CHAIN_NAMES[chain]

    all_pools = await fetch_all_pools(session)

    results = []
    for pool in all_pools:
        # Filter: must be USDC, on target chain, from target protocol
        symbol = pool.get("symbol", "")
        if "USDC" not in symbol.upper():
            continue
        if pool.get("chain") != target_chain:
            continue
        if pool.get("project") not in target_slugs:
            continue

        try:
            yield_pool = YieldPool(
                pool_id=pool.get("pool", ""),
                protocol=_slug_to_protocol(pool["project"]),
                chain=chain,
                symbol=symbol,
                apy_base=Decimal(str(pool.get("apyBase") or 0)),
                apy_reward=Decimal(str(pool.get("apyReward") or 0)),
                apy_total=Decimal(str(pool.get("apy") or 0)),
                tvl_usd=Decimal(str(pool.get("tvlUsd") or 0)),
                utilization=_extract_utilization(pool),
                source=DataSource.DEFILLAMA,
                timestamp=datetime.now(tz=timezone.utc),
            )
            results.append(yield_pool)
            logger.info(
                f"DeFi Llama | {yield_pool.protocol.value} | "
                f"{symbol} | APY: {yield_pool.apy_total:.2f}% | "
                f"TVL: ${yield_pool.tvl_usd:,.0f}"
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"Skipping malformed pool {pool.get('pool')}: {e}")

    logger.info(
        f"DeFi Llama: found {len(results)} USDC pools on {target_chain} "
        f"across {len(target_slugs)} protocols"
    )
    return results


async def fetch_aerodrome_pools(
    session: aiohttp.ClientSession,
    chain: Chain = Chain.BASE,
    min_tvl: int = 100_000,
    max_pools: int = 5,
) -> list[dict]:
    """Fetch top Aerodrome LP pools for monitoring (read-only).

    Returns simplified pool data — not YieldPool objects since these are
    AMM pools, not lending. Used for Base ecosystem awareness, not allocation.
    """
    all_pools = await fetch_all_pools(session)

    target_chain = CHAIN_NAMES[chain]
    results = []

    for pool in all_pools:
        if pool.get("project") not in MONITORED_AMM_SLUGS:
            continue
        if pool.get("chain") != target_chain:
            continue
        tvl = pool.get("tvlUsd") or 0
        if tvl < min_tvl:
            continue

        apy = pool.get("apy") or 0
        symbol = pool.get("symbol", "?")
        results.append({
            "pool_id": pool.get("pool", ""),
            "symbol": symbol,
            "apy_total": round(apy, 2),
            "apy_base": round(pool.get("apyBase") or 0, 2),
            "apy_reward": round(pool.get("apyReward") or 0, 2),
            "tvl_usd": round(tvl),
            "project": pool.get("project", ""),
        })

    # Sort by TVL descending, take top N
    results.sort(key=lambda p: p["tvl_usd"], reverse=True)
    results = results[:max_pools]

    for p in results:
        logger.info(
            f"Aerodrome | {p['symbol']} | "
            f"APY: {p['apy_total']:.2f}% (base: {p['apy_base']:.2f}% + rewards: {p['apy_reward']:.2f}%) | "
            f"TVL: ${p['tvl_usd']:,.0f}"
        )

    logger.info(f"Aerodrome: {len(results)} monitored pools on {target_chain} (read-only)")
    return results


async def fetch_protocol_tvl(
    session: aiohttp.ClientSession,
    protocol_slug: str,
) -> Decimal | None:
    """Fetch total TVL for a protocol from DeFi Llama."""
    url = f"https://api.llama.fi/tvl/{protocol_slug}"
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            tvl = await resp.json()
            return Decimal(str(tvl))
    except Exception as e:
        logger.warning(f"Failed to fetch TVL for {protocol_slug}: {e}")
        return None


def _slug_to_protocol(slug: str) -> ProtocolName:
    """Map DeFi Llama project slug to our ProtocolName enum."""
    for proto, s in PROTOCOL_SLUGS.items():
        if s == slug:
            return proto
    raise ValueError(f"Unknown protocol slug: {slug}")


def _extract_utilization(pool: dict) -> Decimal:
    """Extract utilization from pool data. DeFi Llama may include it
    in different fields depending on pool type."""
    # Some pools have direct utilization
    if pool.get("utilization") is not None:
        return Decimal(str(pool["utilization"]))
    # Lending pools often have borrowFactor or similar
    total_supply = pool.get("totalSupplyUsd") or pool.get("tvlUsd")
    total_borrow = pool.get("totalBorrowUsd")
    if total_supply and total_borrow:
        try:
            supply = Decimal(str(total_supply))
            borrow = Decimal(str(total_borrow))
            if supply > 0:
                return borrow / supply
        except Exception:
            pass
    return Decimal("0")
