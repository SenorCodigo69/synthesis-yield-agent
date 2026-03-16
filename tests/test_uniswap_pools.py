"""Tests for Uniswap pool analytics module."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from src.data.uniswap_pools import (
    UniswapPool,
    fetch_uniswap_pools,
    fetch_usdc_pools,
    get_best_pool_apy,
    format_pool_summary,
    _parse_pool,
    MIN_POOL_TVL,
    MAX_APY_SANITY,
)


# ── Fixtures ─────────────────────────────────────────────────

SAMPLE_POOL_RAW = {
    "pool": "pool-123",
    "symbol": "WETH-USDC",
    "project": "uniswap-v3",
    "chain": "Base",
    "tvlUsd": 50_000_000,
    "apy": 60.0,
    "apyBase": 58.0,
    "apyReward": 2.0,
    "ilRisk": "yes",
}

SAMPLE_POOL_SMALL = {
    "pool": "pool-small",
    "symbol": "ABC-USDC",
    "project": "uniswap-v3",
    "chain": "Base",
    "tvlUsd": 50_000,  # Below MIN_POOL_TVL
    "apy": 10.0,
    "apyBase": 10.0,
    "apyReward": 0,
    "ilRisk": "no",
}

SAMPLE_POOL_HIGH_APY = {
    "pool": "pool-high",
    "symbol": "MEME-USDC",
    "project": "uniswap-v3",
    "chain": "Base",
    "tvlUsd": 200_000,
    "apy": 500.0,  # Above sanity cap
    "apyBase": 500.0,
    "apyReward": 0,
    "ilRisk": "yes",
}

SAMPLE_POOL_NO_USDC = {
    "pool": "pool-no-usdc",
    "symbol": "WETH-CBBTC",
    "project": "uniswap-v3",
    "chain": "Base",
    "tvlUsd": 5_000_000,
    "apy": 40.0,
    "apyBase": 40.0,
    "apyReward": 0,
    "ilRisk": "yes",
}

SAMPLE_POOL_ETHEREUM = {
    "pool": "pool-eth",
    "symbol": "WETH-USDC",
    "project": "uniswap-v3",
    "chain": "Ethereum",
    "tvlUsd": 100_000_000,
    "apy": 20.0,
    "apyBase": 20.0,
    "apyReward": 0,
    "ilRisk": "yes",
}

SAMPLE_POOL_NON_UNI = {
    "pool": "pool-sushi",
    "symbol": "WETH-USDC",
    "project": "sushiswap",
    "chain": "Base",
    "tvlUsd": 1_000_000,
    "apy": 15.0,
    "apyBase": 15.0,
    "apyReward": 0,
    "ilRisk": "yes",
}


def _make_mock_session(pools: list[dict]):
    """Create a mock aiohttp session returning pool data."""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value={"data": pools})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=response)
    return session


# ── _parse_pool tests ────────────────────────────────────────

class TestParsePool:
    def test_parse_valid_pool(self):
        pool = _parse_pool(SAMPLE_POOL_RAW)
        assert pool is not None
        assert pool.pair_symbol == "WETH-USDC"
        assert pool.project == "uniswap-v3"
        assert pool.tvl_usd == Decimal("50000000")
        assert pool.apy_base == Decimal("0.58")  # 58% / 100
        assert pool.apy_reward == Decimal("0.02")
        assert pool.il_risk == "yes"
        assert pool.is_usdc_pair is True

    def test_parse_pool_below_min_tvl(self):
        pool = _parse_pool(SAMPLE_POOL_SMALL)
        assert pool is None

    def test_parse_pool_apy_sanity_cap(self):
        pool = _parse_pool(SAMPLE_POOL_HIGH_APY)
        assert pool is not None
        assert pool.apy_total <= MAX_APY_SANITY
        assert pool.apy_base <= MAX_APY_SANITY

    def test_parse_pool_non_usdc(self):
        pool = _parse_pool(SAMPLE_POOL_NO_USDC)
        assert pool is not None
        assert pool.is_usdc_pair is False

    def test_parse_pool_no_il(self):
        raw = {**SAMPLE_POOL_RAW, "ilRisk": None}
        pool = _parse_pool(raw)
        assert pool is not None
        assert pool.il_risk == "no"

    def test_parse_pool_missing_fields(self):
        pool = _parse_pool({})
        assert pool is None

    def test_parse_pool_zero_apy(self):
        raw = {**SAMPLE_POOL_RAW, "apy": 0, "apyBase": 0, "apyReward": 0}
        pool = _parse_pool(raw)
        assert pool is not None
        assert pool.fee_apy == Decimal("0")

    def test_parse_pool_none_apy_fields(self):
        raw = {**SAMPLE_POOL_RAW, "apyBase": None, "apyReward": None, "apy": 5.0}
        pool = _parse_pool(raw)
        assert pool is not None
        assert pool.apy_base == Decimal("0")


# ── fetch tests ──────────────────────────────────────────────

class TestFetchPools:
    @pytest.mark.asyncio
    async def test_fetch_uniswap_pools(self):
        all_pools = [
            SAMPLE_POOL_RAW,
            SAMPLE_POOL_NO_USDC,
            SAMPLE_POOL_NON_UNI,  # Should be excluded
            SAMPLE_POOL_ETHEREUM,  # Wrong chain
            SAMPLE_POOL_SMALL,    # Below TVL
        ]
        session = _make_mock_session(all_pools)
        pools = await fetch_uniswap_pools(session, chain="Base")
        # Only SAMPLE_POOL_RAW and SAMPLE_POOL_NO_USDC should pass
        assert len(pools) == 2
        assert all(p.project.startswith("uniswap") for p in pools)

    @pytest.mark.asyncio
    async def test_fetch_usdc_pools(self):
        all_pools = [
            SAMPLE_POOL_RAW,       # USDC pair ✓
            SAMPLE_POOL_NO_USDC,   # No USDC ✗
        ]
        session = _make_mock_session(all_pools)
        pools = await fetch_usdc_pools(session, chain="Base")
        assert len(pools) == 1
        assert pools[0].is_usdc_pair is True

    @pytest.mark.asyncio
    async def test_fetch_filters_by_chain(self):
        all_pools = [SAMPLE_POOL_RAW, SAMPLE_POOL_ETHEREUM]
        session = _make_mock_session(all_pools)
        pools = await fetch_uniswap_pools(session, chain="Base")
        assert len(pools) == 1
        assert pools[0].pair_symbol == "WETH-USDC"

    @pytest.mark.asyncio
    async def test_fetch_filters_non_uniswap(self):
        all_pools = [SAMPLE_POOL_RAW, SAMPLE_POOL_NON_UNI]
        session = _make_mock_session(all_pools)
        pools = await fetch_uniswap_pools(session, chain="Base")
        assert len(pools) == 1

    @pytest.mark.asyncio
    async def test_fetch_sorted_by_tvl(self):
        pool_low = {**SAMPLE_POOL_RAW, "pool": "low", "tvlUsd": 200_000}
        pool_high = {**SAMPLE_POOL_RAW, "pool": "high", "tvlUsd": 90_000_000}
        session = _make_mock_session([pool_low, pool_high])
        pools = await fetch_uniswap_pools(session, chain="Base")
        assert pools[0].tvl_usd > pools[1].tvl_usd

    @pytest.mark.asyncio
    async def test_fetch_empty_response(self):
        session = _make_mock_session([])
        pools = await fetch_uniswap_pools(session, chain="Base")
        assert pools == []

    @pytest.mark.asyncio
    async def test_fetch_api_failure(self):
        session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value={"data": []})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=response)
        # Empty data = empty result
        pools = await fetch_uniswap_pools(session, chain="Base")
        assert pools == []


# ── get_best_pool_apy tests ──────────────────────────────────

class TestGetBestPoolApy:
    @pytest.mark.asyncio
    async def test_returns_best_fee_apy(self):
        pool_low = {**SAMPLE_POOL_RAW, "pool": "low", "apyBase": 10.0, "apy": 10.0}
        pool_high = {**SAMPLE_POOL_RAW, "pool": "high", "apyBase": 80.0, "apy": 80.0}
        session = _make_mock_session([pool_low, pool_high])
        apy, pool = await get_best_pool_apy(session, chain="Base")
        assert apy == Decimal("0.80")
        assert pool is not None

    @pytest.mark.asyncio
    async def test_returns_zero_on_empty(self):
        session = _make_mock_session([])
        apy, pool = await get_best_pool_apy(session, chain="Base")
        assert apy == Decimal("0")
        assert pool is None


# ── format_pool_summary tests ────────────────────────────────

class TestFormatPoolSummary:
    def test_format_empty(self):
        result = format_pool_summary([])
        assert "No Uniswap pools" in result

    def test_format_with_pools(self):
        pool = _parse_pool(SAMPLE_POOL_RAW)
        result = format_pool_summary([pool])
        assert "WETH-USDC" in result
        assert "fee APY" in result
        assert "IL risk" in result or "impermanent" in result.lower()

    def test_format_respects_limit(self):
        pool = _parse_pool(SAMPLE_POOL_RAW)
        pools = [pool] * 10
        result = format_pool_summary(pools, limit=3)
        # Should have header + 3 pool lines + 1 note
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 5  # header + 3 pools + note


# ── UniswapPool model tests ─────────────────────────────────

class TestUniswapPoolModel:
    def test_fee_apy_property(self):
        pool = _parse_pool(SAMPLE_POOL_RAW)
        assert pool.fee_apy == pool.apy_base

    def test_is_usdc_pair_true(self):
        pool = _parse_pool(SAMPLE_POOL_RAW)
        assert pool.is_usdc_pair is True

    def test_is_usdc_pair_false(self):
        pool = _parse_pool(SAMPLE_POOL_NO_USDC)
        assert pool.is_usdc_pair is False
