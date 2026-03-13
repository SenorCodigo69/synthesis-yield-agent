"""Aave V3 protocol adapter.

Supply USDC → get aUSDC (rebasing). APY from currentLiquidityRate (RAY).
Uses Pool contract for supply/withdraw, aToken for balance reads.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from web3 import AsyncWeb3

from src.models import ActionType, Chain, ProtocolName, TxReceipt
from src.protocols.base import ProtocolAdapter

logger = logging.getLogger(__name__)

# Minimal ABIs — only what we need
POOL_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"name": "configuration", "type": "uint256"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "currentLiquidityRate", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "currentVariableBorrowRate", "type": "uint128"},
                    {"name": "currentStableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "id", "type": "uint16"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "stableDebtTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
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
            {"name": "to", "type": "address"},
        ],
        "name": "withdraw",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ERC20_ABI = [
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
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

RAY = Decimal(10**27)
USDC_DECIMALS = 6

# Base addresses
ADDRESSES = {
    Chain.BASE: {
        "pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
}


class AaveV3Adapter(ProtocolAdapter):
    """Aave V3 protocol adapter for USDC lending."""

    def __init__(self, w3: AsyncWeb3, chain: Chain, config: dict):
        super().__init__(w3, chain, config)
        addrs = ADDRESSES[chain]
        self._pool = w3.eth.contract(
            address=w3.to_checksum_address(addrs["pool"]),
            abi=POOL_ABI,
        )
        self._usdc_addr = w3.to_checksum_address(addrs["usdc"])
        self._usdc = w3.eth.contract(address=self._usdc_addr, abi=ERC20_ABI)
        self._a_token_addr: str | None = None

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
        return (liquidity_rate / RAY) * 100

    async def get_utilization(self) -> Decimal:
        # Aave doesn't expose utilization directly in getReserveData
        # We can derive it from borrow rate vs supply rate, but for now
        # rely on DeFi Llama for this metric
        return Decimal("0")

    async def get_tvl(self) -> Decimal:
        # Would need to read aToken totalSupply + debt token supplies
        # For now, use DeFi Llama for TVL
        return Decimal("0")

    async def get_balance(self, address: str) -> Decimal:
        data = await self._get_reserve_data()
        a_token_addr = data[8]  # aTokenAddress
        a_token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(a_token_addr),
            abi=ERC20_ABI,
        )
        raw = await a_token.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()
        return Decimal(raw) / Decimal(10**USDC_DECIMALS)

    async def supply(self, amount: Decimal, sender: str) -> TxReceipt:
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._pool.functions.supply(
            self._usdc_addr, raw_amount, sender_addr, 0
        ).build_transaction({
            "from": sender_addr,
            "gas": 300_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Aave V3 supply: {amount} USDC | tx: {tx_hash.hex()}")

        return TxReceipt(
            tx_hash=tx_hash.hex(),
            action=ActionType.SUPPLY,
            protocol=self.name,
            chain=self.chain,
            amount=amount,
            gas_cost_usd=Decimal("0"),  # Calculate from receipt
            timestamp=datetime.now(tz=timezone.utc),
            block_number=receipt["blockNumber"],
        )

    async def withdraw(self, amount: Decimal, sender: str) -> TxReceipt:
        raw_amount = int(amount * Decimal(10**USDC_DECIMALS))
        sender_addr = self.w3.to_checksum_address(sender)

        tx = await self._pool.functions.withdraw(
            self._usdc_addr, raw_amount, sender_addr
        ).build_transaction({
            "from": sender_addr,
            "gas": 300_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Aave V3 withdraw: {amount} USDC | tx: {tx_hash.hex()}")

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
        pool_addr = self.w3.to_checksum_address(ADDRESSES[self.chain]["pool"])

        tx = await self._usdc.functions.approve(
            pool_addr, raw_amount
        ).build_transaction({
            "from": sender_addr,
            "gas": 100_000,
        })

        signed = self.w3.eth.account.sign_transaction(
            tx, private_key=self.config.get("private_key", "")
        )
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"Aave V3 approve: {amount} USDC | tx: {tx_hash.hex()}")

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
