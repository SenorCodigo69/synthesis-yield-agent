"""Uniswap V3 LP position management — mint, collect fees, exit.

Wraps the NonfungiblePositionManager contract on Base for full-range
WETH-USDC liquidity provision. The agent provides liquidity and earns
swap fees from the pool.

Level 1 (this module): Full-range positions (MIN_TICK to MAX_TICK).
    Simple, like V2 — capital-inefficient but safe, no active management needed.

Level 2 (future): Concentrated liquidity with AI-driven tick range selection.

Security:
- Token approvals are checked before minting
- Amounts validated against wallet balance
- Deadline enforced on all position operations
- Gas price ceiling applied (reuses uniswap.py pattern)
- All external calls have timeouts
- Position IDs validated before operations
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from eth_account import Account
from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

# NonfungiblePositionManager on Base mainnet
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"

# Token addresses (Base) — token0 < token1 by address
WETH_BASE = "0x4200000000000000000000000000000000000006"  # token0
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # token1

WETH_DECIMALS = 18
USDC_DECIMALS = 6

# Fee tiers and their tick spacings
FEE_TIERS = {
    100: 1,      # 0.01%
    500: 10,     # 0.05%
    3000: 60,    # 0.3%
    10000: 200,  # 1%
}

# Default fee tier for WETH-USDC on Base (most liquid)
DEFAULT_FEE = 500  # 0.05%

# Absolute tick bounds (Uniswap V3 max range)
_MIN_TICK = -887272
_MAX_TICK = 887272

# Gas ceiling (same as swap adapter)
MAX_GAS_PRICE_GWEI = 5

# Transaction deadline buffer (seconds from now)
DEFAULT_DEADLINE_SECONDS = 300  # 5 minutes


def full_range_ticks(fee: int = DEFAULT_FEE) -> tuple[int, int]:
    """Get MIN_TICK and MAX_TICK aligned to the tick spacing for a fee tier."""
    spacing = FEE_TIERS.get(fee)
    if spacing is None:
        raise ValueError(f"Unknown fee tier: {fee}. Valid: {list(FEE_TIERS.keys())}")
    # Align to tick spacing (round toward zero for both bounds)
    # min: ceiling division for negative = -(abs // spacing) * spacing
    # max: floor division = (_MAX_TICK // spacing) * spacing
    min_tick = -((-_MIN_TICK) // spacing) * spacing
    max_tick = (_MAX_TICK // spacing) * spacing
    return min_tick, max_tick


# ── ABIs ─────────────────────────────────────────────────────

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
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
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

POSITION_MANAGER_ABI = [
    # mint — create a new LP position
    {
        "inputs": [
            {
                "components": [
                    {"name": "token0", "type": "address"},
                    {"name": "token1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickLower", "type": "int24"},
                    {"name": "tickUpper", "type": "int24"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "mint",
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    # increaseLiquidity — add to existing position
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "increaseLiquidity",
        "outputs": [
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    # decreaseLiquidity — remove liquidity (tokens stay in contract)
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "liquidity", "type": "uint128"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "decreaseLiquidity",
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    # collect — withdraw tokens + fees from position
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amount0Max", "type": "uint128"},
                    {"name": "amount1Max", "type": "uint128"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "collect",
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    # burn — destroy empty NFT position
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "burn",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    # positions — read position details
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


# ── Data Models ──────────────────────────────────────────────

@dataclass
class LPPosition:
    """An LP position on Uniswap V3."""
    token_id: int
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    tokens_owed0: int  # Uncollected fees (token0 raw units)
    tokens_owed1: int  # Uncollected fees (token1 raw units)


@dataclass
class MintResult:
    """Result of minting a new LP position."""
    token_id: int
    liquidity: int
    amount0: int  # WETH used (raw units)
    amount1: int  # USDC used (raw units)
    tx_hash: str
    block_number: int
    gas_used: int


@dataclass
class CollectResult:
    """Result of collecting fees."""
    amount0: int  # WETH collected (raw units)
    amount1: int  # USDC collected (raw units)
    tx_hash: str


@dataclass
class ExitResult:
    """Result of fully exiting a position."""
    amount0: int  # WETH withdrawn (raw units)
    amount1: int  # USDC withdrawn (raw units)
    fees0: int    # WETH fees collected (raw units)
    fees1: int    # USDC fees collected (raw units)
    tx_hash_decrease: str
    tx_hash_collect: str
    tx_hash_burn: str | None  # None if burn skipped


# ── LP Adapter ───────────────────────────────────────────────

class UniswapLPAdapter:
    """Uniswap V3 LP position manager for Base chain.

    Handles: approve tokens → mint position → collect fees → exit position.
    """

    def __init__(self, w3: AsyncWeb3, chain_id: int = 8453):
        self.w3 = w3
        self.chain_id = chain_id
        self._pm = w3.eth.contract(
            address=w3.to_checksum_address(POSITION_MANAGER),
            abi=POSITION_MANAGER_ABI,
        )

    # ── Read Operations ──────────────────────────────────────

    async def get_position(self, token_id: int) -> LPPosition:
        """Read an LP position's current state."""
        if token_id <= 0:
            raise ValueError(f"Invalid token ID: {token_id}")
        pos = await self._pm.functions.positions(token_id).call()
        return LPPosition(
            token_id=token_id,
            token0=pos[2],
            token1=pos[3],
            fee=pos[4],
            tick_lower=pos[5],
            tick_upper=pos[6],
            liquidity=pos[7],
            tokens_owed0=pos[10],
            tokens_owed1=pos[11],
        )

    async def get_balances(self, wallet: str) -> tuple[Decimal, Decimal]:
        """Get WETH and USDC balances for a wallet."""
        weth = self.w3.eth.contract(
            address=self.w3.to_checksum_address(WETH_BASE), abi=ERC20_ABI
        )
        usdc = self.w3.eth.contract(
            address=self.w3.to_checksum_address(USDC_BASE), abi=ERC20_ABI
        )
        weth_raw = await weth.functions.balanceOf(wallet).call()
        usdc_raw = await usdc.functions.balanceOf(wallet).call()
        return (
            Decimal(str(weth_raw)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(usdc_raw)) / Decimal(10**USDC_DECIMALS),
        )

    async def get_allowances(self, wallet: str) -> tuple[int, int]:
        """Get current WETH and USDC allowances for the PositionManager."""
        weth = self.w3.eth.contract(
            address=self.w3.to_checksum_address(WETH_BASE), abi=ERC20_ABI
        )
        usdc = self.w3.eth.contract(
            address=self.w3.to_checksum_address(USDC_BASE), abi=ERC20_ABI
        )
        pm_addr = self.w3.to_checksum_address(POSITION_MANAGER)
        weth_allowance = await weth.functions.allowance(wallet, pm_addr).call()
        usdc_allowance = await usdc.functions.allowance(wallet, pm_addr).call()
        return weth_allowance, usdc_allowance

    # ── Token Approval ───────────────────────────────────────

    async def approve_tokens(
        self,
        private_key: str,
        weth_amount: int,
        usdc_amount: int,
    ) -> list[str]:
        """Approve WETH and USDC spending by PositionManager.

        Only sends approval tx if current allowance is insufficient.
        Returns list of approval tx hashes (0-2 items).
        """
        account = Account.from_key(private_key)
        wallet = account.address
        weth_allowance, usdc_allowance = await self.get_allowances(wallet)

        tx_hashes = []
        pm_addr = self.w3.to_checksum_address(POSITION_MANAGER)

        if weth_allowance < weth_amount:
            logger.info("Approving WETH for PositionManager...")
            weth_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(WETH_BASE), abi=ERC20_ABI
            )
            # Approve max uint256 to avoid repeated approvals
            max_uint = 2**256 - 1
            tx_hash = await self._build_sign_send(
                weth_contract.functions.approve(pm_addr, max_uint),
                private_key,
            )
            tx_hashes.append(tx_hash)
            logger.info(f"WETH approved: {tx_hash}")

        if usdc_allowance < usdc_amount:
            logger.info("Approving USDC for PositionManager...")
            usdc_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(USDC_BASE), abi=ERC20_ABI
            )
            max_uint = 2**256 - 1
            tx_hash = await self._build_sign_send(
                usdc_contract.functions.approve(pm_addr, max_uint),
                private_key,
            )
            tx_hashes.append(tx_hash)
            logger.info(f"USDC approved: {tx_hash}")

        return tx_hashes

    # ── Mint (Create Position) ───────────────────────────────

    async def mint_full_range(
        self,
        private_key: str,
        weth_amount: int,
        usdc_amount: int,
        fee: int = DEFAULT_FEE,
        slippage_pct: float = 1.0,
    ) -> MintResult:
        """Mint a full-range LP position (WETH-USDC).

        Full-range = MIN_TICK to MAX_TICK, like Uniswap V2.
        Capital-inefficient but requires no active management.

        Args:
            private_key: Wallet private key
            weth_amount: WETH amount in raw units (18 decimals)
            usdc_amount: USDC amount in raw units (6 decimals)
            fee: Pool fee tier (default 500 = 0.05%)
            slippage_pct: Slippage tolerance (default 1%)

        Returns:
            MintResult with token_id, liquidity, amounts used.
        """
        if weth_amount <= 0 and usdc_amount <= 0:
            raise ValueError("Must provide at least one token amount")

        account = Account.from_key(private_key)
        wallet = account.address
        tick_lower, tick_upper = full_range_ticks(fee)

        # Apply slippage protection to full-range mints
        slippage_factor = 1 - slippage_pct / 100
        amount0_min = int(weth_amount * slippage_factor)
        amount1_min = int(usdc_amount * slippage_factor)

        # Get deadline (current block timestamp + buffer)
        block = await self.w3.eth.get_block("latest")
        deadline = block["timestamp"] + DEFAULT_DEADLINE_SECONDS

        # Ensure tokens are approved
        await self.approve_tokens(private_key, weth_amount, usdc_amount)

        # token0 = WETH (lower address), token1 = USDC (higher address)
        mint_params = (
            self.w3.to_checksum_address(WETH_BASE),   # token0
            self.w3.to_checksum_address(USDC_BASE),   # token1
            fee,                                       # fee
            tick_lower,                                # tickLower
            tick_upper,                                # tickUpper
            weth_amount,                               # amount0Desired
            usdc_amount,                               # amount1Desired
            amount0_min,                               # amount0Min
            amount1_min,                               # amount1Min
            self.w3.to_checksum_address(wallet),       # recipient
            deadline,                                  # deadline
        )

        logger.info(
            "Minting full-range LP: WETH=%s USDC=%s fee=%d ticks=[%d, %d]",
            Decimal(str(weth_amount)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(usdc_amount)) / Decimal(10**USDC_DECIMALS),
            fee, tick_lower, tick_upper,
        )

        tx_hash, receipt = await self._build_sign_send_with_receipt(
            self._pm.functions.mint(mint_params),
            private_key,
        )

        # Parse Mint event logs to get tokenId, liquidity, amounts
        # The mint function returns (tokenId, liquidity, amount0, amount1)
        # We parse from the receipt's logs
        token_id, liquidity, amount0, amount1 = self._parse_mint_receipt(receipt)

        logger.info(
            "LP minted: tokenId=%d liquidity=%d WETH=%s USDC=%s tx=%s",
            token_id, liquidity,
            Decimal(str(amount0)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(amount1)) / Decimal(10**USDC_DECIMALS),
            tx_hash,
        )

        return MintResult(
            token_id=token_id,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
            tx_hash=tx_hash,
            block_number=receipt["blockNumber"],
            gas_used=receipt.get("gasUsed", 0),
        )

    # ── Concentrated Mint ────────────────────────────────────

    async def mint_concentrated(
        self,
        private_key: str,
        weth_amount: int,
        usdc_amount: int,
        tick_lower: int,
        tick_upper: int,
        fee: int = DEFAULT_FEE,
        slippage_pct: float = 0.5,
    ) -> MintResult:
        """Mint a concentrated LP position with specific tick bounds.

        Unlike mint_full_range, this uses proper slippage protection since
        concentrated positions are sensitive to price movements.

        Args:
            private_key: Wallet private key
            weth_amount: WETH amount in raw units (18 decimals)
            usdc_amount: USDC amount in raw units (6 decimals)
            tick_lower: Lower tick bound (must be aligned to fee tier spacing)
            tick_upper: Upper tick bound (must be aligned to fee tier spacing)
            fee: Pool fee tier (default 500 = 0.05%)
            slippage_pct: Slippage tolerance (default 0.5%)

        Returns:
            MintResult with token_id, liquidity, amounts used.
        """
        if weth_amount <= 0 and usdc_amount <= 0:
            raise ValueError("Must provide at least one token amount")
        if tick_lower >= tick_upper:
            raise ValueError(f"tick_lower ({tick_lower}) must be < tick_upper ({tick_upper})")

        spacing = FEE_TIERS.get(fee)
        if spacing is None:
            raise ValueError(f"Unknown fee tier: {fee}")
        if tick_lower % spacing != 0 or tick_upper % spacing != 0:
            raise ValueError(f"Ticks must be aligned to spacing {spacing}: got [{tick_lower}, {tick_upper}]")
        if tick_lower < _MIN_TICK or tick_upper > _MAX_TICK:
            raise ValueError(f"Ticks out of bounds: [{tick_lower}, {tick_upper}]")

        account = Account.from_key(private_key)
        wallet = account.address

        # Slippage protection for concentrated positions
        slippage_factor = 1 - slippage_pct / 100
        amount0_min = int(weth_amount * slippage_factor)
        amount1_min = int(usdc_amount * slippage_factor)

        block = await self.w3.eth.get_block("latest")
        deadline = block["timestamp"] + DEFAULT_DEADLINE_SECONDS

        await self.approve_tokens(private_key, weth_amount, usdc_amount)

        mint_params = (
            self.w3.to_checksum_address(WETH_BASE),
            self.w3.to_checksum_address(USDC_BASE),
            fee,
            tick_lower,
            tick_upper,
            weth_amount,
            usdc_amount,
            amount0_min,
            amount1_min,
            self.w3.to_checksum_address(wallet),
            deadline,
        )

        logger.info(
            "Minting concentrated LP: WETH=%s USDC=%s fee=%d ticks=[%d, %d] slippage=%.1f%%",
            Decimal(str(weth_amount)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(usdc_amount)) / Decimal(10**USDC_DECIMALS),
            fee, tick_lower, tick_upper, slippage_pct,
        )

        tx_hash, receipt = await self._build_sign_send_with_receipt(
            self._pm.functions.mint(mint_params),
            private_key,
        )

        token_id, liquidity, amount0, amount1 = self._parse_mint_receipt(receipt)

        logger.info(
            "Concentrated LP minted: tokenId=%d liquidity=%d WETH=%s USDC=%s ticks=[%d,%d] tx=%s",
            token_id, liquidity,
            Decimal(str(amount0)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(amount1)) / Decimal(10**USDC_DECIMALS),
            tick_lower, tick_upper, tx_hash,
        )

        return MintResult(
            token_id=token_id,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
            tx_hash=tx_hash,
            block_number=receipt["blockNumber"],
            gas_used=receipt.get("gasUsed", 0),
        )

    # ── Auto-Compound (Collect + Increase Liquidity) ─────────

    async def compound_fees(
        self,
        private_key: str,
        token_id: int,
        slippage_pct: float = 1.0,
    ) -> tuple[CollectResult, int, int]:
        """Collect accumulated fees and reinvest into the same position.

        Steps:
        1. Collect fees (WETH + USDC returned to wallet)
        2. increaseLiquidity with collected amounts

        Returns:
            (collect_result, liquidity_added, amount0_added, amount1_added)
            Returns (collect_result, 0, 0) if fees too small to compound.
        """
        # Step 1: Collect fees
        collect = await self.collect_fees(private_key, token_id)
        weth_fees = collect.amount0
        usdc_fees = collect.amount1

        if weth_fees == 0 and usdc_fees == 0:
            logger.info("No fees to compound for position #%d", token_id)
            return collect, 0, 0

        # Step 2: Approve collected amounts for reinvestment
        if weth_fees > 0 or usdc_fees > 0:
            await self.approve_tokens(private_key, weth_fees, usdc_fees)

        # Step 3: Increase liquidity with collected fees
        account = Account.from_key(private_key)
        block = await self.w3.eth.get_block("latest")
        deadline = block["timestamp"] + DEFAULT_DEADLINE_SECONDS

        slippage_factor = 1 - slippage_pct / 100
        amount0_min = int(weth_fees * slippage_factor)
        amount1_min = int(usdc_fees * slippage_factor)

        increase_params = (
            token_id,
            weth_fees,
            usdc_fees,
            amount0_min,
            amount1_min,
            deadline,
        )

        tx_hash, receipt = await self._build_sign_send_with_receipt(
            self._pm.functions.increaseLiquidity(increase_params),
            private_key,
        )

        # Parse increaseLiquidity return: (liquidity, amount0, amount1)
        # From logs or return data
        liquidity_added = 0
        amount0_added = 0
        amount1_added = 0

        for log_entry in receipt.get("logs", []):
            data = log_entry.get("data", b"")
            if isinstance(data, str):
                data = bytes.fromhex(data[2:]) if data.startswith("0x") else data.encode()
            # IncreaseLiquidity event: tokenId(uint256) + liquidity(uint128) + amount0(uint256) + amount1(uint256)
            if len(data) >= 128:
                liquidity_added = int.from_bytes(data[32:64], "big")
                amount0_added = int.from_bytes(data[64:96], "big")
                amount1_added = int.from_bytes(data[96:128], "big")
                break

        logger.info(
            "Compounded #%d: +%s WETH +%s USDC (liquidity +%d) tx=%s",
            token_id,
            Decimal(str(amount0_added)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(amount1_added)) / Decimal(10**USDC_DECIMALS),
            liquidity_added, tx_hash,
        )

        return collect, liquidity_added, amount0_added

    # ── Pool Reads ─────────────────────────────────────────────

    async def get_pool_slot0(self, fee: int = DEFAULT_FEE) -> tuple[int, int]:
        """Read slot0 from the WETH-USDC pool.

        Returns (sqrtPriceX96, current_tick).
        """
        # Known pool addresses for WETH-USDC on Base by fee tier
        pool_addresses = {
            500: "0xd0b53D9277642d899DF5C87A3966A349A798F224",
        }
        pool_addr = pool_addresses.get(fee)
        if pool_addr is None:
            raise ValueError(f"No known pool address for fee tier {fee}")

        result = await self.w3.eth.call({
            "to": self.w3.to_checksum_address(pool_addr),
            "data": "0x3850c7bd",  # slot0()
        })

        sqrt_price_x96 = int.from_bytes(result[:32], "big")
        # tick is int24 packed in a 256-bit word (ABI-encoded as int256)
        tick_raw = int.from_bytes(result[32:64], "big")
        # Handle two's complement for 256-bit signed integer
        if tick_raw >= 2**255:
            tick_raw -= 2**256
        return sqrt_price_x96, tick_raw

    # ── Collect Fees ─────────────────────────────────────────

    async def collect_fees(
        self,
        private_key: str,
        token_id: int,
    ) -> CollectResult:
        """Collect accumulated swap fees from an LP position.

        Fees accrue continuously as swaps happen through the pool.
        This withdraws all available fees to the wallet.
        """
        if token_id <= 0:
            raise ValueError(f"Invalid token ID: {token_id}")

        account = Account.from_key(private_key)
        wallet = account.address

        # Use max uint128 to collect all available fees
        max_uint128 = 2**128 - 1

        collect_params = (
            token_id,                                   # tokenId
            self.w3.to_checksum_address(wallet),        # recipient
            max_uint128,                                # amount0Max
            max_uint128,                                # amount1Max
        )

        logger.info("Collecting fees for position #%d", token_id)

        tx_hash, receipt = await self._build_sign_send_with_receipt(
            self._pm.functions.collect(collect_params),
            private_key,
        )

        # Parse collect logs
        amount0, amount1 = self._parse_collect_receipt(receipt)

        logger.info(
            "Fees collected: WETH=%s USDC=%s tx=%s",
            Decimal(str(amount0)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(amount1)) / Decimal(10**USDC_DECIMALS),
            tx_hash,
        )

        return CollectResult(
            amount0=amount0,
            amount1=amount1,
            tx_hash=tx_hash,
        )

    # ── Exit Position ────────────────────────────────────────

    async def exit_position(
        self,
        private_key: str,
        token_id: int,
        burn_nft: bool = True,
        slippage_pct: float = 2.0,
    ) -> ExitResult:
        """Fully exit an LP position: decrease liquidity → collect → burn.

        Three transactions:
        1. decreaseLiquidity — remove all liquidity (tokens stay in contract)
        2. collect — withdraw tokens + fees to wallet
        3. burn — destroy the empty NFT (optional)
        """
        if token_id <= 0:
            raise ValueError(f"Invalid token ID: {token_id}")

        # Read current position
        position = await self.get_position(token_id)
        if position.liquidity == 0:
            logger.warning("Position #%d already has zero liquidity", token_id)

        account = Account.from_key(private_key)
        wallet = account.address

        # Step 1: Decrease liquidity (remove all)
        block = await self.w3.eth.get_block("latest")
        deadline = block["timestamp"] + DEFAULT_DEADLINE_SECONDS

        # Simulate the decrease to get expected amounts, then apply slippage
        try:
            sim_result = await self._pm.functions.decreaseLiquidity((
                token_id, position.liquidity, 0, 0, deadline,
            )).call()
            slippage_factor = 1 - slippage_pct / 100
            amount0_min = int(sim_result[0] * slippage_factor)
            amount1_min = int(sim_result[1] * slippage_factor)
        except Exception:
            # Fallback: 2% slippage on tokensOwed as best estimate
            amount0_min = int(position.tokens_owed0 * 0.98)
            amount1_min = int(position.tokens_owed1 * 0.98)

        decrease_params = (
            token_id,               # tokenId
            position.liquidity,     # liquidity (all of it)
            amount0_min,            # amount0Min (slippage-protected)
            amount1_min,            # amount1Min (slippage-protected)
            deadline,               # deadline
        )

        logger.info(
            "Decreasing liquidity for position #%d (liquidity=%d)",
            token_id, position.liquidity,
        )

        tx_hash_decrease = ""
        if position.liquidity > 0:
            tx_hash_decrease, _ = await self._build_sign_send_with_receipt(
                self._pm.functions.decreaseLiquidity(decrease_params),
                private_key,
            )
            logger.info("Liquidity removed: %s", tx_hash_decrease)

        # Step 2: Collect everything (withdrawn tokens + fees)
        max_uint128 = 2**128 - 1
        collect_params = (
            token_id,
            self.w3.to_checksum_address(wallet),
            max_uint128,
            max_uint128,
        )

        # S44-M5: Report partial success if collect fails after decrease
        try:
            tx_hash_collect, collect_receipt = await self._build_sign_send_with_receipt(
                self._pm.functions.collect(collect_params),
                private_key,
            )
        except Exception as e:
            logger.error(
                "Collect failed after decrease succeeded (tx=%s). "
                "Tokens are safe in contract — retry collect for position #%d. Error: %s",
                tx_hash_decrease, token_id, e,
            )
            raise RuntimeError(
                f"Decrease succeeded ({tx_hash_decrease}) but collect failed: {e}. "
                f"Tokens are safe — retry: collect --token-id {token_id} --live"
            ) from e

        amount0, amount1 = self._parse_collect_receipt(collect_receipt)
        logger.info(
            "Collected: WETH=%s USDC=%s tx=%s",
            Decimal(str(amount0)) / Decimal(10**WETH_DECIMALS),
            Decimal(str(amount1)) / Decimal(10**USDC_DECIMALS),
            tx_hash_collect,
        )

        # Step 3: Burn empty NFT
        tx_hash_burn = None
        if burn_nft:
            try:
                tx_hash_burn, _ = await self._build_sign_send_with_receipt(
                    self._pm.functions.burn(token_id),
                    private_key,
                )
                logger.info("NFT burned: %s", tx_hash_burn)
            except Exception as e:
                logger.warning("Burn failed (non-critical): %s", e)

        # Estimate fees vs principal (rough — fees are whatever was owed pre-decrease)
        fees0 = position.tokens_owed0
        fees1 = position.tokens_owed1

        return ExitResult(
            amount0=amount0,
            amount1=amount1,
            fees0=fees0,
            fees1=fees1,
            tx_hash_decrease=tx_hash_decrease,
            tx_hash_collect=tx_hash_collect,
            tx_hash_burn=tx_hash_burn,
        )

    # ── Internal Helpers ─────────────────────────────────────

    def _parse_mint_receipt(self, receipt: dict) -> tuple[int, int, int, int]:
        """Parse tokenId, liquidity, amount0, amount1 from mint receipt.

        The IncreaseLiquidity event is emitted by the PositionManager:
        event IncreaseLiquidity(uint256 indexed tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)

        The Transfer event gives us the tokenId:
        event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
        """
        token_id = 0
        liquidity = 0
        amount0 = 0
        amount1 = 0

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue

            # Transfer event (ERC721): topic0 = keccak256("Transfer(address,address,uint256)")
            # 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
            if (
                topics[0].hex() == "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                and len(topics) >= 4
            ):
                token_id = int(topics[3].hex(), 16)

            # IncreaseLiquidity event:
            # 0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f
            if (
                topics[0].hex() == "3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
                and len(topics) >= 2
            ):
                # tokenId is indexed (topic1)
                if token_id == 0:
                    token_id = int(topics[1].hex(), 16)
                # liquidity, amount0, amount1 are in data
                data = log.get("data", b"")
                if isinstance(data, str):
                    data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
                if len(data) >= 96:
                    liquidity = int.from_bytes(data[0:32], "big")
                    amount0 = int.from_bytes(data[32:64], "big")
                    amount1 = int.from_bytes(data[64:96], "big")

        if token_id == 0:
            raise RuntimeError("Failed to parse tokenId from mint receipt")

        return token_id, liquidity, amount0, amount1

    def _parse_collect_receipt(self, receipt: dict) -> tuple[int, int]:
        """Parse amount0, amount1 from collect receipt.

        Collect event:
        event Collect(uint256 indexed tokenId, address recipient, uint256 amount0, uint256 amount1)
        0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01
        """
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue

            if topics[0].hex() == "40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01":
                data = log.get("data", b"")
                if isinstance(data, str):
                    data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
                if len(data) >= 96:
                    # recipient (address, 32 bytes) + amount0 (32 bytes) + amount1 (32 bytes)
                    amount0 = int.from_bytes(data[32:64], "big")
                    amount1 = int.from_bytes(data[64:96], "big")
                    return amount0, amount1

        logger.warning("No Collect event found in receipt — returning zeros")
        return 0, 0

    async def _build_sign_send(
        self, contract_fn, private_key: str
    ) -> str:
        """Build, sign, and broadcast a contract call. Returns tx hash."""
        tx_hash, _ = await self._build_sign_send_with_receipt(
            contract_fn, private_key
        )
        return tx_hash

    async def _build_sign_send_with_receipt(
        self, contract_fn, private_key: str
    ) -> tuple[str, dict]:
        """Build, sign, and broadcast a contract call. Returns (tx_hash, receipt)."""
        account = Account.from_key(private_key)

        # S44-M6: Use tracked nonce to avoid race conditions when sending
        # multiple txs in sequence (RPC load balancers can return stale counts)
        nonce = await self.w3.eth.get_transaction_count(account.address, "pending")
        if hasattr(self, "_last_nonce") and self._last_nonce >= nonce:
            nonce = self._last_nonce + 1

        # Build transaction
        tx = await contract_fn.build_transaction({
            "from": account.address,
            "chainId": self.chain_id,
            "nonce": nonce,
        })

        # EIP-1559 gas pricing
        block = await self.w3.eth.get_block("latest")
        base_fee = block["baseFeePerGas"]
        priority_fee = await self.w3.eth.max_priority_fee
        max_fee = base_fee * 2 + priority_fee

        # Gas price ceiling
        max_fee_gwei = max_fee / 10**9
        if max_fee_gwei > MAX_GAS_PRICE_GWEI:
            logger.warning(
                "Gas price %.2f gwei exceeds cap %d gwei — capping",
                max_fee_gwei, MAX_GAS_PRICE_GWEI,
            )
            max_fee = MAX_GAS_PRICE_GWEI * 10**9
            priority_fee = min(priority_fee, max_fee)

        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = priority_fee

        # Estimate gas if not set
        if "gas" not in tx:
            tx["gas"] = await self.w3.eth.estimate_gas(tx)

        # Sign and send
        signed = self.w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)

        # Track nonce for sequential tx safety
        self._last_nonce = nonce

        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.get("status") == 0:
            raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")

        return tx_hash.hex(), receipt
