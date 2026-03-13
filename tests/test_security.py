"""Security tests — validates all audit findings are fixed."""

import pytest
from decimal import Decimal

from src.protocols.tx_helpers import (
    MissingPrivateKeyError,
    TransactionRevertedError,
    get_private_key,
    validate_amount,
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
