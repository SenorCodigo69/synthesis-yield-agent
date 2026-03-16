"""Tests for Uniswap V3 LP position management."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from src.uniswap_lp import (
    UniswapLPAdapter,
    LPPosition,
    MintResult,
    CollectResult,
    ExitResult,
    full_range_ticks,
    WETH_BASE,
    USDC_BASE,
    POSITION_MANAGER,
    FEE_TIERS,
    DEFAULT_FEE,
    WETH_DECIMALS,
    USDC_DECIMALS,
    _MIN_TICK,
    _MAX_TICK,
)


# ── Tick Math Tests ──────────────────────────────────────────

class TestTickMath:
    def test_full_range_ticks_default_fee(self):
        """Full range ticks align to tick spacing 10 (0.05% fee)."""
        min_t, max_t = full_range_ticks(500)
        assert min_t % 10 == 0
        assert max_t % 10 == 0
        assert min_t < 0
        assert max_t > 0
        # Should be the largest aligned values
        assert min_t == -887270
        assert max_t == 887270

    def test_full_range_ticks_300bps(self):
        """Full range ticks align to tick spacing 60 (0.3% fee)."""
        min_t, max_t = full_range_ticks(3000)
        assert min_t % 60 == 0
        assert max_t % 60 == 0
        assert min_t == -887220
        assert max_t == 887220

    def test_full_range_ticks_100bps(self):
        """Full range ticks align to tick spacing 200 (1% fee)."""
        min_t, max_t = full_range_ticks(10000)
        assert min_t % 200 == 0
        assert max_t % 200 == 0
        assert min_t == -887200
        assert max_t == 887200

    def test_full_range_ticks_1bps(self):
        """Full range ticks align to tick spacing 1 (0.01% fee)."""
        min_t, max_t = full_range_ticks(100)
        assert min_t % 1 == 0
        assert max_t % 1 == 0
        assert min_t == _MIN_TICK  # -887272, already aligned
        assert max_t == _MAX_TICK  # 887272, already aligned

    def test_full_range_ticks_invalid_fee(self):
        with pytest.raises(ValueError, match="Unknown fee tier"):
            full_range_ticks(999)

    def test_ticks_are_symmetric(self):
        """Min and max ticks should be roughly symmetric."""
        for fee in FEE_TIERS:
            min_t, max_t = full_range_ticks(fee)
            assert abs(abs(min_t) - abs(max_t)) <= FEE_TIERS[fee]

    def test_min_less_than_max(self):
        for fee in FEE_TIERS:
            min_t, max_t = full_range_ticks(fee)
            assert min_t < max_t


# ── Token Order Tests ────────────────────────────────────────

class TestTokenOrder:
    def test_weth_is_token0(self):
        """WETH address < USDC address on Base, so WETH is token0."""
        assert WETH_BASE.lower() < USDC_BASE.lower()

    def test_position_manager_address(self):
        assert POSITION_MANAGER == "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"


# ── LPPosition Model Tests ──────────────────────────────────

class TestLPPosition:
    def test_create_position(self):
        pos = LPPosition(
            token_id=12345,
            token0=WETH_BASE,
            token1=USDC_BASE,
            fee=500,
            tick_lower=-887270,
            tick_upper=887270,
            liquidity=1000000,
            tokens_owed0=100,
            tokens_owed1=200,
        )
        assert pos.token_id == 12345
        assert pos.liquidity == 1000000
        assert pos.fee == 500


# ── Adapter Mock Setup ───────────────────────────────────────

def _make_mock_w3():
    """Create a mock AsyncWeb3 instance."""
    w3 = MagicMock()
    w3.to_checksum_address = lambda addr: addr
    w3.eth = MagicMock()
    w3.eth.contract = MagicMock()
    w3.eth.get_block = AsyncMock(return_value={"timestamp": 1700000000, "baseFeePerGas": 100})
    w3.eth.get_transaction_count = AsyncMock(return_value=0)
    w3.eth.estimate_gas = AsyncMock(return_value=300000)
    w3.eth.send_raw_transaction = AsyncMock(return_value=b"\x01" * 32)
    w3.eth.wait_for_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "blockNumber": 12345,
        "gasUsed": 250000,
        "logs": [],
    })

    # max_priority_fee is a property that returns a coroutine
    async def _max_priority_fee():
        return 1000

    type(w3.eth).max_priority_fee = PropertyMock(side_effect=lambda: _max_priority_fee())

    # Mock account signing
    mock_signed = MagicMock()
    mock_signed.raw_transaction = b"\x02" * 100
    w3.eth.account = MagicMock()
    w3.eth.account.sign_transaction = MagicMock(return_value=mock_signed)

    return w3


# ── Adapter Tests ────────────────────────────────────────────

class TestUniswapLPAdapter:
    def test_init(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        assert adapter.chain_id == 8453

    @pytest.mark.asyncio
    async def test_get_balances(self):
        w3 = _make_mock_w3()
        mock_contract = MagicMock()
        mock_fn = MagicMock()
        mock_fn.call = AsyncMock(side_effect=[
            440000000000000,  # 0.00044 WETH
            9600000,          # 9.60 USDC
        ])
        mock_contract.functions.balanceOf = MagicMock(return_value=mock_fn)
        w3.eth.contract = MagicMock(return_value=mock_contract)

        adapter = UniswapLPAdapter(w3)
        weth, usdc = await adapter.get_balances("0xtest")
        assert weth == Decimal("440000000000000") / Decimal(10**18)
        assert usdc == Decimal("9600000") / Decimal(10**6)

    @pytest.mark.asyncio
    async def test_get_position(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)

        # Mock positions() call
        mock_fn = MagicMock()
        mock_fn.call = AsyncMock(return_value=(
            0,                # nonce
            "0x0000",         # operator
            WETH_BASE,        # token0
            USDC_BASE,        # token1
            500,              # fee
            -887270,          # tickLower
            887270,           # tickUpper
            1000000,          # liquidity
            0,                # feeGrowthInside0LastX128
            0,                # feeGrowthInside1LastX128
            500,              # tokensOwed0
            1000,             # tokensOwed1
        ))
        adapter._pm.functions.positions = MagicMock(return_value=mock_fn)

        pos = await adapter.get_position(12345)
        assert pos.token_id == 12345
        assert pos.token0 == WETH_BASE
        assert pos.token1 == USDC_BASE
        assert pos.fee == 500
        assert pos.liquidity == 1000000
        assert pos.tokens_owed0 == 500
        assert pos.tokens_owed1 == 1000

    @pytest.mark.asyncio
    async def test_get_position_invalid_id(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        with pytest.raises(ValueError, match="Invalid token ID"):
            await adapter.get_position(0)

    @pytest.mark.asyncio
    async def test_get_position_negative_id(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        with pytest.raises(ValueError, match="Invalid token ID"):
            await adapter.get_position(-1)


class TestGetAllowances:
    @pytest.mark.asyncio
    async def test_get_allowances(self):
        w3 = _make_mock_w3()
        mock_contract = MagicMock()
        mock_fn = MagicMock()
        mock_fn.call = AsyncMock(side_effect=[
            2**256 - 1,  # WETH allowance (max)
            0,           # USDC allowance (zero)
        ])
        mock_contract.functions.allowance = MagicMock(return_value=mock_fn)
        w3.eth.contract = MagicMock(return_value=mock_contract)

        adapter = UniswapLPAdapter(w3)
        weth_a, usdc_a = await adapter.get_allowances("0xtest")
        assert weth_a == 2**256 - 1
        assert usdc_a == 0


class TestMintValidation:
    @pytest.mark.asyncio
    async def test_mint_zero_amounts_rejected(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        with pytest.raises(ValueError, match="Must provide at least one"):
            await adapter.mint_full_range("0x" + "ab" * 32, 0, 0)


class TestReceiptParsing:
    def test_parse_mint_receipt_with_events(self):
        """Parse tokenId, liquidity, amount0, amount1 from logs."""
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)

        # Transfer event (tokenId = 42)
        transfer_topic = bytes.fromhex(
            "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )
        from_topic = bytes(32)  # address(0) — mint
        to_topic = bytes.fromhex("0000000000000000000000008d691720bf8c81044db1a77b82d0ef5f5bffde6c")
        token_id_topic = (42).to_bytes(32, "big")

        # IncreaseLiquidity event
        increase_topic = bytes.fromhex(
            "3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
        )
        token_id_indexed = (42).to_bytes(32, "big")
        # data: liquidity(32) + amount0(32) + amount1(32)
        liquidity_bytes = (5000000).to_bytes(32, "big")
        amount0_bytes = (440000000000000).to_bytes(32, "big")  # 0.00044 WETH
        amount1_bytes = (9600000).to_bytes(32, "big")          # 9.60 USDC
        increase_data = liquidity_bytes + amount0_bytes + amount1_bytes

        receipt = {
            "logs": [
                {
                    "topics": [transfer_topic, from_topic, to_topic, token_id_topic],
                    "data": b"",
                },
                {
                    "topics": [increase_topic, token_id_indexed],
                    "data": increase_data,
                },
            ],
        }

        token_id, liquidity, amount0, amount1 = adapter._parse_mint_receipt(receipt)
        assert token_id == 42
        assert liquidity == 5000000
        assert amount0 == 440000000000000
        assert amount1 == 9600000

    def test_parse_mint_receipt_no_events(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        with pytest.raises(RuntimeError, match="Failed to parse tokenId"):
            adapter._parse_mint_receipt({"logs": []})

    def test_parse_collect_receipt(self):
        """Parse amount0, amount1 from Collect event."""
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)

        collect_topic = bytes.fromhex(
            "40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"
        )
        token_id_indexed = (42).to_bytes(32, "big")
        # data: recipient(32) + amount0(32) + amount1(32)
        recipient_bytes = bytes.fromhex(
            "0000000000000000000000008d691720bf8c81044db1a77b82d0ef5f5bffde6c"
        )
        amount0_bytes = (100).to_bytes(32, "big")
        amount1_bytes = (200).to_bytes(32, "big")

        receipt = {
            "logs": [
                {
                    "topics": [collect_topic, token_id_indexed],
                    "data": recipient_bytes + amount0_bytes + amount1_bytes,
                },
            ],
        }

        amount0, amount1 = adapter._parse_collect_receipt(receipt)
        assert amount0 == 100
        assert amount1 == 200

    def test_parse_collect_receipt_no_events(self):
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)
        amount0, amount1 = adapter._parse_collect_receipt({"logs": []})
        assert amount0 == 0
        assert amount1 == 0

    def test_parse_mint_receipt_hex_data(self):
        """Handle data as hex string (some web3 versions return strings)."""
        w3 = _make_mock_w3()
        adapter = UniswapLPAdapter(w3)

        increase_topic = bytes.fromhex(
            "3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
        )
        transfer_topic = bytes.fromhex(
            "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )

        token_id_topic = (99).to_bytes(32, "big")
        liquidity = (1000).to_bytes(32, "big")
        amt0 = (500).to_bytes(32, "big")
        amt1 = (600).to_bytes(32, "big")
        hex_data = "0x" + (liquidity + amt0 + amt1).hex()

        receipt = {
            "logs": [
                {
                    "topics": [transfer_topic, bytes(32), bytes(32), token_id_topic],
                    "data": b"",
                },
                {
                    "topics": [increase_topic, token_id_topic],
                    "data": hex_data,
                },
            ],
        }

        token_id, liq, a0, a1 = adapter._parse_mint_receipt(receipt)
        assert token_id == 99
        assert liq == 1000
        assert a0 == 500
        assert a1 == 600


class TestConstants:
    def test_fee_tiers(self):
        assert FEE_TIERS[100] == 1
        assert FEE_TIERS[500] == 10
        assert FEE_TIERS[3000] == 60
        assert FEE_TIERS[10000] == 200

    def test_default_fee(self):
        assert DEFAULT_FEE == 500

    def test_decimals(self):
        assert WETH_DECIMALS == 18
        assert USDC_DECIMALS == 6
