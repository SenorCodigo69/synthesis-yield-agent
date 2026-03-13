"""Tests for protocol adapters."""

import pytest
from decimal import Decimal

from src.models import Chain, ProtocolName
from src.protocols.abis import RAY, USDC_DECIMALS, AAVE_POOL_ABI, COMPOUND_COMET_ABI, ERC4626_ABI


def test_shared_abis_exist():
    """All required ABI fragments should be present."""
    # Aave needs getReserveData, supply, withdraw
    aave_names = {fn["name"] for fn in AAVE_POOL_ABI}
    assert "getReserveData" in aave_names
    assert "supply" in aave_names
    assert "withdraw" in aave_names

    # Compound needs getSupplyRate, getUtilization, supply, withdraw
    compound_names = {fn["name"] for fn in COMPOUND_COMET_ABI}
    assert "getSupplyRate" in compound_names
    assert "getUtilization" in compound_names
    assert "supply" in compound_names
    assert "withdraw" in compound_names

    # ERC4626 needs deposit, withdraw, convertToAssets, balanceOf
    erc4626_names = {fn["name"] for fn in ERC4626_ABI}
    assert "deposit" in erc4626_names
    assert "withdraw" in erc4626_names
    assert "convertToAssets" in erc4626_names
    assert "balanceOf" in erc4626_names


def test_ray_constant():
    """RAY = 1e27 (Aave's rate unit)."""
    assert RAY == 10**27


def test_usdc_decimals():
    """USDC uses 6 decimals."""
    assert USDC_DECIMALS == 6


def test_aave_addresses():
    """Aave V3 Base addresses should be correct."""
    from src.protocols.aave_v3 import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["pool"] == "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    assert ADDRESSES[Chain.BASE]["usdc"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_compound_addresses():
    """Compound V3 Base addresses should be correct."""
    from src.protocols.compound_v3 import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["comet"] == "0xb125E6687d4313864e53df431d5425969c15Eb2F"


def test_morpho_addresses():
    """Morpho Base addresses should be correct."""
    from src.protocols.morpho_blue import ADDRESSES
    assert Chain.BASE in ADDRESSES
    assert ADDRESSES[Chain.BASE]["morpho_singleton"] == "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"


def test_no_abi_duplication():
    """ABIs should be imported from shared abis module, not defined inline."""
    import inspect
    import src.protocols.aave_v3 as aave
    import src.protocols.compound_v3 as compound
    import src.protocols.morpho_blue as morpho
    import src.protocols.abis as shared

    # Check that adapters import ABIs from the shared module (not re-defined)
    # An imported name's id matches the source module's object
    assert aave.AAVE_POOL_ABI is shared.AAVE_POOL_ABI
    assert aave.ERC20_ABI is shared.ERC20_ABI
    assert compound.COMPOUND_COMET_ABI is shared.COMPOUND_COMET_ABI
    assert morpho.ERC4626_ABI is shared.ERC4626_ABI
