"""Aave V3 protocol adapter.

Supply USDC -> get aUSDC (rebasing). APY from currentLiquidityRate (RAY).
Uses Pool contract for supply/withdraw, aToken for balance reads.

Security:
- SEC-C01: Explicit nonce via build_tx_with_safety()
- SEC-H02: Chain ID enforced in every transaction
- SEC-H03: Dynamic gas estimation with fallback
- SEC-H04: No config dict stored — signer passed at tx time
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, ProtocolName, TxReceipt
from src.protocols.base import ProtocolAdapter
from src.protocols.abis import AAVE_POOL_ABI, ERC20_ABI, RAY, USDC_DECIMALS
from src.protocols.tx_helpers import (
    TransactionSigner,
    build_tx_with_safety,
    sign_and_send,
    validate_amount,
)

logger = logging.getLogger(__name__)

ADDRESSES = {
    Chain.BASE: {
        "pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
}


class AaveV3Adapter(ProtocolAdapter):
    """Aave V3 protocol adapter for USDC lending."""

    def __init__(self, w3: AsyncWeb3, chain: Chain):
        super().__init__(w3, chain)
        addrs = ADDRESSES[chain]
        self._pool = w3.eth.contract(
            address=w3.to_checksum_address(addrs["pool"]),
            abi=AAVE_POOL_ABI,
        )
        self._usdc_addr = w3.to_checksum_address(addrs["usdc"])
        self._usdc = w3.eth.contract(address=self._usdc_addr, abi=ERC20_ABI)

    @property
    def name(self) -> ProtocolName:
        return ProtocolName.AAVE_V3

    @property
    def supported_assets(self) -> list[str]:
        return ["USDC"]

    async def _get_reserve_data(self) -> tuple:
        return await self._pool.functions.getReserveData(self._usdc_addr).call()

    async def get_supply_rate(self) -> Decimal:
        data = await self._get_reserve_data()
        liquidity_rate = Decimal(data[2])
        return (liquidity_rate / Decimal(RAY)) * 100

    async def get_utilization(self) -> Decimal:
        return Decimal("0")

    async def get_tvl(self) -> Decimal:
        return Decimal("0")

    async def get_balance(self, address: str) -> Decimal:
        data = await self._get_reserve_data()
        a_token_addr = data[8]
        a_token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(a_token_addr),
            abi=ERC20_ABI,
        )
        raw = await a_token.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()
        return Decimal(raw) / Decimal(10**USDC_DECIMALS)

    async def supply(self, amount: Decimal, sender: str, signer: TransactionSigner) -> TxReceipt:
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await build_tx_with_safety(
            self.w3,
            self._pool.functions.supply(self._usdc_addr, raw_amount, sender_addr, 0),
            sender_addr,
            fallback_gas=300_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Aave V3 supply: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.SUPPLY, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def withdraw(self, amount: Decimal, sender: str, signer: TransactionSigner) -> TxReceipt:
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await build_tx_with_safety(
            self.w3,
            self._pool.functions.withdraw(self._usdc_addr, raw_amount, sender_addr),
            sender_addr,
            fallback_gas=300_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Aave V3 withdraw: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.WITHDRAW, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def approve(self, amount: Decimal, sender: str, signer: TransactionSigner) -> TxReceipt:
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)
        pool_addr = self.w3.to_checksum_address(ADDRESSES[self.chain]["pool"])

        tx = await build_tx_with_safety(
            self.w3,
            self._usdc.functions.approve(pool_addr, raw_amount),
            sender_addr,
            fallback_gas=100_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Aave V3 approve: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.APPROVE, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )
