"""Uniswap pool analytics — fee APY from V3/V2 pools on Base.

Fetches pool data from DeFi Llama's yield aggregator and calculates
fee-based APY so the AI brain can compare LP yield vs lending yield.

Data source: DeFi Llama yields API (free, no auth, aggregates on-chain data).

Level 1: Read-only analytics (no LP positions).
Level 2 (future): Actual LP position management via NonfungiblePositionManager.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
DEFILLAMA_URL = "https://yields.llama.fi/pools"

# Minimum TVL to consider a pool meaningful
MIN_POOL_TVL = Decimal("100000")  # $100k

# Sanity cap on APY (anything higher is likely rewards/incentives, not fees)
MAX_APY_SANITY = Decimal("2.0")  # 200%


@dataclass
class UniswapPool:
    """A Uniswap pool with fee analytics."""
    pool_id: str               # DeFi Llama pool ID
    pair_symbol: str           # e.g., "WETH-USDC"
    project: str               # "uniswap-v3" or "uniswap-v2"
    tvl_usd: Decimal
    apy_base: Decimal          # Fee-based APY (from trading volume)
    apy_reward: Decimal        # Reward APY (liquidity mining, etc.)
    apy_total: Decimal         # Total APY (base + reward)
    il_risk: str               # "no", "yes" — impermanent loss risk
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    @property
    def fee_apy(self) -> Decimal:
        """Fee-based APY only (excludes reward incentives)."""
        return self.apy_base

    @property
    def is_usdc_pair(self) -> bool:
        return "USDC" in self.pair_symbol.upper()


async def _fetch_defillama_pools(
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Fetch all pools from DeFi Llama."""
    async with session.get(DEFILLAMA_URL, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("data", [])


def _parse_pool(raw: dict) -> UniswapPool | None:
    """Parse a raw DeFi Llama pool into a UniswapPool."""
    try:
        # S44-M1: Validate numeric fields are finite before Decimal conversion
        raw_tvl = float(raw.get("tvlUsd", 0) or 0)
        raw_apy_base = float(raw.get("apyBase", 0) or 0)
        raw_apy_reward = float(raw.get("apyReward", 0) or 0)
        raw_apy_total = float(raw.get("apy", 0) or 0)

        if not all(math.isfinite(v) for v in (raw_tvl, raw_apy_base, raw_apy_reward, raw_apy_total)):
            logger.warning("Non-finite value in pool data: tvl=%s apy=%s", raw_tvl, raw_apy_total)
            return None

        tvl = Decimal(str(raw_tvl))
        if tvl < MIN_POOL_TVL:
            return None

        apy_base = Decimal(str(raw_apy_base)) / Decimal("100")
        apy_reward = Decimal(str(raw_apy_reward)) / Decimal("100")
        apy_total = Decimal(str(raw_apy_total)) / Decimal("100")

        # Sanity cap
        if apy_total > MAX_APY_SANITY:
            apy_total = MAX_APY_SANITY
        if apy_base > MAX_APY_SANITY:
            apy_base = MAX_APY_SANITY

        return UniswapPool(
            pool_id=raw.get("pool", ""),
            pair_symbol=raw.get("symbol", "UNKNOWN"),
            project=raw.get("project", ""),
            tvl_usd=tvl,
            apy_base=apy_base,
            apy_reward=apy_reward,
            apy_total=apy_total,
            il_risk="yes" if raw.get("ilRisk") == "yes" else "no",
        )
    except (TypeError, ValueError) as e:
        logger.warning("Failed to parse pool: %s", e)
        return None


# ── Public API ───────────────────────────────────────────────

async def fetch_uniswap_pools(
    session: aiohttp.ClientSession,
    chain: str = "Base",
    usdc_only: bool = False,
    min_tvl: Decimal | None = None,
) -> list[UniswapPool]:
    """Fetch Uniswap V3/V2 pools on a chain from DeFi Llama.

    Args:
        session: aiohttp session.
        chain: Chain name ("Base", "Ethereum", "Arbitrum").
        usdc_only: If True, only return USDC-paired pools.
        min_tvl: Override minimum TVL filter.

    Returns:
        List of UniswapPool sorted by TVL descending.
    """
    effective_min_tvl = min_tvl if min_tvl is not None else MIN_POOL_TVL

    raw_pools = await _fetch_defillama_pools(session)

    pools = []
    for raw in raw_pools:
        # Filter: chain + uniswap project
        if raw.get("chain") != chain:
            continue
        project = raw.get("project", "").lower()
        if "uniswap" not in project:
            continue

        pool = _parse_pool(raw)
        if pool is None:
            continue
        if pool.tvl_usd < effective_min_tvl:
            continue
        if usdc_only and not pool.is_usdc_pair:
            continue

        pools.append(pool)

    # Sort by TVL descending
    pools.sort(key=lambda p: p.tvl_usd, reverse=True)

    for p in pools[:10]:
        logger.info(
            "Uniswap | %s | %s | %s APY (base: %s) | $%s TVL",
            p.pair_symbol, p.project,
            f"{p.apy_total:.2%}", f"{p.apy_base:.2%}",
            f"{p.tvl_usd:,.0f}",
        )

    logger.info(
        "Uniswap pools on %s: %d total, %d with TVL > $%s",
        chain, len(pools), len([p for p in pools if p.tvl_usd >= effective_min_tvl]),
        f"{effective_min_tvl:,.0f}",
    )

    return pools


async def fetch_usdc_pools(
    session: aiohttp.ClientSession,
    chain: str = "Base",
) -> list[UniswapPool]:
    """Fetch USDC-paired Uniswap pools (for yield comparison with lending)."""
    return await fetch_uniswap_pools(session, chain=chain, usdc_only=True)


async def get_best_pool_apy(
    session: aiohttp.ClientSession,
    chain: str = "Base",
) -> tuple[Decimal, UniswapPool | None]:
    """Get the best fee APY from USDC Uniswap pools.

    Returns (best_fee_apy, best_pool) for comparison with lending protocols.
    Uses fee APY only (apy_base), not reward incentives.
    """
    pools = await fetch_usdc_pools(session, chain=chain)
    if not pools:
        return Decimal("0"), None
    # Sort by fee APY (base), not total — we want sustainable yield
    best = max(pools, key=lambda p: p.apy_base)
    return best.apy_base, best


def format_pool_summary(pools: list[UniswapPool], limit: int = 5) -> str:
    """Format pool data for AI reasoning context."""
    if not pools:
        return "No Uniswap pools available."

    lines = ["Uniswap LP Fee Yields (Base):"]
    for p in pools[:limit]:
        il_tag = " [IL risk]" if p.il_risk == "yes" else ""
        lines.append(
            f"  {p.pair_symbol:<20} {p.project:<14} "
            f"{p.apy_base:>6.2%} fee APY  "
            f"${p.tvl_usd:>12,.0f} TVL"
            f"{il_tag}"
        )
    lines.append(
        f"  NOTE: LP yields come with impermanent loss risk. "
        f"Compare with lending yields (no IL) before recommending."
    )
    return "\n".join(lines)
