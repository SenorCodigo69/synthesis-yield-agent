"""Compound V3 (Comet) protocol adapter.

Supply USDC directly to Comet contract. APY from getSupplyRate(utilization).
Single Comet per base asset -- simpler than Aave's multi-pool architecture.

Security:
- SEC-C01: Explicit nonce via build_tx_with_safety()
- SEC-H02: Chain ID enforced in every transaction
- SEC-H03: Dynamic gas estimation with fallback
- SEC-H04: No config dict stored — signer passed at tx time
- SEC-M03: Rate sanity check before APY exponentiation
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, ProtocolName, TxReceipt
from src.protocols.base import ProtocolAdapter
from src.protocols.abis import COMPOUND_COMET_ABI, ERC20_ABI, USDC_DECIMALS
from src.protocols.tx_helpers import (
    TransactionSigner,
    build_tx_with_safety,
    sign_and_send,
    validate_amount,
)

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = Decimal(str(365.25 * 24 * 3600))

# SEC-M03: Max rate per second (~30% APY). Anything above is likely corrupt data.
MAX_RATE_PER_SEC = Decimal("1e-8")

ADDRESSES = {
    Chain.BASE: {
        "comet": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
}


class CompoundV3Adapter(ProtocolAdapter):
    """Compound V3 (Comet) adapter for USDC lending."""

    def __init__(self, w3: AsyncWeb3, chain: Chain):
        super().__init__(w3, chain)
        addrs = ADDRESSES[chain]
        self._comet = w3.eth.contract(
            address=w3.to_checksum_address(addrs["comet"]),
            abi=COMPOUND_COMET_ABI,
        )
        self._usdc_addr = w3.to_checksum_address(addrs["usdc"])
        self._usdc = w3.eth.contract(address=self._usdc_addr, abi=ERC20_ABI)

    @property
    def name(self) -> ProtocolName:
        return ProtocolName.COMPOUND_V3

    @property
    def supported_assets(self) -> list[str]:
        return ["USDC"]

    async def get_supply_rate(self) -> Decimal:
        utilization = await self._comet.functions.getUtilization().call()
        rate_per_sec = await self._comet.functions.getSupplyRate(utilization).call()
        rate = Decimal(rate_per_sec) / Decimal(10**18)

        # SEC-M03: Sanity check before expensive exponentiation
        if rate > MAX_RATE_PER_SEC:
            logger.error(
                f"Compound rate per sec {rate} exceeds sanity cap {MAX_RATE_PER_SEC} "
                f"(~30% APY) — treating as invalid"
            )
            return Decimal("0")

        apy = ((1 + rate) ** SECONDS_PER_YEAR - 1) * 100
        return apy

    async def get_utilization(self) -> Decimal:
        util_raw = await self._comet.functions.getUtilization().call()
        return Decimal(util_raw) / Decimal(10**18)

    async def get_tvl(self) -> Decimal:
        total_supply = await self._comet.functions.totalSupply().call()
        return Decimal(total_supply) / Decimal(10**USDC_DECIMALS)

    async def get_balance(self, address: str) -> Decimal:
        raw = await self._comet.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()
        return Decimal(raw) / Decimal(10**USDC_DECIMALS)

    async def supply(self, amount: Decimal, sender: str, signer: TransactionSigner) -> TxReceipt:
        validate_amount(amount)
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await build_tx_with_safety(
            self.w3,
            self._comet.functions.supply(self._usdc_addr, raw_amount),
            sender_addr,
            fallback_gas=250_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Compound V3 supply: {amount} USDC | tx: {tx_hash}")

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
            self._comet.functions.withdraw(self._usdc_addr, raw_amount),
            sender_addr,
            fallback_gas=250_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Compound V3 withdraw: {amount} USDC | tx: {tx_hash}")

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
        comet_addr = self.w3.to_checksum_address(ADDRESSES[self.chain]["comet"])

        tx = await build_tx_with_safety(
            self.w3,
            self._usdc.functions.approve(comet_addr, raw_amount),
            sender_addr,
            fallback_gas=100_000,
        )

        tx_hash, receipt = await sign_and_send(self.w3, tx, signer)
        logger.info(f"Compound V3 approve: {amount} USDC | tx: {tx_hash}")

        return TxReceipt(
            tx_hash=tx_hash, action=ActionType.APPROVE, protocol=self.name,
            chain=self.chain, amount=amount, gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )
