"""Tests for protocol adapters."""

import pytest
from decimal import Decimal

from src.models import Chain, ProtocolName
from src.protocols.aave_v3 import AaveV3Adapter
from src.protocols.compound_v3 import CompoundV3Adapter
from src.protocols.morpho_blue import MorphoBlueAdapter


def test_aave_adapter_properties():
    """Test Aave V3 adapter has correct metadata."""
    # Can't instantiate fully without web3, but test class attributes
    assert AaveV3Adapter.name.fget is not None
    # Verify address constants
    from src.protocols.aave_v3 import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["pool"] == "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    assert ADDRESSES[Chain.BASE]["usdc"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_compound_adapter_properties():
    """Test Compound V3 adapter has correct metadata."""
    from src.protocols.compound_v3 import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["comet"] == "0xb125E6687d4313864e53df431d5425969c15Eb2F"


def test_morpho_adapter_properties():
    """Test Morpho Blue adapter has correct metadata."""
    from src.protocols.morpho_blue import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["morpho_singleton"] == "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"


def test_ray_constant():
    """Test RAY constant used in Aave rate conversion."""
    from src.protocols.aave_v3 import RAY
    assert RAY == Decimal(10**27)
