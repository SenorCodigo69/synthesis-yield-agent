"""Tests for the multi-source data layer."""

import pytest
import aiohttp
from decimal import Decimal

from src.models import Chain, DataSource, ProtocolName, YieldPool, ValidatedRate
from src.data import defillama


@pytest.mark.asyncio
async def test_fetch_usdc_pools_live():
    """Integration test: fetch real USDC pools from DeFi Llama."""
    async with aiohttp.ClientSession() as session:
        pools = await defillama.fetch_usdc_pools(session, Chain.BASE)

    assert len(pools) > 0, "Should find at least one USDC pool on Base"

    for pool in pools:
        assert pool.symbol and "USDC" in pool.symbol.upper()
        assert pool.chain == Chain.BASE
        assert pool.source == DataSource.DEFILLAMA
        assert pool.apy_total >= 0
        assert pool.tvl_usd >= 0
        assert pool.protocol in ProtocolName


@pytest.mark.asyncio
async def test_fetch_usdc_pools_all_protocols():
    """Check we get pools from all 3 target protocols on Base."""
    async with aiohttp.ClientSession() as session:
        pools = await defillama.fetch_usdc_pools(session, Chain.BASE)

    protocols_found = {pool.protocol for pool in pools}
    # At minimum we should find Aave V3 and Morpho (Compound may have tiny TVL)
    assert ProtocolName.AAVE_V3 in protocols_found, "Aave V3 USDC pool not found on Base"
    assert ProtocolName.MORPHO in protocols_found, "Morpho USDC pool not found on Base"


@pytest.mark.asyncio
async def test_fetch_protocol_tvl():
    """Test protocol-level TVL fetch."""
    async with aiohttp.ClientSession() as session:
        tvl = await defillama.fetch_protocol_tvl(session, "aave-v3")

    assert tvl is not None
    assert tvl > 0, "Aave V3 should have positive TVL"


def test_yield_pool_model():
    """Test YieldPool dataclass."""
    pool = YieldPool(
        pool_id="test-pool",
        protocol=ProtocolName.AAVE_V3,
        chain=Chain.BASE,
        symbol="USDC",
        apy_base=Decimal("2.5"),
        apy_reward=Decimal("0"),
        apy_total=Decimal("2.5"),
        tvl_usd=Decimal("100000000"),
        utilization=Decimal("0.65"),
        source=DataSource.DEFILLAMA,
    )
    assert pool.apy_total == Decimal("2.5")
    assert pool.protocol == ProtocolName.AAVE_V3


def test_validated_rate_model():
    """Test ValidatedRate dataclass."""
    rate = ValidatedRate(
        protocol=ProtocolName.AAVE_V3,
        chain=Chain.BASE,
        apy_median=Decimal("2.5"),
        apy_sources={
            DataSource.DEFILLAMA: Decimal("2.4"),
            DataSource.ONCHAIN: Decimal("2.6"),
        },
        tvl_usd=Decimal("100000000"),
        utilization=Decimal("0.65"),
        divergence=Decimal("0.2"),
        is_valid=True,
    )
    assert rate.is_valid
    assert len(rate.apy_sources) == 2
    assert rate.warnings == []
