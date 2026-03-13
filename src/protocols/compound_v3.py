"""Compound V3 (Comet) protocol adapter.

Supply USDC directly to Comet contract. APY from getSupplyRate(utilization).
Single Comet per base asset — simpler than Aave's multi-pool architecture.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, ProtocolName, TxReceipt
from src.protocols.base import ProtocolAdapter

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = Decimal(str(365.25 * 24 * 3600))

COMET_ABI = [
    {
        "inputs": [{"name": "utilization", "type": "uint256"}],
        "name": "getSupplyRate",
        "outputs": [{"name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getUtilization",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalBorrow",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "supply",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

USDC_DECIMALS = 6

ADDRESSES = {
    Chain.BASE: {
        "comet": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
}


class CompoundV3Adapter(ProtocolAdapter):
    """Compound V3 (Comet) adapter for USDC lending."""

    def __init__(self, w3: AsyncWeb3, chain: Chain, config: dict):
        super().__init__(w3, chain, config)
        addrs = ADDRESSES[chain]
        self._comet = w3.eth.contract(
            address=w3.to_checksum_address(addrs["comet"]),
            abi=COMET_ABI,
        )
        self._usdc_addr = w3.to_checksum_address(addrs["usdc"])
        self._usdc = w3.eth.contract(
            address=self._usdc_addr, abi=ERC20_APPROVE_ABI
        )

    @property
    def name(self) -> ProtocolName:
        return ProtocolName.COMPOUND_V3

    @property
    def supported_assets(self) -> list[str]:
        return ["USDC"]

    async def get_supply_rate(self) -> Decimal:
        utilization = await self._comet.functions.getUtilization().call()
        rate_per_sec = await self._comet.functions.getSupplyRate(utilization).call()
        # Convert per-second rate (1e18 scaled) to APY percentage
        rate = Decimal(rate_per_sec) / Decimal(10**18)
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

    async def supply(self, amount: Decimal, sender: str) -> TxReceipt:
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._comet.functions.supply(
            self._usdc_addr, raw_amount
        ).build_transaction({
            "from": sender_addr,
            "gas": 250_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Compound V3 supply: {amount} USDC | tx: {tx_hash.hex()}")

        return TxReceipt(
            tx_hash=tx_hash.hex(),
            action=ActionType.SUPPLY,
            protocol=self.name,
            chain=self.chain,
            amount=amount,
            gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def withdraw(self, amount: Decimal, sender: str) -> TxReceipt:
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._comet.functions.withdraw(
            self._usdc_addr, raw_amount
        ).build_transaction({
            "from": sender_addr,
            "gas": 250_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Compound V3 withdraw: {amount} USDC | tx: {tx_hash.hex()}")

        return TxReceipt(
            tx_hash=tx_hash.hex(),
            action=ActionType.WITHDRAW,
            protocol=self.name,
            chain=self.chain,
            amount=amount,
            gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def approve(self, amount: Decimal, sender: str) -> TxReceipt:
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)
        comet_addr = self.w3.to_checksum_address(ADDRESSES[self.chain]["comet"])

        tx = await self._usdc.functions.approve(
            comet_addr, raw_amount
        ).build_transaction({
            "from": sender_addr,
            "gas": 100_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Compound V3 approve: {amount} USDC | tx: {tx_hash.hex()}")

        return TxReceipt(
            tx_hash=tx_hash.hex(),
            action=ActionType.APPROVE,
            protocol=self.name,
            chain=self.chain,
            amount=amount,
            gas_cost_usd=Decimal("0"),
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )
