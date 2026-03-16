"""Settings loader — YAML config + .env."""

import os
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.models import SpendingScope

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_config(config_path: Path | None = None) -> dict:
    """Load YAML config, merge with env vars."""
    load_dotenv(CONFIG_DIR / ".env")

    path = Path(config_path) if config_path else CONFIG_DIR / "default.yaml"
    # Validate config path is within project directory
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(
            f"Config path {resolved} is outside project root {PROJECT_ROOT}"
        )
    with open(resolved) as f:
        config = yaml.safe_load(f)

    # Override with env vars where set
    if os.getenv("BASE_RPC_URL"):
        config["rpc_url"] = os.getenv("BASE_RPC_URL")
    else:
        config["rpc_url"] = "https://mainnet.base.org"

    if os.getenv("PRIVATE_KEY"):
        config["_private_key"] = os.getenv("PRIVATE_KEY")

    if os.getenv("UNISWAP_API_KEY"):
        config["uniswap_api_key"] = os.getenv("UNISWAP_API_KEY")

    if os.getenv("ANTHROPIC_API_KEY"):
        config["anthropic_api_key"] = os.getenv("ANTHROPIC_API_KEY")

    if os.getenv("BLOCKNATIVE_API_KEY"):
        config["blocknative_api_key"] = os.getenv("BLOCKNATIVE_API_KEY")

    return config


def pop_private_key(config: dict) -> str | None:
    """Extract and remove private key from config dict.

    Call this once during setup, then pass the key to TransactionSigner.
    The key is deleted from the config dict to prevent accidental exposure
    via logging or serialization.
    """
    return config.pop("_private_key", None)


class SpendingScopeError(ValueError):
    """Raised when spending scope config values are out of safe bounds."""
    pass


def load_spending_scope(config: dict) -> SpendingScope:
    """Extract and validate spending scope from config.

    Validates that all values are within safe bounds to prevent
    misconfiguration from causing unexpected behavior.
    """
    sc = config.get("spending_scope", {})
    scope = SpendingScope(
        max_total_allocation_pct=Decimal(str(sc.get("max_total_allocation_pct", 0.80))),
        max_per_protocol_pct=Decimal(str(sc.get("max_per_protocol_pct", 0.40))),
        min_protocol_tvl_usd=Decimal(str(sc.get("min_protocol_tvl_usd", 50_000_000))),
        max_utilization=Decimal(str(sc.get("max_utilization", 0.90))),
        max_apy_sanity=Decimal(str(sc.get("max_apy_sanity", 0.50))),
        gas_ceiling_gwei=sc.get("gas_ceiling_gwei", 100),
        withdrawal_cooldown_secs=sc.get("withdrawal_cooldown_secs", 3600),
        reserve_buffer_pct=Decimal(str(sc.get("reserve_buffer_pct", 0.20))),
    )
    _validate_spending_scope(scope)
    return scope


def _validate_spending_scope(scope: SpendingScope) -> None:
    """Validate spending scope values are within safe bounds."""
    if not (Decimal("0") < scope.max_total_allocation_pct <= Decimal("1.0")):
        raise SpendingScopeError(
            f"max_total_allocation_pct must be (0, 1.0], got {scope.max_total_allocation_pct}"
        )
    if not (Decimal("0") < scope.max_per_protocol_pct <= Decimal("1.0")):
        raise SpendingScopeError(
            f"max_per_protocol_pct must be (0, 1.0], got {scope.max_per_protocol_pct}"
        )
    if scope.min_protocol_tvl_usd < Decimal("0"):
        raise SpendingScopeError(
            f"min_protocol_tvl_usd must be >= 0, got {scope.min_protocol_tvl_usd}"
        )
    if not (Decimal("0") < scope.max_utilization <= Decimal("1.0")):
        raise SpendingScopeError(
            f"max_utilization must be (0, 1.0], got {scope.max_utilization}"
        )
    if scope.max_apy_sanity <= Decimal("0"):
        raise SpendingScopeError(
            f"max_apy_sanity must be > 0, got {scope.max_apy_sanity}"
        )
    if scope.gas_ceiling_gwei < 0:
        raise SpendingScopeError(
            f"gas_ceiling_gwei must be >= 0, got {scope.gas_ceiling_gwei}"
        )
    if scope.withdrawal_cooldown_secs < 0:
        raise SpendingScopeError(
            f"withdrawal_cooldown_secs must be >= 0, got {scope.withdrawal_cooldown_secs}"
        )
    if not (Decimal("0") <= scope.reserve_buffer_pct < Decimal("1.0")):
        raise SpendingScopeError(
            f"reserve_buffer_pct must be [0, 1.0), got {scope.reserve_buffer_pct}"
        )
