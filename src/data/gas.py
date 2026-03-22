"""Gas price tracking — on-chain basefee + optional Blocknative.

Used to calculate net APY (gross APY minus amortized gas costs)
and to trigger gas-aware circuit breakers.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
from web3 import AsyncWeb3

from src.models import GasPrice

logger = logging.getLogger(__name__)


async def fetch_gas_onchain(w3: AsyncWeb3) -> GasPrice | None:
    """Fetch current gas price from the chain's latest block."""
    try:
        block = await w3.eth.get_block("latest")
        base_fee_wei = block.get("baseFeePerGas", 0)
        base_fee_gwei = Decimal(base_fee_wei) / Decimal(10**9)

        # Priority fee — use eth_maxPriorityFeePerGas if available
        try:
            priority_wei = await w3.eth.max_priority_fee
            priority_gwei = Decimal(priority_wei) / Decimal(10**9)
        except Exception:
            priority_gwei = Decimal("0.001")  # Base is very cheap

        gas = GasPrice(
            base_fee_gwei=base_fee_gwei,
            priority_fee_gwei=priority_gwei,
            source="onchain",
            timestamp=datetime.now(tz=timezone.utc),
        )
        logger.info(
            f"Gas (on-chain): {gas.total_gwei:.4f} gwei "
            f"(base: {base_fee_gwei:.4f}, priority: {priority_gwei:.4f})"
        )
        return gas
    except Exception as e:
        logger.error(f"Failed to fetch on-chain gas: {e}")
        return None


async def fetch_gas_blocknative(
    session: aiohttp.ClientSession,
    api_key: str | None = None,
) -> GasPrice | None:
    """Fetch gas estimate from Blocknative API (optional, needs API key)."""
    if not api_key:
        return None

    try:
        url = "https://api.blocknative.com/gasprices/blockprices"
        headers = {"Authorization": api_key}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            block_prices = data.get("blockPrices", [{}])[0]
            estimated = block_prices.get("estimatedPrices", [{}])[0]

            gas = GasPrice(
                base_fee_gwei=Decimal(str(block_prices.get("baseFeePerGas", 0))),
                priority_fee_gwei=Decimal(
                    str(estimated.get("maxPriorityFeePerGas", 0))
                ),
                source="blocknative",
                timestamp=datetime.now(tz=timezone.utc),
            )
            logger.info(f"Gas (Blocknative): {gas.total_gwei:.4f} gwei")
            return gas
    except Exception as e:
        logger.warning(f"Blocknative gas fetch failed: {e}")
        return None


def estimate_tx_cost_usd(
    gas_price: GasPrice,
    gas_limit: int = 200_000,
    eth_price_usd: Decimal = Decimal("2500"),
) -> Decimal:
    """Estimate transaction cost in USD.

    Default gas_limit=200k covers most DeFi interactions.
    On Base, typical costs are <$0.01.
    """
    gas_cost_eth = (gas_price.total_gwei * Decimal(gas_limit)) / Decimal(10**9)
    return gas_cost_eth * eth_price_usd
