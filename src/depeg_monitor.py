"""USDC depeg monitor — fetches live USDC price for circuit breaker.

Primary: on-chain pool reads (WETH-USDC pool on Base).
Derives USDC/USD price from pool sqrtPriceX96 cross-referenced
against a known ETH/USD Chainlink feed.

Fallback: assumes $1.00 with consecutive failure tracking.
No external API dependencies (CoinGecko, DeFi Llama removed).
"""

import logging
import math
import time
from decimal import Decimal

import aiohttp

logger = logging.getLogger(__name__)

# ── On-chain constants ────────────────────────────────────────────

# WETH-USDC 0.05% pool on Base mainnet
POOL_ADDRESS = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
SLOT0_SELECTOR = "0x3850c7bd"

WETH_DECIMALS = 18
USDC_DECIMALS = 6

# Chainlink ETH/USD price feed on Base mainnet
CHAINLINK_ETH_USD = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"
# latestRoundData() selector
LATEST_ROUND_SELECTOR = "0xfeaf968c"
CHAINLINK_DECIMALS = 8

# Public Base RPCs (rotation on failure)
BASE_RPCS = [
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://base.drpc.org",
    "https://mainnet.base.org",
]

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Sanity bounds for USDC price — any value outside this range is treated
# as corrupt data (API compromise, parsing error, etc.)
USDC_PRICE_FLOOR = Decimal("0.50")
USDC_PRICE_CEILING = Decimal("1.50")

# Track consecutive failures — if too many, block new deposits
_consecutive_failures = 0
_last_successful_fetch = 0.0
MAX_CONSECUTIVE_FAILURES = 3
MAX_STALE_PRICE_SECONDS = 600  # 10 minutes


async def fetch_usdc_price(session: aiohttp.ClientSession) -> Decimal:
    """Fetch current USDC price from on-chain sources.

    Method: read WETH-USDC pool price (USDC per ETH) and Chainlink ETH/USD
    feed, then derive USDC/USD = chainlink_eth_usd / pool_eth_price.

    Returns Decimal price (e.g., 0.9995 or 1.0003).
    Returns 1.0 if all sources fail, but tracks consecutive failures.
    After MAX_CONSECUTIVE_FAILURES, returns a sentinel value (0.0)
    that the circuit breaker should treat as "unknown — block deposits".
    """
    global _consecutive_failures, _last_successful_fetch

    price = await _fetch_onchain(session)
    if price is not None:
        _consecutive_failures = 0
        _last_successful_fetch = time.time()
        return price

    _consecutive_failures += 1

    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        stale_seconds = time.time() - _last_successful_fetch if _last_successful_fetch else float("inf")
        if stale_seconds > MAX_STALE_PRICE_SECONDS:
            logger.error(
                f"USDC price source failed {_consecutive_failures}x consecutively, "
                f"last success {stale_seconds:.0f}s ago — returning unknown price (blocks deposits)"
            )
            return Decimal("0")  # Sentinel: circuit breaker should block deposits

    logger.warning("On-chain USDC price fetch failed — assuming $1.00 (fail-safe)")
    return Decimal("1.0")


def _validate_usdc_price(price: Decimal, source: str) -> Decimal | None:
    """Validate fetched USDC price is within sane bounds.

    Rejects clearly corrupt values (API compromise, parsing errors).
    USDC at $0.50 or $1.50 would already be a catastrophic event —
    anything beyond that is data corruption, not a real price.
    """
    if price < USDC_PRICE_FLOOR or price > USDC_PRICE_CEILING:
        logger.error(
            f"USDC price ${price:.4f} from {source} outside sane bounds "
            f"[${USDC_PRICE_FLOOR}-${USDC_PRICE_CEILING}] — rejecting as corrupt"
        )
        return None
    return price


async def _rpc_call(session: aiohttp.ClientSession, to: str, data: str) -> str | None:
    """Make an eth_call to Base RPCs with rotation on failure."""
    for i, rpc_url in enumerate(BASE_RPCS):
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
                "id": 1,
            }
            async with session.post(
                rpc_url, json=payload, timeout=HTTP_TIMEOUT,
            ) as resp:
                result = await resp.json()

            if "error" in result:
                raise RuntimeError(result["error"].get("message", str(result["error"])))

            return result["result"]

        except Exception as e:
            logger.warning("RPC %s failed for %s: %s", rpc_url, to[:10], e)
            if i == len(BASE_RPCS) - 1:
                return None
            continue
    return None


async def _fetch_onchain(session: aiohttp.ClientSession) -> Decimal | None:
    """Derive USDC/USD price from on-chain pool + Chainlink.

    1. Read WETH-USDC pool sqrtPriceX96 → compute pool_eth_price (USDC per ETH)
    2. Read Chainlink ETH/USD feed → chainlink_eth_usd (USD per ETH)
    3. USDC price = chainlink_eth_usd / pool_eth_price

    If USDC is worth exactly $1, these two ETH prices will match.
    If USDC depegs to $0.95, the pool will show higher USDC-per-ETH
    while Chainlink stays the same → ratio drops to 0.95.
    """
    try:
        # 1. Pool price: USDC per ETH
        pool_result = await _rpc_call(session, POOL_ADDRESS, SLOT0_SELECTOR)
        if not pool_result or len(pool_result) < 66:
            logger.warning("On-chain pool read failed or empty")
            return None

        sqrt_price_x96 = int(pool_result[:66], 16)
        if sqrt_price_x96 == 0:
            logger.warning("Pool sqrtPriceX96 is zero")
            return None

        sqrt_p = sqrt_price_x96 / (2**96)
        pool_eth_price = sqrt_p * sqrt_p * (10 ** (WETH_DECIMALS - USDC_DECIMALS))

        if pool_eth_price <= 0 or not math.isfinite(pool_eth_price):
            logger.warning(f"Invalid pool ETH price: {pool_eth_price}")
            return None

        # 2. Chainlink ETH/USD
        cl_result = await _rpc_call(session, CHAINLINK_ETH_USD, LATEST_ROUND_SELECTOR)
        if not cl_result or len(cl_result) < 130:
            logger.warning("Chainlink feed read failed or empty")
            return None

        # latestRoundData returns (roundId, answer, startedAt, updatedAt, answeredInRound)
        # answer is at offset 32-64 bytes (second word)
        answer_hex = cl_result[2 + 64:2 + 128]  # skip 0x, skip roundId
        chainlink_answer = int(answer_hex, 16)

        # Handle two's complement for signed int256
        if chainlink_answer >= 2**255:
            chainlink_answer -= 2**256

        if chainlink_answer <= 0:
            logger.warning(f"Invalid Chainlink ETH price: {chainlink_answer}")
            return None

        chainlink_eth_usd = float(chainlink_answer) / (10**CHAINLINK_DECIMALS)

        # Sanity check: ETH price should be reasonable ($100-$100,000)
        if chainlink_eth_usd < 100 or chainlink_eth_usd > 100_000:
            logger.warning(f"Chainlink ETH price out of range: ${chainlink_eth_usd:.2f}")
            return None

        # 3. Derive USDC price
        usdc_price = Decimal(str(chainlink_eth_usd / pool_eth_price))

        validated = _validate_usdc_price(usdc_price, "on-chain")
        if validated is not None:
            logger.info(
                f"USDC price (on-chain): ${validated:.4f} "
                f"(pool ETH=${pool_eth_price:,.2f}, Chainlink ETH=${chainlink_eth_usd:,.2f})"
            )
            return validated

    except Exception as e:
        logger.warning(f"On-chain USDC price derivation failed: {e}")
    return None
