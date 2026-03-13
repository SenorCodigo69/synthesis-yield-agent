"""Data models for the yield agent."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class ProtocolName(str, Enum):
    AAVE_V3 = "aave-v3"
    MORPHO = "morpho-v1"
    COMPOUND_V3 = "compound-v3"


class Chain(str, Enum):
    BASE = "Base"
    ETHEREUM = "Ethereum"
    ARBITRUM = "Arbitrum"


class DataSource(str, Enum):
    DEFILLAMA = "defillama"
    ONCHAIN = "onchain"


class ActionType(str, Enum):
    SUPPLY = "supply"
    WITHDRAW = "withdraw"
    APPROVE = "approve"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class YieldPool:
    """A yield opportunity from a specific protocol."""
    pool_id: str
    protocol: ProtocolName
    chain: Chain
    symbol: str
    apy_base: Decimal
    apy_reward: Decimal
    apy_total: Decimal
    tvl_usd: Decimal
    utilization: Decimal
    source: DataSource
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class ValidatedRate:
    """Cross-validated rate from multiple sources."""
    protocol: ProtocolName
    chain: Chain
    apy_median: Decimal
    apy_sources: dict[DataSource, Decimal] = field(default_factory=dict)
    tvl_usd: Decimal = Decimal("0")
    utilization: Decimal = Decimal("0")
    divergence: Decimal = Decimal("0")
    is_valid: bool = True
    warnings: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class Allocation:
    """Current or target allocation to a protocol."""
    protocol: ProtocolName
    chain: Chain
    amount_usd: Decimal
    target_pct: Decimal
    actual_pct: Decimal
    last_rebalance: datetime | None = None


@dataclass
class TxReceipt:
    """On-chain transaction receipt."""
    tx_hash: str
    action: ActionType
    protocol: ProtocolName
    chain: Chain
    amount: Decimal
    gas_cost_usd: Decimal
    timestamp: datetime
    block_number: int


@dataclass
class SpendingScope:
    """Human-defined spending constraints."""
    max_total_allocation_pct: Decimal = Decimal("0.80")
    max_per_protocol_pct: Decimal = Decimal("0.40")
    min_protocol_tvl_usd: Decimal = Decimal("50000000")
    max_utilization: Decimal = Decimal("0.90")
    max_apy_sanity: Decimal = Decimal("0.50")
    gas_ceiling_gwei: int = 100
    withdrawal_cooldown_secs: int = 3600
    reserve_buffer_pct: Decimal = Decimal("0.20")


@dataclass
class GasPrice:
    """Current gas price info."""
    base_fee_gwei: Decimal
    priority_fee_gwei: Decimal
    source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def total_gwei(self) -> Decimal:
        return self.base_fee_gwei + self.priority_fee_gwei


class ExecutionMode(str, Enum):
    PAPER = "paper"        # Simulated — no on-chain interaction
    DRY_RUN = "dry_run"    # Build txs but don't sign/send
    LIVE = "live"          # Real on-chain execution


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    SIMULATED = "simulated"  # Dry-run — logged but not real


@dataclass
class ExecutionRecord:
    """A single execution action (supply or withdraw)."""
    id: str                          # UUID
    action: ActionType
    protocol: ProtocolName
    chain: Chain
    amount_usd: Decimal
    mode: ExecutionMode
    tx_hash: str | None = None       # None in paper mode
    block_number: int | None = None
    gas_cost_usd: Decimal = Decimal("0")
    simulated_gas_usd: Decimal = Decimal("0")  # Estimated gas in paper mode
    reasoning: str = ""
    status: ExecutionStatus = ExecutionStatus.PENDING
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class PortfolioSnapshot:
    """Point-in-time portfolio state."""
    total_capital_usd: Decimal
    allocated_usd: Decimal
    reserve_usd: Decimal
    unrealized_yield_usd: Decimal
    total_gas_spent_usd: Decimal
    positions: dict[str, Decimal]    # protocol -> amount_usd
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def net_value_usd(self) -> Decimal:
        return self.total_capital_usd + self.unrealized_yield_usd - self.total_gas_spent_usd
