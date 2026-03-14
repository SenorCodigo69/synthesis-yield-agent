"""USDC depeg monitor — fetches live USDC price for circuit breaker.

Uses CoinGecko free API (no key needed, ~30 req/min limit).
Falls back to DeFi Llama stablecoin endpoint.
"""

import logging
from decimal import Decimal

import aiohttp

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
DEFILLAMA_STABLES_URL = "https://stablecoins.llama.fi/stablecoinprices/current"

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def fetch_usdc_price(session: aiohttp.ClientSession) -> Decimal:
    """Fetch current USDC price from CoinGecko, fallback to DeFi Llama.

    Returns Decimal price (e.g., 0.9995 or 1.0003).
    Returns 1.0 if all sources fail (fail-safe, not fail-open — the
    circuit breaker treats 1.0 as "no depeg detected").
    """
    price = await _fetch_coingecko(session)
    if price is not None:
        return price

    price = await _fetch_defillama(session)
    if price is not None:
        return price

    logger.warning("All USDC price sources failed — assuming $1.00 (fail-safe)")
    return Decimal("1.0")


async def _fetch_coingecko(session: aiohttp.ClientSession) -> Decimal | None:
    """Fetch USDC price from CoinGecko free API."""
    try:
        params = {"ids": "usd-coin", "vs_currencies": "usd"}
        async with session.get(
            COINGECKO_URL, params=params, timeout=HTTP_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            price = data.get("usd-coin", {}).get("usd")
            if price is not None:
                result = Decimal(str(price))
                logger.info(f"USDC price (CoinGecko): ${result:.4f}")
                return result
    except Exception as e:
        logger.warning(f"CoinGecko USDC price fetch failed: {e}")
    return None


async def _fetch_defillama(session: aiohttp.ClientSession) -> Decimal | None:
    """Fetch USDC price from DeFi Llama stablecoins endpoint."""
    try:
        async with session.get(
            DEFILLAMA_STABLES_URL, timeout=HTTP_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            # DeFi Llama returns prices keyed by coingecko ID
            price = data.get("usd-coin", {}).get("price")
            if price is not None:
                result = Decimal(str(price))
                logger.info(f"USDC price (DeFi Llama): ${result:.4f}")
                return result
    except Exception as e:
        logger.warning(f"DeFi Llama USDC price fetch failed: {e}")
    return None
