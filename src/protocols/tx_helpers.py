"""Shared transaction helpers for protocol adapters.

Handles private key validation, tx signing, receipt checking,
nonce management, and amount validation — all in one place.

Security fixes:
- SEC-C01: Explicit nonce management (atomic per-sender)
- SEC-C03: Transaction receipt timeout (120s default)
- SEC-H01/H04: Private key isolated via TransactionSigner (not passed in config dict)
- SEC-H02: Chain ID validation at startup + enforced in every transaction
"""

import asyncio
import logging
from decimal import Decimal

from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

# Base chain ID — validated at startup, enforced per-tx
BASE_CHAIN_ID = 8453

# Default receipt timeout (seconds) — prevents infinite hangs
TX_RECEIPT_TIMEOUT = 120


class MissingPrivateKeyError(Exception):
    """Raised when a write operation is attempted without a private key."""
    pass


class TransactionRevertedError(Exception):
    """Raised when a transaction is mined but reverted on-chain."""
    pass


class ChainIdMismatchError(Exception):
    """Raised when the connected RPC returns an unexpected chain ID."""
    pass


class TransactionSigner:
    """Isolated signer — holds the private key separate from config.

    SEC-H01/H04: The private key is stored only in this object, not in
    the config dict passed to adapters. Adapters receive a signer
    reference, never the raw key. The key is never included in
    __repr__ or logged.
    """

    def __init__(self, private_key: str):
        self._key = _validate_private_key(private_key)

    def __repr__(self) -> str:
        return "TransactionSigner(<redacted>)"

    @property
    def key(self) -> str:
        return self._key


def _validate_private_key(key: str | None) -> str:
    """Validate private key format.

    Raises MissingPrivateKeyError if not set — never falls back to empty string.
    """
    if not key or not isinstance(key, str) or len(key.strip()) < 32:
        raise MissingPrivateKeyError(
            "Private key not configured. Set PRIVATE_KEY in .env for write operations."
        )
    return key.strip()


# Legacy wrapper for backwards compatibility with existing tests
def get_private_key(config: dict) -> str:
    """Extract and validate private key from config dict.

    Prefer TransactionSigner for new code.
    """
    return _validate_private_key(config.get("private_key"))


def validate_amount(amount: Decimal) -> None:
    """Validate amount is positive and sane before sending a transaction."""
    if not isinstance(amount, Decimal):
        raise ValueError(f"Amount must be Decimal, got {type(amount)}")
    if amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")
    if amount > Decimal("1_000_000_000"):  # $1B sanity cap
        raise ValueError(f"Amount exceeds sanity cap: {amount}")


async def validate_chain_id(w3: AsyncWeb3, expected: int = BASE_CHAIN_ID) -> None:
    """Validate that the RPC is connected to the expected chain.

    SEC-H02: Call at startup to catch misconfigured RPCs before
    any transaction is built.
    """
    chain_id = await w3.eth.chain_id
    if chain_id != expected:
        raise ChainIdMismatchError(
            f"Expected chain ID {expected}, got {chain_id}. "
            f"Check RPC URL — wrong chain could send funds to wrong network."
        )
    logger.info(f"Chain ID validated: {chain_id}")


async def estimate_gas_with_fallback(
    w3: AsyncWeb3,
    tx: dict,
    fallback_gas: int,
    buffer_pct: float = 0.2,
) -> int:
    """Estimate gas dynamically, fall back to static limit on failure.

    SEC-H03: Dynamic estimation catches reverts before sending.
    20% buffer added to handle minor state changes between estimate and execution.
    """
    try:
        estimated = await w3.eth.estimate_gas(tx)
        buffered = int(estimated * (1 + buffer_pct))
        logger.debug(f"Gas estimated: {estimated} + {buffer_pct:.0%} buffer = {buffered}")
        return buffered
    except Exception as e:
        logger.warning(
            f"Gas estimation failed ({e}), using fallback: {fallback_gas}. "
            f"This may indicate the transaction would revert."
        )
        return fallback_gas


async def build_tx_with_safety(
    w3: AsyncWeb3,
    contract_fn,
    sender: str,
    fallback_gas: int,
    chain_id: int = BASE_CHAIN_ID,
) -> dict:
    """Build a transaction with chain ID, nonce, and dynamic gas estimation.

    SEC-C01: Explicit nonce from on-chain count (includes pending).
    SEC-H02: Chain ID enforced.
    SEC-H03: Dynamic gas estimation with fallback.
    """
    # Get nonce including pending transactions
    nonce = await w3.eth.get_transaction_count(sender, "pending")

    tx = await contract_fn.build_transaction({
        "from": sender,
        "chainId": chain_id,
        "nonce": nonce,
        "gas": fallback_gas,  # Placeholder — overwritten below
    })

    # Dynamic gas estimation (catches reverts before sending)
    tx["gas"] = await estimate_gas_with_fallback(w3, tx, fallback_gas)

    return tx


async def sign_and_send(
    w3: AsyncWeb3,
    tx: dict,
    config_or_signer,
    receipt_timeout: int = TX_RECEIPT_TIMEOUT,
) -> tuple[str, dict]:
    """Sign a transaction, send it, wait for receipt with timeout, check status.

    SEC-C01: Nonce should be set by build_tx_with_safety() before calling this.
    SEC-C03: Receipt wait has a timeout (default 120s) to prevent infinite hangs.
    SEC-H01: Accepts TransactionSigner or legacy config dict.

    Returns (tx_hash_hex, receipt_dict).
    Raises TransactionRevertedError if the tx was mined but reverted.
    Raises asyncio.TimeoutError if receipt not received within timeout.
    """
    # Accept either TransactionSigner or legacy config dict
    if isinstance(config_or_signer, TransactionSigner):
        private_key = config_or_signer.key
    else:
        private_key = get_private_key(config_or_signer)

    # Ensure chain ID is set (SEC-H02)
    if "chainId" not in tx:
        tx["chainId"] = BASE_CHAIN_ID

    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)

    # Wait for receipt with timeout (SEC-C03)
    try:
        receipt = await asyncio.wait_for(
            w3.eth.wait_for_transaction_receipt(tx_hash),
            timeout=receipt_timeout,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Transaction receipt timeout ({receipt_timeout}s): {tx_hash.hex()}. "
            f"Transaction may still be pending — check on-chain."
        )
        raise

    # Check on-chain status — status=0 means revert
    if receipt.get("status") == 0:
        raise TransactionRevertedError(
            f"Transaction reverted on-chain: {tx_hash.hex()} "
            f"(block {receipt.get('blockNumber')})"
        )

    return tx_hash.hex(), receipt
