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

    path = config_path or CONFIG_DIR / "default.yaml"
    with open(path) as f:
        config = yaml.safe_load(f)

    # Override with env vars where set
    if os.getenv("BASE_RPC_URL"):
        config["rpc_url"] = os.getenv("BASE_RPC_URL")
    else:
        config["rpc_url"] = "https://mainnet.base.org"

    if os.getenv("PRIVATE_KEY"):
        config["private_key"] = os.getenv("PRIVATE_KEY")

    if os.getenv("BLOCKNATIVE_API_KEY"):
        config["blocknative_api_key"] = os.getenv("BLOCKNATIVE_API_KEY")

    return config


def load_spending_scope(config: dict) -> SpendingScope:
    """Extract spending scope from config."""
    sc = config.get("spending_scope", {})
    return SpendingScope(
        max_total_allocation_pct=Decimal(str(sc.get("max_total_allocation_pct", 0.80))),
        max_per_protocol_pct=Decimal(str(sc.get("max_per_protocol_pct", 0.40))),
        min_protocol_tvl_usd=Decimal(str(sc.get("min_protocol_tvl_usd", 50_000_000))),
        max_utilization=Decimal(str(sc.get("max_utilization", 0.90))),
        max_apy_sanity=Decimal(str(sc.get("max_apy_sanity", 0.50))),
        gas_ceiling_gwei=sc.get("gas_ceiling_gwei", 100),
        withdrawal_cooldown_secs=sc.get("withdrawal_cooldown_secs", 3600),
        reserve_buffer_pct=Decimal(str(sc.get("reserve_buffer_pct", 0.20))),
    )
