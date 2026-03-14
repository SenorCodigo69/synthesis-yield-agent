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


# Sanity bounds for USDC price — any value outside this range is treated
# as corrupt data (API compromise, parsing error, etc.)
USDC_PRICE_FLOOR = Decimal("0.50")
USDC_PRICE_CEILING = Decimal("1.50")


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
                validated = _validate_usdc_price(result, "CoinGecko")
                if validated is not None:
                    logger.info(f"USDC price (CoinGecko): ${validated:.4f}")
                    return validated
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
                validated = _validate_usdc_price(result, "DeFi Llama")
                if validated is not None:
                    logger.info(f"USDC price (DeFi Llama): ${validated:.4f}")
                    return validated
    except Exception as e:
        logger.warning(f"DeFi Llama USDC price fetch failed: {e}")
    return None
