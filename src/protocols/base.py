"""Abstract protocol adapter interface.

Adding a new protocol = implementing this interface.
"""

from abc import ABC, abstractmethod
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, HealthStatus, ProtocolName, TxReceipt


class ProtocolAdapter(ABC):
    """Base class for DeFi protocol adapters."""

    def __init__(self, w3: AsyncWeb3, chain: Chain, config: dict):
        self.w3 = w3
        self.chain = chain
        self.config = config

    @property
    @abstractmethod
    def name(self) -> ProtocolName:
        """Protocol identifier."""
        ...

    @property
    @abstractmethod
    def supported_assets(self) -> list[str]:
        """Assets this adapter supports (e.g., ['USDC'])."""
        ...

    # ── Read methods ────────────────────────────────────────────────────

    @abstractmethod
    async def get_supply_rate(self) -> Decimal:
        """Current supply APY as a percentage (e.g., 2.5 = 2.5%)."""
        ...

    @abstractmethod
    async def get_utilization(self) -> Decimal:
        """Current utilization ratio (0 to 1)."""
        ...

    @abstractmethod
    async def get_tvl(self) -> Decimal:
        """Total value locked in USD."""
        ...

    @abstractmethod
    async def get_balance(self, address: str) -> Decimal:
        """User's supplied balance in the protocol (in USDC, 6 decimals)."""
        ...

    # ── Write methods ───────────────────────────────────────────────────

    @abstractmethod
    async def supply(self, amount: Decimal, sender: str) -> TxReceipt:
        """Deposit USDC into the protocol."""
        ...

    @abstractmethod
    async def withdraw(self, amount: Decimal, sender: str) -> TxReceipt:
        """Withdraw USDC from the protocol."""
        ...

    @abstractmethod
    async def approve(self, amount: Decimal, sender: str) -> TxReceipt:
        """Approve USDC spending for the protocol contract."""
        ...

    # ── Safety methods ──────────────────────────────────────────────────

    async def health_check(self) -> HealthStatus:
        """Check if the protocol is healthy enough for interactions."""
        try:
            rate = await self.get_supply_rate()
            util = await self.get_utilization()

            if util > Decimal("0.95"):
                return HealthStatus.CRITICAL
            if util > Decimal("0.85"):
                return HealthStatus.WARNING
            if rate > Decimal("50"):  # APY > 50% is suspicious
                return HealthStatus.WARNING
            return HealthStatus.HEALTHY
        except Exception:
            return HealthStatus.CRITICAL

    async def can_withdraw(self, amount: Decimal) -> bool:
        """Check if there's enough liquidity to withdraw."""
        try:
            tvl = await self.get_tvl()
            util = await self.get_utilization()
            available = tvl * (1 - util)
            return available >= amount
        except Exception:
            return False
