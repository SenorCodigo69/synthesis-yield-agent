"""Security tests — validates all audit findings are fixed."""

import asyncio

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.protocols.tx_helpers import (
    BASE_CHAIN_ID,
    ChainIdMismatchError,
    MissingPrivateKeyError,
    TransactionRevertedError,
    TransactionSigner,
    build_tx_with_safety,
    estimate_gas_with_fallback,
    get_private_key,
    sign_and_send,
    validate_amount,
    validate_chain_id,
)
from src.data.gas import estimate_tx_cost_usd
from src.models import GasPrice, SpendingScope


# ── SEC-001: Private key validation ──────────────────────────────────────

class TestPrivateKeyValidation:
    def test_missing_key_raises(self):
        with pytest.raises(MissingPrivateKeyError):
            get_private_key({})

    def test_empty_string_raises(self):
        with pytest.raises(MissingPrivateKeyError):
            get_private_key({"private_key": ""})

    def test_short_key_raises(self):
        with pytest.raises(MissingPrivateKeyError):
            get_private_key({"private_key": "abc123"})

    def test_none_raises(self):
        with pytest.raises(MissingPrivateKeyError):
            get_private_key({"private_key": None})

    def test_valid_key_returns(self):
        key = "0x" + "a" * 64
        result = get_private_key({"private_key": key})
        assert result == key

    def test_whitespace_stripped(self):
        key = "0x" + "b" * 64
        result = get_private_key({"private_key": f"  {key}  "})
        assert result == key


# ── SEC-H01/H04: TransactionSigner isolation ────────────────────────────

class TestTransactionSigner:
    def test_signer_stores_key(self):
        key = "0x" + "a" * 64
        signer = TransactionSigner(key)
        assert signer.key == key

    def test_signer_repr_redacted(self):
        """Private key must NEVER appear in repr/str."""
        key = "0x" + "c" * 64
        signer = TransactionSigner(key)
        assert key not in repr(signer)
        assert "redacted" in repr(signer).lower()

    def test_signer_rejects_invalid_key(self):
        with pytest.raises(MissingPrivateKeyError):
            TransactionSigner("")

    def test_signer_rejects_short_key(self):
        with pytest.raises(MissingPrivateKeyError):
            TransactionSigner("abc")

    def test_signer_rejects_none(self):
        with pytest.raises(MissingPrivateKeyError):
            TransactionSigner(None)


# ── SEC-H02: Chain ID validation ────────────────────────────────────────

class TestChainIdValidation:
    def test_correct_chain_id_passes(self):
        w3 = MagicMock()
        # chain_id is an async property in AsyncWeb3 — mock as coroutine

        async def fake_chain_id():
            return BASE_CHAIN_ID

        type(w3.eth).chain_id = property(lambda self: fake_chain_id())

        async def _run():
            await validate_chain_id(w3, BASE_CHAIN_ID)

        asyncio.get_event_loop().run_until_complete(_run())

    def test_wrong_chain_id_raises(self):
        w3 = MagicMock()

        async def fake_chain_id():
            return 1  # Ethereum mainnet

        type(w3.eth).chain_id = property(lambda self: fake_chain_id())

        async def _run():
            with pytest.raises(ChainIdMismatchError, match="Expected chain ID 8453"):
                await validate_chain_id(w3, BASE_CHAIN_ID)

        asyncio.get_event_loop().run_until_complete(_run())


# ── SEC-003: Amount validation ───────────────────────────────────────────

class TestAmountValidation:
    def test_zero_raises(self):
        with pytest.raises(ValueError, match="positive"):
            validate_amount(Decimal("0"))

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="positive"):
            validate_amount(Decimal("-100"))

    def test_insane_amount_raises(self):
        with pytest.raises(ValueError, match="sanity cap"):
            validate_amount(Decimal("2000000000"))

    def test_valid_amount_passes(self):
        validate_amount(Decimal("100.50"))
        validate_amount(Decimal("0.01"))
        validate_amount(Decimal("999999999"))

    def test_non_decimal_raises(self):
        with pytest.raises(ValueError, match="Decimal"):
            validate_amount(100)

    def test_non_decimal_float_raises(self):
        with pytest.raises(ValueError, match="Decimal"):
            validate_amount(100.5)


# ── Gas cost estimation ──────────────────────────────────────────────────

