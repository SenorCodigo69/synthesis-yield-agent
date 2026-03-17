"""Uniswap AI Skills integration — swap planning + liquidity planning.

Integrates with the Uniswap AI Skills framework (github.com/Uniswap/uniswap-ai)
to generate swap/LP deep links, plan trades, and align with Uniswap's agent
ecosystem.

Our agent already executes swaps via the Trading API and manages LP via
NonfungiblePositionManager — this module adds Uniswap-standard planning
and deep link generation for compatibility with the broader agent ecosystem.

Skills used:
- swap-planner: Plan + generate deep links for token swaps
- liquidity-planner: Plan + generate deep links for LP positions (V2/V3/V4)
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────

UNISWAP_APP_BASE = "https://app.uniswap.org"

# Base mainnet token addresses
TOKENS = {
    "ETH": "NATIVE",
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

CHAIN_IDS = {
    "ethereum": 1,
    "base": 8453,
    "arbitrum": 42161,
    "optimism": 10,
    "polygon": 137,
    "unichain": 130,
}


# ── Swap Planner ──────────────────────────────────────────────


@dataclass
class SwapPlan:
    """Output of the swap planner skill."""
    input_token: str
    output_token: str
    amount: float
    chain: str
    chain_id: int
    deep_link: str
    summary: str
    warnings: list[str]


def plan_swap(
    input_token: str,
    output_token: str,
    amount: float,
    chain: str = "base",
) -> SwapPlan:
    """Plan a token swap and generate a Uniswap deep link.

    Implements the swap-planner skill from Uniswap AI.

    Args:
        input_token: Token symbol or address (e.g., "USDC", "ETH")
        output_token: Token symbol or address
        amount: Amount of input token
        chain: Chain name (default "base")

    Returns:
        SwapPlan with deep link and analysis.
    """
    chain_id = CHAIN_IDS.get(chain.lower())
    if chain_id is None:
        raise ValueError(f"Unknown chain: {chain}. Valid: {list(CHAIN_IDS.keys())}")

    # Resolve token addresses
    input_addr = TOKENS.get(input_token.upper(), input_token)
    output_addr = TOKENS.get(output_token.upper(), output_token)

    # Generate deep link (Uniswap app URL format)
    deep_link = (
        f"{UNISWAP_APP_BASE}/swap"
        f"?inputCurrency={quote(str(input_addr), safe='')}"
        f"&outputCurrency={quote(str(output_addr), safe='')}"
        f"&exactAmount={quote(str(amount), safe='')}"
        f"&chain={quote(chain.lower(), safe='')}"
    )

    # Warnings
    warnings = []
    if amount > 10000 and input_token.upper() == "USDC":
        warnings.append(f"Large swap (${amount:,.0f}) — check slippage and liquidity depth")
    if input_token.upper() == output_token.upper():
        warnings.append("Input and output tokens are the same")
    if chain.lower() == "ethereum":
        warnings.append("Ethereum mainnet — gas fees may be high")

    summary = (
        f"Swap {amount} {input_token} → {output_token} on {chain.title()} "
        f"(chain ID {chain_id})"
    )

    return SwapPlan(
        input_token=input_token,
        output_token=output_token,
        amount=amount,
        chain=chain,
        chain_id=chain_id,
        deep_link=deep_link,
        summary=summary,
        warnings=warnings,
    )


# ── Liquidity Planner ────────────────────────────────────────


@dataclass
class LPPlan:
    """Output of the liquidity planner skill."""
    token0: str
    token1: str
    fee_tier: int
    version: str  # "v3" or "v4"
    chain: str
    chain_id: int
    hook_address: str | None
    deep_link: str
    summary: str
    warnings: list[str]


def plan_liquidity(
    token0: str,
    token1: str,
    fee_tier: int = 500,
    chain: str = "base",
    version: str = "v3",
    hook_address: str | None = None,
) -> LPPlan:
    """Plan a liquidity position and generate a Uniswap deep link.

    Implements the liquidity-planner skill from Uniswap AI.

    Args:
        token0: First token symbol or address
        token1: Second token symbol or address
        fee_tier: Fee tier in hundredths of a bip (500 = 0.05%)
        chain: Chain name
        version: "v3" or "v4"
        hook_address: Optional V4 hook address

    Returns:
        LPPlan with deep link and analysis.
    """
    chain_id = CHAIN_IDS.get(chain.lower())
    if chain_id is None:
        raise ValueError(f"Unknown chain: {chain}")

    token0_addr = TOKENS.get(token0.upper(), token0)
    token1_addr = TOKENS.get(token1.upper(), token1)
    fee_pct = fee_tier / 1_000_000

    # Generate deep link
    deep_link = (
        f"{UNISWAP_APP_BASE}/add/{quote(str(token0_addr), safe='')}/{quote(str(token1_addr), safe='')}"
        f"/{fee_tier}"
        f"?chain={quote(chain.lower(), safe='')}"
    )
    if hook_address and version == "v4":
        deep_link += f"&hook={quote(str(hook_address), safe='')}"

    warnings = []
    if fee_tier not in [100, 500, 3000, 10000]:
        warnings.append(f"Non-standard fee tier: {fee_tier}")
    if version == "v4" and not hook_address:
        warnings.append("V4 selected but no hook address — position will use default behavior")
    if version == "v4" and hook_address:
        warnings.append(f"V4 hook at {hook_address[:10]}... — verify hook is audited")

    summary = (
        f"LP {token0}/{token1} ({fee_pct:.2%} fee) on {chain.title()} "
        f"({version.upper()}{' + hook' if hook_address else ''})"
    )

    return LPPlan(
        token0=token0,
        token1=token1,
        fee_tier=fee_tier,
        version=version,
        chain=chain,
        chain_id=chain_id,
        hook_address=hook_address,
        deep_link=deep_link,
        summary=summary,
        warnings=warnings,
    )


# ── AI-Enhanced Planning ──────────────────────────────────────


def plan_optimal_lp_with_signals(
    signals,  # LPSignals from lp_signals.py
    optimized_range,  # OptimizedRange from lp_optimizer.py
    hook_address: str | None = None,
) -> LPPlan:
    """Generate an LP plan using our quant signals + Uniswap skill format.

    Bridges our concentrated LP optimizer with the Uniswap AI Skills
    liquidity-planner format.
    """
    plan = plan_liquidity(
        token0="WETH",
        token1="USDC",
        fee_tier=500,
        chain="base",
        version="v4" if hook_address else "v3",
        hook_address=hook_address,
    )

    # Enrich with our signal analysis
    plan.warnings.extend([
        f"Regime: {signals.regime} ({signals.regime_confidence:.0%} confidence)",
        f"Recommended range: ${optimized_range.price_lower:,.0f}–${optimized_range.price_upper:,.0f} ({optimized_range.width_pct:.0%} width)",
        f"ATR: {signals.atr_pct:.1%} | RSI: {signals.rsi:.0f} | ADX: {signals.adx:.0f}",
    ])

    plan.summary = (
        f"AI-optimized LP WETH/USDC (0.05%) on Base — "
        f"{signals.regime} regime, {optimized_range.width_pct:.0%} width, "
        f"${optimized_range.price_lower:,.0f}–${optimized_range.price_upper:,.0f}"
    )

    return plan


# ── Multi-Chain Pool Discovery ────────────────────────────────


@dataclass
class PoolRecommendation:
    """A discovered pool with yield metrics."""
    chain: str
    token0: str
    token1: str
    fee_tier: int
    tvl_usd: float
    fee_apy: float
    volume_24h: float
    deep_link: str
    score: float  # Composite score (higher = better)


async def discover_best_pools(
    min_tvl: float = 100_000,
    min_volume: float = 10_000,
    chains: list[str] | None = None,
) -> list[PoolRecommendation]:
    """Discover the best LP pools across chains using DeFi Llama data.

    Scores pools by fee_apy * sqrt(tvl) to balance yield with safety.

    Args:
        min_tvl: Minimum TVL in USD
        min_volume: Minimum 24h volume
        chains: Chains to scan (default: Base only)

    Returns:
        Sorted list of pool recommendations (best first).
    """
    import aiohttp
    import math

    if chains is None:
        chains = ["base"]

    pools = []

    try:
        async with aiohttp.ClientSession() as session:
            # DeFi Llama pools endpoint (Uniswap V3)
            async with session.get(
                "https://yields.llama.fi/pools",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("DeFi Llama pools API failed: %d", resp.status)
                    return []
                data = await resp.json()

        for pool in data.get("data", []):
            # Filter: Uniswap V3 on target chains
            project = pool.get("project", "").lower()
            if "uniswap" not in project:
                continue

            chain = pool.get("chain", "").lower()
            if chain not in [c.lower() for c in chains]:
                continue

            tvl = pool.get("tvlUsd", 0) or 0
            apy = pool.get("apy", 0) or 0
            volume = pool.get("volumeUsd1d", 0) or 0

            if tvl < min_tvl or volume < min_volume:
                continue

            symbol = pool.get("symbol", "")
            tokens = symbol.split("-") if "-" in symbol else symbol.split("/")
            if len(tokens) < 2:
                continue

            # Extract fee tier from pool metadata
            fee_str = pool.get("poolMeta", "")
            fee_tier = 3000  # default
            if "0.01%" in str(fee_str):
                fee_tier = 100
            elif "0.05%" in str(fee_str):
                fee_tier = 500
            elif "0.3%" in str(fee_str):
                fee_tier = 3000
            elif "1%" in str(fee_str):
                fee_tier = 10000

            # Score: APY weighted by sqrt(TVL) — balances yield with safety
            score = apy * math.sqrt(tvl) / 1000 if tvl > 0 else 0

            token0 = tokens[0].strip()
            token1 = tokens[1].strip()

            deep_link = (
                f"{UNISWAP_APP_BASE}/add/"
                f"{TOKENS.get(token0.upper(), token0)}/"
                f"{TOKENS.get(token1.upper(), token1)}/"
                f"{fee_tier}?chain={chain}"
            )

            pools.append(PoolRecommendation(
                chain=chain,
                token0=token0,
                token1=token1,
                fee_tier=fee_tier,
                tvl_usd=tvl,
                fee_apy=apy,
                volume_24h=volume,
                deep_link=deep_link,
                score=score,
            ))

    except Exception as e:
        logger.error("Pool discovery failed: %s", e)
        return []

    # Sort by score (best first), cap at 50
    pools.sort(key=lambda p: p.score, reverse=True)
    return pools[:50]
