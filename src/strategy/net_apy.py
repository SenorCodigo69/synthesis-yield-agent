"""Net APY calculation — gross APY minus amortized gas costs.

A protocol showing 5% APY is worthless if gas costs eat 3% of your deposit.
This module computes the actual yield you'd earn after gas.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from src.models import GasPrice, ValidatedRate

logger = logging.getLogger(__name__)

# Base chain gas estimates (in gas units)
# Deposit = approve + supply, Withdraw = withdraw only
GAS_ESTIMATES: dict[str, int] = {
    "approve": 50_000,
    "supply": 150_000,
    "withdraw": 120_000,
}

# ETH price assumption for gas cost calc — updated at runtime via oracle
DEFAULT_ETH_PRICE_USD = Decimal("3500")


@dataclass
class NetAPY:
    """Net APY breakdown for a protocol."""
    protocol_name: str
    gross_apy: Decimal       # Raw APY from cross-validation
    gas_cost_usd: Decimal    # Estimated round-trip gas cost (deposit + withdraw)
    net_apy: Decimal         # APY after gas amortization
    hold_days: int           # Assumed hold period for amortization
    amount_usd: Decimal      # Capital amount used for calculation


def estimate_gas_cost_usd(
    gas_price: GasPrice,
    eth_price_usd: Decimal = DEFAULT_ETH_PRICE_USD,
) -> Decimal:
    """Estimate round-trip gas cost in USD (approve + supply + withdraw)."""
    total_gas_units = (
        GAS_ESTIMATES["approve"]
        + GAS_ESTIMATES["supply"]
        + GAS_ESTIMATES["withdraw"]
    )

    # Gas cost in ETH = gas_units * gas_price_gwei / 1e9
    gas_cost_eth = Decimal(str(total_gas_units)) * gas_price.total_gwei / Decimal("1000000000")
    gas_cost_usd = gas_cost_eth * eth_price_usd

    return gas_cost_usd


def calculate_net_apy(
    rate: ValidatedRate,
    gas_price: GasPrice,
    amount_usd: Decimal,
    hold_days: int = 90,
    eth_price_usd: Decimal = DEFAULT_ETH_PRICE_USD,
) -> NetAPY:
    """Calculate net APY after amortizing gas costs over the hold period.

    Formula:
      gas_cost_annualized = gas_cost_usd / amount_usd * (365 / hold_days) * 100
      net_apy = gross_apy - gas_cost_annualized

    Args:
        rate: Cross-validated rate data.
        gas_price: Current gas price.
        amount_usd: Capital amount to deposit.
        hold_days: Expected hold period (longer = lower gas impact).
        eth_price_usd: ETH/USD price for gas calculation.
    """
    gross_apy = rate.apy_median
    gas_cost_usd = estimate_gas_cost_usd(gas_price, eth_price_usd)

    if amount_usd <= 0 or hold_days <= 0:
        return NetAPY(
            protocol_name=rate.protocol.value,
            gross_apy=gross_apy,
            gas_cost_usd=gas_cost_usd,
            net_apy=Decimal("0"),
            hold_days=max(hold_days, 1),
            amount_usd=amount_usd,
        )

    # Annualize gas cost as a % of the deposit
    gas_pct_annualized = (
        gas_cost_usd / amount_usd
        * (Decimal("365") / Decimal(str(hold_days)))
        * Decimal("100")
    )

    net_apy = gross_apy - gas_pct_annualized

    logger.info(
        f"Net APY {rate.protocol.value}: {gross_apy:.2f}% gross "
        f"- {gas_pct_annualized:.4f}% gas = {net_apy:.2f}% net "
        f"(gas ${gas_cost_usd:.4f}, {hold_days}d hold, ${amount_usd:,.0f} deposit)"
    )

    return NetAPY(
        protocol_name=rate.protocol.value,
        gross_apy=gross_apy,
        gas_cost_usd=gas_cost_usd,
        net_apy=net_apy,
        hold_days=hold_days,
        amount_usd=amount_usd,
    )
