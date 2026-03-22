"""Tests for Aerodrome AMM pool monitoring."""

import pytest
from unittest.mock import AsyncMock, patch

from src.data.defillama import fetch_aerodrome_pools, MONITORED_AMM_SLUGS
from src.models import Chain


SAMPLE_POOLS = [
    {
        "pool": "aero-usdc-weth-1",
        "project": "aerodrome-v1",
        "chain": "Base",
        "symbol": "USDC-WETH",
        "apy": 12.5,
        "apyBase": 8.2,
        "apyReward": 4.3,
        "tvlUsd": 5_000_000,
    },
    {
        "pool": "aero-usdc-dai-2",
        "project": "aerodrome-v1",
        "chain": "Base",
        "symbol": "USDC-DAI",
        "apy": 6.1,
        "apyBase": 3.0,
        "apyReward": 3.1,
        "tvlUsd": 2_000_000,
    },
    {
        "pool": "aero-tiny-pool",
        "project": "aerodrome-v1",
        "chain": "Base",
        "symbol": "TINY-PAIR",
        "apy": 50.0,
        "apyBase": 50.0,
        "apyReward": 0,
        "tvlUsd": 50_000,  # Below min_tvl
    },
    {
        "pool": "aero-eth-pool",
        "project": "aerodrome-v1",
        "chain": "Ethereum",  # Wrong chain
        "symbol": "WETH-USDC",
        "apy": 10.0,
        "apyBase": 10.0,
        "apyReward": 0,
        "tvlUsd": 1_000_000,
    },
    {
        "pool": "aave-usdc",
        "project": "aave-v3",  # Not Aerodrome
        "chain": "Base",
        "symbol": "USDC",
        "apy": 2.5,
        "apyBase": 2.5,
        "apyReward": 0,
        "tvlUsd": 100_000_000,
    },
]


@pytest.fixture
def mock_pools():
    """Mock fetch_all_pools to return sample data."""
    with patch("src.data.defillama.fetch_all_pools", new_callable=AsyncMock) as mock:
        mock.return_value = SAMPLE_POOLS
        yield mock


class TestAerodromeMonitor:
    @pytest.mark.asyncio
    async def test_fetches_aerodrome_pools_only(self, mock_pools):
        """Should only return Aerodrome pools, not Aave etc."""
        pools = await fetch_aerodrome_pools(AsyncMock())
        assert all(p["project"] in MONITORED_AMM_SLUGS for p in pools)

    @pytest.mark.asyncio
    async def test_filters_by_chain(self, mock_pools):
        """Should only return Base pools."""
        pools = await fetch_aerodrome_pools(AsyncMock(), chain=Chain.BASE)
        # Ethereum pool should be excluded
        symbols = [p["symbol"] for p in pools]
        assert "WETH-USDC" not in symbols  # The Ethereum one

    @pytest.mark.asyncio
    async def test_filters_by_min_tvl(self, mock_pools):
        """Should exclude pools below min TVL."""
        pools = await fetch_aerodrome_pools(AsyncMock(), min_tvl=100_000)
        symbols = [p["symbol"] for p in pools]
        assert "TINY-PAIR" not in symbols

    @pytest.mark.asyncio
    async def test_sorted_by_tvl_desc(self, mock_pools):
        """Should return pools sorted by TVL descending."""
        pools = await fetch_aerodrome_pools(AsyncMock())
        tvls = [p["tvl_usd"] for p in pools]
        assert tvls == sorted(tvls, reverse=True)

    @pytest.mark.asyncio
    async def test_max_pools_limit(self, mock_pools):
        """Should respect max_pools limit."""
        pools = await fetch_aerodrome_pools(AsyncMock(), max_pools=1)
        assert len(pools) == 1

    @pytest.mark.asyncio
    async def test_pool_data_structure(self, mock_pools):
        """Should return properly structured pool data."""
        pools = await fetch_aerodrome_pools(AsyncMock())
        assert len(pools) == 2  # Only Base pools above min TVL

        top = pools[0]
        assert top["symbol"] == "USDC-WETH"
        assert top["apy_total"] == 12.5
        assert top["apy_base"] == 8.2
        assert top["apy_reward"] == 4.3
        assert top["tvl_usd"] == 5_000_000

    @pytest.mark.asyncio
    async def test_empty_when_no_aerodrome(self, mock_pools):
        """Should return empty list when no Aerodrome pools exist."""
        mock_pools.return_value = [SAMPLE_POOLS[-1]]  # Only Aave
        pools = await fetch_aerodrome_pools(AsyncMock())
        assert pools == []


class TestMonitoredSlugs:
    def test_aerodrome_in_monitored(self):
        """Aerodrome should be in the monitored AMM set."""
        assert "aerodrome-v1" in MONITORED_AMM_SLUGS
        assert "aerodrome-slipstream" in MONITORED_AMM_SLUGS