class TestGasCost:
    def test_base_chain_gas_cheap(self):
        """On Base, gas should be very cheap (<$0.01 typically)."""
        gas = GasPrice(
            base_fee_gwei=Decimal("0.001"),
            priority_fee_gwei=Decimal("0.001"),
            source="test",
        )
        cost = estimate_tx_cost_usd(gas, gas_limit=200_000)
        assert cost < Decimal("1"), f"Base gas cost should be <$1, got ${cost}"

    def test_high_gas_expensive(self):
        """High gas should produce high cost."""
        gas = GasPrice(
            base_fee_gwei=Decimal("100"),
            priority_fee_gwei=Decimal("5"),
            source="test",
        )
        cost = estimate_tx_cost_usd(gas, gas_limit=200_000)
        assert cost > Decimal("10"), f"High gas should be >$10, got ${cost}"


# ── Spending scope defaults ──────────────────────────────────────────────

class TestSpendingScope:
    def test_defaults_are_safe(self):
        scope = SpendingScope()
        assert scope.max_total_allocation_pct <= Decimal("0.80")
        assert scope.max_per_protocol_pct <= Decimal("0.40")
        assert scope.min_protocol_tvl_usd >= Decimal("50000000")
        assert scope.max_utilization <= Decimal("0.90")
        assert scope.max_apy_sanity <= Decimal("0.50")
        assert scope.reserve_buffer_pct >= Decimal("0.10")

    def test_hardcoded_apy_sanity_cap(self):
        """APY > 50% should be flagged as unsustainable."""
        scope = SpendingScope()
        assert scope.max_apy_sanity == Decimal("0.50")


# ── SEC-H03: Dynamic gas estimation ─────────────────────────────────────

class TestGasEstimation:
    def test_estimate_with_fallback_uses_estimate(self):
        """Should use dynamic estimate when available."""
        w3 = AsyncMock()
        w3.eth.estimate_gas = AsyncMock(return_value=100_000)

        async def _run():
            result = await estimate_gas_with_fallback(w3, {}, fallback_gas=300_000)
            assert result == 120_000  # 100k + 20% buffer
            return result

        asyncio.get_event_loop().run_until_complete(_run())

    def test_estimate_with_fallback_uses_fallback(self):
        """Should use fallback when estimate fails."""
        w3 = AsyncMock()
        w3.eth.estimate_gas = AsyncMock(side_effect=Exception("revert"))

        async def _run():
            result = await estimate_gas_with_fallback(w3, {}, fallback_gas=300_000)
            assert result == 300_000
            return result

        asyncio.get_event_loop().run_until_complete(_run())


# ── SEC-C02: Morpho slippage protection ─────────────────────────────────

class TestMorphoSlippage:
    def test_slippage_error_importable(self):
        """SlippageExceededError should be importable."""
        from src.protocols.morpho_blue import SlippageExceededError
        assert issubclass(SlippageExceededError, Exception)


# ── SEC-H04: Adapters don't store config ─────────────────────────────────

class TestAdapterIsolation:
    def test_base_adapter_no_config_param(self):
        """ProtocolAdapter.__init__ should not accept config."""
        import inspect
        from src.protocols.base import ProtocolAdapter
        sig = inspect.signature(ProtocolAdapter.__init__)
        param_names = list(sig.parameters.keys())
        assert "config" not in param_names

    def test_aave_adapter_no_config_param(self):
        import inspect
        from src.protocols.aave_v3 import AaveV3Adapter
        sig = inspect.signature(AaveV3Adapter.__init__)
        param_names = list(sig.parameters.keys())
        assert "config" not in param_names

    def test_compound_adapter_no_config_param(self):
        import inspect
        from src.protocols.compound_v3 import CompoundV3Adapter
        sig = inspect.signature(CompoundV3Adapter.__init__)
        param_names = list(sig.parameters.keys())
        assert "config" not in param_names

    def test_morpho_adapter_no_config_param(self):
        import inspect
        from src.protocols.morpho_blue import MorphoBlueAdapter
        sig = inspect.signature(MorphoBlueAdapter.__init__)
        param_names = list(sig.parameters.keys())
        assert "config" not in param_names


# ── SEC-M03: Compound rate sanity check ──────────────────────────────────

class TestCompoundRateSanity:
    def test_max_rate_constant_exists(self):
        from src.protocols.compound_v3 import MAX_RATE_PER_SEC
        assert MAX_RATE_PER_SEC > 0
        assert MAX_RATE_PER_SEC < Decimal("1e-6")  # Should be very small
