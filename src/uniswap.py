"""Uniswap Trading API adapter — swap, quote, and Permit2 integration.

Uses the Uniswap Trading API (trade-api.gateway.uniswap.org) for
optimal routing across V2/V3/V4 pools and UniswapX on Base chain.

Flow: check_approval → quote → sign Permit2 → swap → broadcast

Security:
- Validates swap tx `to` address against known Uniswap contracts
- Validates Permit2 domain (chain ID + verifying contract)
- Validates quote amount_out > 0
- Enforces gas price ceiling
- API timeouts on all requests
- Error messages sanitized (no API response body leaks)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

API_BASE = "https://trade-api.gateway.uniswap.org/v1"
API_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Well-known addresses on Base (chain 8453)
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
UNIVERSAL_ROUTER = "0x6ff5693b99212da76ad316178a184ab56d299b43"
PROXY_NO_PERMIT2 = "0x02E5be68D46DAc0B524905bfF209cf47EE6dB2a9"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"

USDC_DECIMALS = 6
WETH_DECIMALS = 18
BASE_CHAIN_ID = 8453

# Allowlist of valid `to` addresses for swap transactions
ALLOWED_SWAP_TARGETS = {
    UNIVERSAL_ROUTER.lower(),
    PROXY_NO_PERMIT2.lower(),
    PERMIT2.lower(),
    USDC_BASE.lower(),
    WETH_BASE.lower(),
}

# Gas price ceiling (gwei) — prevents overspend on gas spikes
MAX_GAS_PRICE_GWEI = 5  # Base mainnet is typically <0.01 gwei

VALID_ROUTING = {"CLASSIC", "WRAP", "UNWRAP", "BRIDGE"}


@dataclass
class SwapQuote:
    """Parsed quote from the Uniswap Trading API."""
    quote_raw: dict
    permit_data: dict | None
    routing: str
    amount_in: str
    amount_out: str
    token_in: str
    token_out: str
    gas_fee: str | None
    request_id: str


@dataclass
class SwapResult:
    """Result of a completed swap."""
    tx_hash: str
    block_number: int
    amount_in: Decimal
    amount_out: Decimal
    token_in: str
    token_out: str
    gas_used: int
    routing: str


class UniswapAdapter:
    """Uniswap Trading API adapter for Base chain.

    Handles the full swap flow: approval check, quoting, Permit2
    signing, and transaction execution.
    """

    def __init__(self, api_key: str, w3: AsyncWeb3, chain_id: int = BASE_CHAIN_ID):
        self._api_key = api_key
        self.w3 = w3
        self.chain_id = chain_id

    def __repr__(self) -> str:
        return f"UniswapAdapter(chain={self.chain_id})"

    @property
    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    async def check_approval(
        self,
        session: aiohttp.ClientSession,
        wallet: str,
        token: str,
        amount: str,
    ) -> dict | None:
        """Check if Permit2 is approved to spend the token.

        Returns the approval transaction dict if needed, None if already approved.
        """
        payload = {
            "walletAddress": wallet,
            "token": token,
            "amount": amount,
            "chainId": self.chain_id,
            "includeGasInfo": True,
        }
        async with session.post(
            f"{API_BASE}/check_approval",
            json=payload,
            headers=self._headers,
            timeout=API_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"check_approval failed ({resp.status})")
            data = await resp.json()

        approval_tx = data.get("approval")
        if approval_tx:
            logger.info(f"Permit2 approval needed for {token}")
            return approval_tx
        logger.info(f"Permit2 already approved for {token}")
        return None

    async def get_quote(
        self,
        session: aiohttp.ClientSession,
        token_in: str,
        token_out: str,
        amount: str,
        wallet: str,
        swap_type: str = "EXACT_INPUT",
        slippage: float = 0.5,
    ) -> SwapQuote:
        """Get a swap quote with optimal routing."""
        payload = {
            "type": swap_type,
            "amount": amount,
            "tokenIn": token_in,
            "tokenOut": token_out,
            "tokenInChainId": self.chain_id,
            "tokenOutChainId": self.chain_id,
            "swapper": wallet,
            "slippageTolerance": slippage,
            "routingPreference": "BEST_PRICE",
        }
        async with session.post(
            f"{API_BASE}/quote",
            json=payload,
            headers=self._headers,
            timeout=API_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"quote failed ({resp.status})")
            data = await resp.json()

        quote = data.get("quote", {})
        routing = data.get("routing", "CLASSIC")
        permit_data = data.get("permitData")

        amount_out = quote.get("output", {}).get("amount", "0")

        # Validate quote is non-zero
        if not amount_out or amount_out == "0":
            raise RuntimeError("Quote returned zero output amount — aborting")

        # Validate routing type
        if routing not in VALID_ROUTING:
            raise RuntimeError(
                f"Unsupported routing type: {routing}. "
                f"Expected one of {VALID_ROUTING}"
            )

        return SwapQuote(
            quote_raw=quote,
            permit_data=permit_data,
            routing=routing,
            amount_in=quote.get("input", {}).get("amount", amount),
            amount_out=amount_out,
            token_in=token_in,
            token_out=token_out,
            gas_fee=data.get("gasFee"),
            request_id=data.get("requestId", ""),
        )

    def sign_permit2(self, quote: SwapQuote, private_key: str) -> str | None:
        """Sign the Permit2 EIP-712 typed data from the quote.

        Validates domain chain ID and verifying contract before signing.
        Returns the hex signature, or None if no permit data.
        """
        if not quote.permit_data:
            return None

        domain = quote.permit_data["domain"]
        types = quote.permit_data["types"]
        values = quote.permit_data["values"]

        # Validate Permit2 domain before signing
        if domain.get("chainId") != self.chain_id:
            raise RuntimeError(
                f"Permit2 domain chain ID mismatch: "
                f"expected {self.chain_id}, got {domain.get('chainId')}"
            )
        verifier = domain.get("verifyingContract", "").lower()
        if verifier != PERMIT2.lower():
            raise RuntimeError(
                f"Permit2 verifying contract mismatch: "
                f"expected {PERMIT2}, got {domain.get('verifyingContract')}"
            )

        account = Account.from_key(private_key)
        signable = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=values,
        )
        signed = account.sign_message(signable)
        sig_hex = signed.signature.hex()
        return sig_hex if sig_hex.startswith("0x") else f"0x{sig_hex}"

    async def execute_swap(
        self,
        session: aiohttp.ClientSession,
        quote: SwapQuote,
        signature: str | None,
    ) -> dict:
        """Execute the swap via the Trading API.

        Returns the transaction dict to sign and broadcast.
        """
        payload = {
            "quote": quote.quote_raw,
            "simulateTransaction": True,
            "refreshGasPrice": True,
        }
        if signature and quote.permit_data:
            payload["signature"] = signature
            payload["permitData"] = quote.permit_data

        async with session.post(
            f"{API_BASE}/swap",
            json=payload,
            headers=self._headers,
            timeout=API_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"swap endpoint failed ({resp.status})")
            data = await resp.json()

        swap_tx = data.get("swap")
        if not swap_tx or not swap_tx.get("data"):
            raise RuntimeError("Empty swap transaction returned by API")

        # Validate swap target is a known Uniswap contract
        target = swap_tx.get("to", "").lower()
        if target not in ALLOWED_SWAP_TARGETS:
            raise RuntimeError(
                f"Swap tx targets unknown contract {swap_tx.get('to')} — "
                f"rejecting to prevent fund theft. "
                f"Allowed: {[UNIVERSAL_ROUTER, PROXY_NO_PERMIT2]}"
            )

        return swap_tx

    async def swap(
        self,
        session: aiohttp.ClientSession,
        token_in: str,
        token_out: str,
        amount: str,
        private_key: str,
        slippage: float = 0.5,
    ) -> SwapResult:
        """Full swap flow: check approval → quote → sign → execute → broadcast.

        Args:
            token_in: Input token address (use NATIVE_ETH for ETH)
            token_out: Output token address
            amount: Amount in smallest unit (e.g., "1000000" for 1 USDC)
            private_key: Wallet private key for signing
            slippage: Slippage tolerance percentage (0.5 = 0.5%)

        Returns:
            SwapResult with tx hash and quoted amounts.
        """
        # Validate amount
        try:
            amount_int = int(amount)
            if amount_int <= 0:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f"Amount must be a positive integer string, got: {amount!r}")

        account = Account.from_key(private_key)
        wallet = account.address

        # Step 1: Check and handle Permit2 approval (skip for native ETH)
        if token_in != NATIVE_ETH:
            approval_tx = await self.check_approval(session, wallet, token_in, amount)
            if approval_tx:
                logger.info("Sending Permit2 approval transaction...")
                await self._sign_and_broadcast(approval_tx, private_key)
                logger.info("Permit2 approval confirmed")

        # Step 2: Get quote
        logger.info(f"Getting quote: {amount} {token_in} → {token_out}")
        quote = await self.get_quote(
            session, token_in, token_out, amount, wallet, slippage=slippage,
        )
        logger.info(
            f"Quote: {quote.amount_in} → {quote.amount_out} "
            f"(routing: {quote.routing})"
        )

        # Step 3: Sign Permit2 if needed
        signature = self.sign_permit2(quote, private_key)

        # Step 4: Get swap transaction
        swap_tx = await self.execute_swap(session, quote, signature)

        # Step 5: Sign and broadcast
        logger.info("Broadcasting swap transaction...")
        tx_hash, receipt = await self._sign_and_broadcast(swap_tx, private_key)

        logger.info(
            f"Swap complete: tx={tx_hash} block={receipt['blockNumber']}"
        )

        return SwapResult(
            tx_hash=tx_hash,
            block_number=receipt["blockNumber"],
            amount_in=Decimal(quote.amount_in),
            amount_out=Decimal(quote.amount_out),
            token_in=token_in,
            token_out=token_out,
            gas_used=receipt.get("gasUsed", 0),
            routing=quote.routing,
        )

    async def _sign_and_broadcast(
        self, tx_dict: dict, private_key: str
    ) -> tuple[str, dict]:
        """Sign a transaction dict and broadcast to Base chain."""
        account = Account.from_key(private_key)

        def _parse_int(val) -> int:
            if val is None:
                return 0
            if isinstance(val, str):
                return int(val, 16) if val.startswith("0x") else int(val)
            return int(val)

        # Build web3-compatible tx (EIP-1559)
        tx: dict = {
            "to": self.w3.to_checksum_address(tx_dict["to"]),
            "from": account.address,
            "data": tx_dict.get("data", "0x"),
            "value": _parse_int(tx_dict.get("value", 0)),
            "chainId": self.chain_id,  # Always enforce our chain ID
        }

        # Use gas params from API if provided, otherwise estimate
        if tx_dict.get("gasLimit"):
            tx["gas"] = _parse_int(tx_dict["gasLimit"])
        else:
            tx["gas"] = await self.w3.eth.estimate_gas(tx)

        # EIP-1559 gas pricing — use API values or fetch from chain
        if tx_dict.get("maxFeePerGas"):
            max_fee = _parse_int(tx_dict["maxFeePerGas"])
            priority_fee = _parse_int(tx_dict.get("maxPriorityFeePerGas", 0))
        else:
            base_fee = (await self.w3.eth.get_block("latest"))["baseFeePerGas"]
            max_fee = base_fee * 2
            priority_fee = await self.w3.eth.max_priority_fee

        # Gas price ceiling — prevent overspend
        max_fee_gwei = max_fee / 10**9
        if max_fee_gwei > MAX_GAS_PRICE_GWEI:
            logger.warning(
                f"Gas price {max_fee_gwei:.2f} gwei exceeds cap "
                f"{MAX_GAS_PRICE_GWEI} gwei — capping"
            )
            max_fee = MAX_GAS_PRICE_GWEI * 10**9
            priority_fee = min(priority_fee, max_fee)

        # Validate gas values are positive
        if max_fee <= 0:
            base_fee = (await self.w3.eth.get_block("latest"))["baseFeePerGas"]
            max_fee = base_fee * 2
            priority_fee = await self.w3.eth.max_priority_fee

        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = priority_fee

        # Get nonce
        tx["nonce"] = await self.w3.eth.get_transaction_count(
            account.address, "pending"
        )

        # Sign and send
        signed = self.w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)

        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.get("status") == 0:
            raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")

        return tx_hash.hex(), receipt
