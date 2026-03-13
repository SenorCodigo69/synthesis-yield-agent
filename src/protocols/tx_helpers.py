"""Shared transaction helpers for protocol adapters.

Handles private key validation, tx signing, receipt checking,
and amount validation — all in one place to avoid duplication.
"""

from decimal import Decimal

from web3 import AsyncWeb3


class MissingPrivateKeyError(Exception):
    """Raised when a write operation is attempted without a private key."""
    pass


class TransactionRevertedError(Exception):
    """Raised when a transaction is mined but reverted on-chain."""
    pass


def get_private_key(config: dict) -> str:
    """Extract and validate private key from config.

    Raises MissingPrivateKeyError if not set — never falls back to empty string.
    """
    key = config.get("private_key")
    if not key or not isinstance(key, str) or len(key.strip()) < 32:
        raise MissingPrivateKeyError(
            "Private key not configured. Set PRIVATE_KEY in .env for write operations."
        )
    return key.strip()


def validate_amount(amount: Decimal) -> None:
    """Validate amount is positive and sane before sending a transaction."""
    if not isinstance(amount, Decimal):
        raise ValueError(f"Amount must be Decimal, got {type(amount)}")
    if amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")
    if amount > Decimal("1_000_000_000"):  # $1B sanity cap
        raise ValueError(f"Amount exceeds sanity cap: {amount}")


async def sign_and_send(
    w3: AsyncWeb3,
    tx: dict,
    config: dict,
) -> tuple[str, dict]:
    """Sign a transaction, send it, wait for receipt, and check status.

    Returns (tx_hash_hex, receipt_dict).
    Raises TransactionRevertedError if the tx was mined but reverted.
    """
    private_key = get_private_key(config)

    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)

    # Check on-chain status — status=0 means revert
    if receipt.get("status") == 0:
        raise TransactionRevertedError(
            f"Transaction reverted on-chain: {tx_hash.hex()} "
            f"(block {receipt.get('blockNumber')})"
        )

    return tx_hash.hex(), receipt
