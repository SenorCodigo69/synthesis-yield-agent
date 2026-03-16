"""Uniswap V3 tick ↔ price conversion utilities.

Handles the WETH-USDC pair on Base where:
- token0 = WETH (18 decimals)
- token1 = USDC (6 decimals)
- price = token1/token0 (USDC per WETH)

The 10^12 factor accounts for the decimal difference.
"""

import math

# Absolute tick bounds (Uniswap V3)
MIN_TICK = -887272
MAX_TICK = 887272

# Float-safe tick bounds: 1.0001^tick overflows float64 beyond ~±709783
_FLOAT_SAFE_MAX_TICK = 709780
_FLOAT_SAFE_MIN_TICK = -709780

# Fee tier → tick spacing
FEE_TICK_SPACING = {
    100: 1,      # 0.01%
    500: 10,     # 0.05%
    3000: 60,    # 0.3%
    10000: 200,  # 1%
}


def _check_tick_float_safe(tick: int) -> None:
    """Raise OverflowError if tick would overflow float64."""
    if tick > _FLOAT_SAFE_MAX_TICK or tick < _FLOAT_SAFE_MIN_TICK:
        raise OverflowError(f"Tick {tick} outside float-safe range [{_FLOAT_SAFE_MIN_TICK}, {_FLOAT_SAFE_MAX_TICK}]")


def tick_to_price(tick: int) -> float:
    """Convert a Uniswap V3 tick to raw price (token1/token0 in smallest units)."""
    _check_tick_float_safe(tick)
    return 1.0001 ** tick


def price_to_tick(price: float) -> int:
    """Convert a raw price to the nearest Uniswap V3 tick."""
    if price <= 0:
        raise ValueError("Price must be positive")
    return int(math.floor(math.log(price) / math.log(1.0001)))


def tick_to_eth_price(tick: int) -> float:
    """Convert tick to human-readable USDC-per-ETH price.

    For WETH(18)/USDC(6): price_usdc_per_eth = 1.0001^tick / 10^12
    """
    _check_tick_float_safe(tick)
    result = 1.0001 ** tick / 1e12
    if not math.isfinite(result):
        raise OverflowError(f"tick_to_eth_price({tick}) produced non-finite result")
    return result


def eth_price_to_tick(price_usdc_per_eth: float) -> int:
    """Convert USDC-per-ETH price to the nearest tick.

    Inverse of tick_to_eth_price.
    """
    if price_usdc_per_eth <= 0:
        raise ValueError("Price must be positive")
    raw_price = price_usdc_per_eth * 1e12
    return int(math.floor(math.log(raw_price) / math.log(1.0001)))


def align_tick(tick: int, spacing: int, round_down: bool = True) -> int:
    """Round tick to the nearest valid value for a given tick spacing.

    Args:
        tick: Raw tick value.
        spacing: Tick spacing for the fee tier.
        round_down: If True, round toward negative infinity. If False, round up.
    """
    if spacing <= 0:
        raise ValueError("Tick spacing must be positive")
    if round_down:
        return (tick // spacing) * spacing
    return -(-tick // spacing) * spacing


def aligned_range(
    price_lower: float, price_upper: float, fee: int = 500
) -> tuple[int, int]:
    """Convert USDC-per-ETH price bounds to aligned ticks for a fee tier.

    Returns (tick_lower, tick_upper) snapped to the fee tier's tick spacing.
    tick_lower is rounded down, tick_upper is rounded up.
    """
    if price_lower >= price_upper:
        raise ValueError(f"price_lower ({price_lower}) must be less than price_upper ({price_upper})")

    spacing = FEE_TICK_SPACING.get(fee)
    if spacing is None:
        raise ValueError(f"Unknown fee tier: {fee}")

    raw_lower = eth_price_to_tick(price_lower)
    raw_upper = eth_price_to_tick(price_upper)

    tick_lower = align_tick(raw_lower, spacing, round_down=True)
    tick_upper = align_tick(raw_upper, spacing, round_down=False)

    # Clamp to absolute bounds
    tick_lower = max(MIN_TICK, tick_lower)
    tick_upper = min(MAX_TICK, tick_upper)

    # Ensure at least one spacing apart
    if tick_upper <= tick_lower:
        tick_upper = tick_lower + spacing

    return tick_lower, tick_upper


def tick_to_sqrt_price_x96(tick: int) -> int:
    """Convert tick to sqrtPriceX96 for on-chain compatibility."""
    _check_tick_float_safe(tick)
    sqrt_price = math.sqrt(1.0001 ** tick)
    return int(sqrt_price * (2 ** 96))
