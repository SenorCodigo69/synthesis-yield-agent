"""Morpho Blue protocol adapter.

For the hackathon, we use MetaMorpho vaults (ERC-4626 compatible)
rather than raw Morpho Blue markets. Vaults handle market allocation
internally -- simpler interface: deposit(assets, receiver) / withdraw().

DeFi Llama slug: morpho-v1 (NOT morpho-blue).
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, ProtocolName, TxReceipt
from src.protocols.base import ProtocolAdapter
from src.protocols.abis import ERC20_ABI, ERC4626_ABI, USDC_DECIMALS
from src.protocols.tx_helpers import sign_and_send, validate_amount

logger = logging.getLogger(__name__)

ADDRESSES = {
    Chain.BASE: {
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "morpho_singleton": "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",
    }
}


class MorphoBlueAdapter(ProtocolAdapter):
    """Morpho Blue adapter using MetaMorpho vaults (ERC-4626).

    On-chain rate reads for Morpho require knowing the specific vault.
    We rely on DeFi Llama for rate discovery and use on-chain for
    balance reads and execution only.
    """

    def __init__(
        self,
        w3: AsyncWeb3,
        chain: Chain,
        config: dict,
        vault_address: str | None = None,
    ):
        super().__init__(w3, chain, config)
        self._usdc_addr = w3.to_checksum_address(ADDRESSES[chain]["usdc"])
        self._usdc = w3.eth.contract(address=self._usdc_addr, abi=ERC20_ABI)
        self._vault = None
        self._vault_addr: str | None = None
        if vault_address:
            self.set_vault(vault_address)

    def set_vault(self, vault_address: str) -> None:
        """Set the MetaMorpho vault to use for operations."""
        self._vault_addr = self.w3.to_checksum_address(vault_address)
        self._vault = self.w3.eth.contract(
            address=self._vault_addr, abi=ERC4626_ABI,
        )
        logger.info(f"Morpho vault set: {self._vault_addr}")

    def _require_vault(self) -> None:
        """Guard: fail fast if no vault is configured."""
        if not self._vault:
            raise RuntimeError("No vault set -- call set_vault() first")

    @property
    def name(self) -> ProtocolName:
        return ProtocolName.MORPHO

    @property
    def supported_assets(self) -> list[str]:
        return ["USDC"]

    async def get_supply_rate(self) -> Decimal:
        logger.debug("Morpho supply rate: use DeFi Llama (no single on-chain read)")
        return Decimal("0")

    async def get_utilization(self) -> Decimal:
        return Decimal("0")

    async def get_tvl(self) -> Decimal:
        if not self._vault:
            return Decimal("0")
        total = await self._vault.functions.totalAssets().call()
        return Decimal(total) / Decimal(10**USDC_DECIMALS)

    async def get_balance(self, address: str) -> Decimal:
        if not self._vault:
            return Decimal("0")
        shares = await self._vault.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()
        if shares == 0:
            return Decimal("0")
        assets = await self._vault.functions.convertToAssets(shares).call()
        return Decimal(assets) / Decimal(10**USDC_DECIMALS)

    async def supply(self, amount: Decimal, sender: str) -> TxReceipt:
        self._require_vault()
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._vault.functions.deposit(
            raw_amount, sender_addr
        ).build_transaction({"from": sender_addr, "gas": 300_000})

        tx_hash, receipt = await sign_and_send(self.w3, tx, self.config)
        logger.info(f"Morpho supply: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.SUPPLY, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def withdraw(self, amount: Decimal, sender: str) -> TxReceipt:
        self._require_vault()
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._vault.functions.withdraw(
            raw_amount, sender_addr, sender_addr
        ).build_transaction({"from": sender_addr, "gas": 300_000})

        tx_hash, receipt = await sign_and_send(self.w3, tx, self.config)
        logger.info(f"Morpho withdraw: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.WITHDRAW, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def approve(self, amount: Decimal, sender: str) -> TxReceipt:
        self._require_vault()
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._usdc.functions.approve(
            self._vault_addr, raw_amount
        ).build_transaction({"from": sender_addr, "gas": 100_000})

        tx_hash, receipt = await sign_and_send(self.w3, tx, self.config)
        logger.info(f"Morpho approve: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.APPROVE, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )
