"""On-chain rate reads via web3.py -- direct contract calls.

Second data source for cross-validation against DeFi Llama.
Base chain by default.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from src.models import Chain, DataSource, ProtocolName, YieldPool
from src.protocols.abis import (
    AAVE_POOL_ABI, COMPOUND_COMET_ABI, METAMORPHO_QUEUE_ABI,
    MORPHO_BLUE_ABI, MORPHO_IRM_ABI, RAY,
)

logger = logging.getLogger(__name__)

# Addresses on Base
ADDRESSES = {
    Chain.BASE: {
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "aave_pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "compound_comet": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
    }
}

SECONDS_PER_YEAR = 365.25 * 24 * 3600
RPC_REQUEST_TIMEOUT = 15  # seconds

# Max rate per second (~30% APY) — anything above is corrupt on-chain data
MAX_RATE_PER_SEC = Decimal("1e-8")

# Morpho Blue — same singleton address on all EVM chains
MORPHO_ADDRESSES = {
    Chain.BASE: {
        "morpho_singleton": "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",
        # Known USDC markets — read directly from the Morpho singleton,
        # no vault abstraction needed. IDs computed from MarketParams.
        "usdc_market_ids": [
            # USDC/WETH 86% LLTV — largest ($56M+ supply)
            "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda",
            # USDC/cbETH
            "0xdba352d93a64b17c71104cbddc6aef85cd432322a1446b5b65163cbbc615cd0c",
        ],
    }
}


async def create_web3(rpc_url: str) -> AsyncWeb3:
    """Create an async web3 instance with timeout."""
    return AsyncWeb3(AsyncHTTPProvider(
        rpc_url,
        request_kwargs={"timeout": RPC_REQUEST_TIMEOUT},
    ))


async def fetch_aave_rate(
    w3: AsyncWeb3,
    chain: Chain = Chain.BASE,
) -> YieldPool | None:
    """Fetch Aave V3 USDC supply rate directly from the Pool contract."""
    try:
        addrs = ADDRESSES[chain]
        pool = w3.eth.contract(
            address=w3.to_checksum_address(addrs["aave_pool"]),
            abi=AAVE_POOL_ABI,
        )
        usdc = w3.to_checksum_address(addrs["usdc"])

        reserve_data = await pool.functions.getReserveData(usdc).call()
        liquidity_rate_ray = Decimal(reserve_data[2])
        supply_apy_pct = (liquidity_rate_ray / Decimal(RAY)) * 100

        logger.info(f"On-chain | Aave V3 | USDC supply APY: {supply_apy_pct:.4f}%")

        return YieldPool(
            pool_id=f"aave-v3-{chain.value}-usdc-onchain",
            protocol=ProtocolName.AAVE_V3,
            chain=chain,
            symbol="USDC",
            apy_base=supply_apy_pct,
            apy_reward=Decimal("0"),
            apy_total=supply_apy_pct,
            tvl_usd=Decimal("0"),
            utilization=Decimal("0"),
            source=DataSource.ONCHAIN,
            timestamp=datetime.now(tz=timezone.utc),
        )
    except Exception as e:
        logger.error(f"Failed to fetch Aave V3 on-chain rate: {e}")
        return None


async def fetch_compound_rate(
    w3: AsyncWeb3,
    chain: Chain = Chain.BASE,
) -> YieldPool | None:
    """Fetch Compound V3 USDC supply rate from the Comet contract."""
    try:
        addrs = ADDRESSES[chain]
        comet = w3.eth.contract(
            address=w3.to_checksum_address(addrs["compound_comet"]),
            abi=COMPOUND_COMET_ABI,
        )

        utilization = await comet.functions.getUtilization().call()
        supply_rate_per_sec = await comet.functions.getSupplyRate(utilization).call()

        rate_decimal = Decimal(supply_rate_per_sec) / Decimal(10**18)

        # Sanity check: reject corrupt on-chain data before expensive exponentiation
        if rate_decimal > MAX_RATE_PER_SEC:
            logger.error(
                f"Compound on-chain rate {rate_decimal} exceeds sanity cap "
                f"{MAX_RATE_PER_SEC} (~30% APY) — treating as invalid"
            )
            return None

        supply_apy = ((1 + rate_decimal) ** Decimal(str(SECONDS_PER_YEAR)) - 1) * 100

        util_pct = Decimal(utilization) / Decimal(10**18)

        logger.info(
            f"On-chain | Compound V3 | USDC supply APY: {supply_apy:.4f}% | "
            f"Utilization: {util_pct:.2%}"
        )

        return YieldPool(
            pool_id=f"compound-v3-{chain.value}-usdc-onchain",
            protocol=ProtocolName.COMPOUND_V3,
            chain=chain,
            symbol="USDC",
            apy_base=supply_apy,
            apy_reward=Decimal("0"),
            apy_total=supply_apy,
            tvl_usd=Decimal("0"),
            utilization=util_pct,
            source=DataSource.ONCHAIN,
            timestamp=datetime.now(tz=timezone.utc),
        )
    except Exception as e:
        logger.error(f"Failed to fetch Compound V3 on-chain rate: {e}")
        return None


async def fetch_morpho_rate(
    w3: AsyncWeb3,
    chain: Chain = Chain.BASE,
) -> YieldPool | None:
    """Fetch Morpho Blue supply rate directly on-chain.

    Reads market state from the Morpho Blue singleton, then calls the
    AdaptiveCurveIrm to get the borrow rate per second. Computes supply
    APY as: borrowAPY * utilization * (1 - fee), weighted across markets.
    """
    try:
        addrs = MORPHO_ADDRESSES[chain]
        morpho = w3.eth.contract(
            address=w3.to_checksum_address(addrs["morpho_singleton"]),
            abi=MORPHO_BLUE_ABI,
        )

        total_supply = Decimal("0")
        weighted_apy = Decimal("0")
        markets_read = 0

        for market_id_hex in addrs["usdc_market_ids"]:
            market_id = bytes.fromhex(market_id_hex[2:])

            # Get market state from Morpho singleton
            market_data = await morpho.functions.market(market_id).call()
            total_supply_assets = Decimal(market_data[0])
            total_borrow_assets = Decimal(market_data[2])
            fee_raw = Decimal(market_data[5])

            if total_supply_assets == 0:
                continue

            utilization = total_borrow_assets / total_supply_assets

            # Get market params to find the IRM address
            params = await morpho.functions.idToMarketParams(market_id).call()
            irm_address = params[3]

            # Call IRM.borrowRateView(marketParams, market) for borrow rate/sec
            irm = w3.eth.contract(
                address=w3.to_checksum_address(irm_address),
                abi=MORPHO_IRM_ABI,
            )
            borrow_rate_per_sec = Decimal(
                await irm.functions.borrowRateView(params, market_data).call()
            )

            # supply APY = borrowAPY * utilization * (1 - fee)
            rate_per_sec = borrow_rate_per_sec / Decimal(10**18)
            fee_pct = fee_raw / Decimal(10**18)
            supply_apy = (
                ((1 + rate_per_sec) ** Decimal(str(SECONDS_PER_YEAR)) - 1)
                * utilization
                * (1 - fee_pct)
                * 100
            )

            weighted_apy += supply_apy * total_supply_assets
            total_supply += total_supply_assets
            markets_read += 1

        if total_supply == 0:
            return None

        avg_apy = weighted_apy / total_supply
        tvl_usd = total_supply / Decimal(10**6)  # USDC = 6 decimals

        logger.info(
            f"On-chain | Morpho Blue | USDC supply APY: {avg_apy:.4f}% | "
            f"TVL: ${tvl_usd:,.0f} | {markets_read} markets"
        )

        return YieldPool(
            pool_id=f"morpho-v1-{chain.value}-usdc-onchain",
            protocol=ProtocolName.MORPHO,
            chain=chain,
            symbol="USDC",
            apy_base=avg_apy,
            apy_reward=Decimal("0"),
            apy_total=avg_apy,
            tvl_usd=tvl_usd,
            utilization=Decimal("0"),
            source=DataSource.ONCHAIN,
            timestamp=datetime.now(tz=timezone.utc),
        )
    except Exception as e:
        logger.error(f"Failed to fetch Morpho on-chain rate: {e}")
        return None


async def fetch_all_onchain_rates(
    rpc_url: str,
    chain: Chain = Chain.BASE,
) -> list[YieldPool]:
    """Fetch on-chain rates from all supported protocols."""
    w3 = await create_web3(rpc_url)
    results = []

    for fetcher in [fetch_aave_rate, fetch_compound_rate, fetch_morpho_rate]:
        pool = await fetcher(w3, chain)
        if pool:
            results.append(pool)

    logger.info(f"On-chain: fetched {len(results)} rates on {chain.value}")
    return results
