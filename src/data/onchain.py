"""On-chain rate reads via web3.py — direct contract calls.

Second data source for cross-validation against DeFi Llama.
Base chain by default.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from src.models import Chain, DataSource, ProtocolName, YieldPool

logger = logging.getLogger(__name__)

# ─── Aave V3 ────────────────────────────────────────────────────────────────
# Pool.getReserveData(address asset) returns a tuple where:
#   index 2 = currentLiquidityRate (RAY = 1e27)
#   index 4 = currentVariableBorrowRate (RAY)
AAVE_POOL_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"name": "configuration", "type": "uint256"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "currentLiquidityRate", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "currentVariableBorrowRate", "type": "uint128"},
                    {"name": "currentStableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "id", "type": "uint16"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "stableDebtTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

# ─── Compound V3 ─────────────────────────────────────────────────────────────
# Comet.getSupplyRate(uint utilization) and getUtilization()
COMPOUND_COMET_ABI = [
    {
        "inputs": [{"name": "utilization", "type": "uint256"}],
        "name": "getSupplyRate",
        "outputs": [{"name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getUtilization",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalBorrow",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Addresses on Base
ADDRESSES = {
    Chain.BASE: {
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "aave_pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "compound_comet": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
        "morpho_singleton": "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",
    }
}

RAY = Decimal(10**27)
SECONDS_PER_YEAR = 365.25 * 24 * 3600


async def create_web3(rpc_url: str) -> AsyncWeb3:
    """Create an async web3 instance."""
    return AsyncWeb3(AsyncHTTPProvider(rpc_url))


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
        # currentLiquidityRate is at index 2, in RAY (1e27)
        liquidity_rate_ray = Decimal(reserve_data[2])
        supply_apy_pct = (liquidity_rate_ray / RAY) * 100

        logger.info(f"On-chain | Aave V3 | USDC supply APY: {supply_apy_pct:.4f}%")

        return YieldPool(
            pool_id=f"aave-v3-{chain.value}-usdc-onchain",
            protocol=ProtocolName.AAVE_V3,
            chain=chain,
            symbol="USDC",
            apy_base=supply_apy_pct,
            apy_reward=Decimal("0"),
            apy_total=supply_apy_pct,
            tvl_usd=Decimal("0"),  # Not available from this call
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

        # Convert per-second rate to APY percentage
        # rate is scaled by 1e18
        rate_decimal = Decimal(supply_rate_per_sec) / Decimal(10**18)
        supply_apy = ((1 + rate_decimal) ** Decimal(str(SECONDS_PER_YEAR)) - 1) * 100

        # Utilization is scaled by 1e18
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


async def fetch_all_onchain_rates(
    rpc_url: str,
    chain: Chain = Chain.BASE,
) -> list[YieldPool]:
    """Fetch on-chain rates from all supported protocols."""
    w3 = await create_web3(rpc_url)
    results = []

    for fetcher in [fetch_aave_rate, fetch_compound_rate]:
        pool = await fetcher(w3, chain)
        if pool:
            results.append(pool)

    logger.info(f"On-chain: fetched {len(results)} rates on {chain.value}")
    return results
