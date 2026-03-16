"""Impermanent loss tracker for concentrated Uniswap V3 LP positions.

Computes IL for concentrated positions (different formula than full-range)
and tracks whether fee income offsets the IL.
"""

import math
from dataclasses import dataclass

from . import lp_tick_math as tm


@dataclass
class ILReport:
    """Impermanent loss report for a concentrated LP position."""

    position_token_id: int
    entry_price_eth: float       # ETH price when minted (USDC per ETH)
    current_price_eth: float     # ETH price now
    tick_lower: int
    tick_upper: int
    price_lower: float           # Lower bound (USDC per ETH)
    price_upper: float           # Upper bound (USDC per ETH)
    value_if_held: float         # USD value if just held tokens
    value_in_pool: float         # USD value of LP position
    il_pct: float                # IL as percentage (0.0 = no IL, negative = loss)
    fees_earned_usd: float       # Fees collected
    net_pnl_usd: float           # fees + IL (IL is negative)
    is_profitable: bool


def compute_concentrated_il(
    entry_price: float,
    current_price: float,
    price_lower: float,
    price_upper: float,
) -> float:
    """Compute impermanent loss for a concentrated LP position.

    For a position between prices [pa, pb] with entry at p0 and current at p1:

    If p1 is within [pa, pb]:
        IL = 1 - [2*sqrt(p1/p0) - p1/p0 - 1] / [2*sqrt(pb/pa) / (sqrt(pb/pa) - 1) * (sqrt(p1/p0) - 1 + (1 - sqrt(p0/p1))*(pa/p0))]

    Simplified approximation using the standard concentrated IL formula:
        value_pool / value_hold ratio

    Returns IL as a fraction (e.g., -0.03 = 3% IL loss).
    """
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if price_lower <= 0 or price_upper <= 0 or price_lower >= price_upper:
        return 0.0

    pa = price_lower
    pb = price_upper
    p0 = entry_price
    p1 = current_price

    sqrt_pa = math.sqrt(pa)
    sqrt_pb = math.sqrt(pb)
    sqrt_p0 = math.sqrt(p0)
    sqrt_p1 = math.sqrt(p1)

    # Clamp p0 to be within range for initial deposit calculation
    p0_clamped = max(pa, min(pb, p0))
    sqrt_p0c = math.sqrt(p0_clamped)

    # Initial token amounts (normalized to L=1)
    # amount0 (WETH) = L * (1/sqrt(p0) - 1/sqrt(pb))
    # amount1 (USDC) = L * (sqrt(p0) - sqrt(pa))
    x0 = (1 / sqrt_p0c - 1 / sqrt_pb) if p0_clamped < pb else 0
    y0 = (sqrt_p0c - sqrt_pa) if p0_clamped > pa else 0

    # Value at entry (in USDC terms)
    value_hold = x0 * p1 + y0  # HODL value at current price

    if value_hold == 0:
        return 0.0

    # Current token amounts in pool
    p1_clamped = max(pa, min(pb, p1))
    sqrt_p1c = math.sqrt(p1_clamped)

    if p1 <= pa:
        # All WETH (price dropped below range)
        x1 = 1 / sqrt_pa - 1 / sqrt_pb
        y1 = 0
    elif p1 >= pb:
        # All USDC (price rose above range)
        x1 = 0
        y1 = sqrt_pb - sqrt_pa
    else:
        # In range
        x1 = 1 / sqrt_p1c - 1 / sqrt_pb
        y1 = sqrt_p1c - sqrt_pa

    value_pool = x1 * p1 + y1

    # IL = (value_pool / value_hold) - 1 (negative = loss)
    il = (value_pool / value_hold) - 1
    return il


def compute_il_report(
    token_id: int,
    entry_price: float,
    current_price: float,
    tick_lower: int,
    tick_upper: int,
    fees_weth: float,
    fees_usdc: float,
    position_value_usd: float,
) -> ILReport:
    """Compute a full IL report for a position.

    Args:
        token_id: NFT position token ID.
        entry_price: ETH price when minted (USDC per ETH).
        current_price: Current ETH price.
        tick_lower: Position lower tick.
        tick_upper: Position upper tick.
        fees_weth: WETH fees earned.
        fees_usdc: USDC fees earned.
        position_value_usd: Current position value in USD.
    """
    price_lower = tm.tick_to_eth_price(tick_lower)
    price_upper = tm.tick_to_eth_price(tick_upper)

    il_pct = compute_concentrated_il(entry_price, current_price, price_lower, price_upper)
    fees_usd = fees_weth * current_price + fees_usdc

    # Estimate HODL value (approximate using position value + IL)
    # value_hold = value_pool / (1 + il)
    if abs(1 + il_pct) > 1e-9:
        value_hold = position_value_usd / (1 + il_pct)
    else:
        value_hold = position_value_usd

    il_usd = position_value_usd - value_hold  # Negative = loss
    net_pnl = fees_usd + il_usd

    return ILReport(
        position_token_id=token_id,
        entry_price_eth=entry_price,
        current_price_eth=current_price,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        price_lower=price_lower,
        price_upper=price_upper,
        value_if_held=value_hold,
        value_in_pool=position_value_usd,
        il_pct=il_pct,
        fees_earned_usd=fees_usd,
        net_pnl_usd=net_pnl,
        is_profitable=net_pnl >= 0,
    )
