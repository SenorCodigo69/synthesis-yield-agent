"""Cross-validation aggregator — multi-source rate validation.

Core principle: never trust a single source for yield decisions.
Fetches from DeFi Llama + on-chain, cross-validates, uses median.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

import aiohttp

from src.models import (
    Chain,
    DataSource,
    ProtocolName,
    ValidatedRate,
    YieldPool,
)
from src.data import defillama, onchain

logger = logging.getLogger(__name__)


async def fetch_validated_rates(
    http_session: aiohttp.ClientSession,
    rpc_url: str,
    chain: Chain = Chain.BASE,
    protocols: list[ProtocolName] | None = None,
    rate_divergence_warn: Decimal = Decimal("0.005"),
    rate_divergence_block: Decimal = Decimal("0.02"),
) -> list[ValidatedRate]:
    """Fetch rates from multiple sources, cross-validate, return validated rates.

    Cross-validation rules:
    - Divergence > rate_divergence_warn (0.5%): log warning, use median
    - Divergence > rate_divergence_block (2%): mark invalid, block actions
    """
    if protocols is None:
        protocols = list(ProtocolName)

    # Fetch from both sources
    dl_pools = await defillama.fetch_usdc_pools(http_session, chain, protocols)
    oc_pools = await onchain.fetch_all_onchain_rates(rpc_url, chain)

    # Group by protocol
    dl_by_proto: dict[ProtocolName, list[YieldPool]] = {}
    for pool in dl_pools:
        dl_by_proto.setdefault(pool.protocol, []).append(pool)

    oc_by_proto: dict[ProtocolName, YieldPool] = {}
    for pool in oc_pools:
        oc_by_proto[pool.protocol] = pool

    results = []
    for proto in protocols:
        dl_pools_for_proto = dl_by_proto.get(proto, [])
        oc_pool = oc_by_proto.get(proto)

        validated = _validate_protocol_rates(
            proto, chain, dl_pools_for_proto, oc_pool,
            rate_divergence_warn, rate_divergence_block,
        )
        if validated:
            results.append(validated)

    logger.info(
        f"Aggregator: {len(results)} validated rates "
        f"({sum(1 for r in results if r.is_valid)} valid, "
        f"{sum(1 for r in results if not r.is_valid)} blocked)"
    )
    return results


def _validate_protocol_rates(
    proto: ProtocolName,
    chain: Chain,
    dl_pools: list[YieldPool],
    oc_pool: YieldPool | None,
    warn_threshold: Decimal,
    block_threshold: Decimal,
) -> ValidatedRate | None:
    """Cross-validate rates for a single protocol."""
    sources: dict[DataSource, Decimal] = {}
    warnings: list[str] = []
    tvl_usd = Decimal("0")
    utilization = Decimal("0")

    # DeFi Llama — prefer exact "USDC" symbol with non-zero APY (raw lending pool),
    # then fall back to highest TVL pool with non-zero APY (MetaMorpho vaults etc.).
    # Wrapped vaults like SYRUPUSDC may report 0% APY — skip those.
    if dl_pools:
        exact_usdc = [
            p for p in dl_pools
            if p.symbol.upper() == "USDC" and p.apy_total > 0
        ]
        if exact_usdc:
            best_dl = max(exact_usdc, key=lambda p: p.tvl_usd)
        else:
            nonzero = [p for p in dl_pools if p.apy_total > 0]
            best_dl = max(nonzero or dl_pools, key=lambda p: p.tvl_usd)
        sources[DataSource.DEFILLAMA] = best_dl.apy_total
        tvl_usd = best_dl.tvl_usd
        utilization = best_dl.utilization

    # On-chain
    if oc_pool:
        sources[DataSource.ONCHAIN] = oc_pool.apy_total
        if oc_pool.utilization > 0:
            utilization = oc_pool.utilization

    if not sources:
        logger.warning(f"No rate data for {proto.value} on {chain.value}")
        return None

    # Calculate median and divergence
    rates = list(sources.values())
    apy_median = Decimal(str(median(float(r) for r in rates)))

    divergence = Decimal("0")
    is_valid = True

    if len(rates) >= 2:
        divergence = abs(max(rates) - min(rates))
        if divergence > block_threshold * 100:
            # Divergence thresholds are in decimal (0.02 = 2%), rates are in % (2.5%)
            is_valid = False
            warnings.append(
                f"BLOCKED: rate divergence {divergence:.2f}% exceeds "
                f"{block_threshold * 100:.1f}% threshold"
            )
            logger.error(f"{proto.value}: {warnings[-1]}")
        elif divergence > warn_threshold * 100:
            warnings.append(
                f"WARNING: rate divergence {divergence:.2f}% exceeds "
                f"{warn_threshold * 100:.1f}% threshold — using median"
            )
            logger.warning(f"{proto.value}: {warnings[-1]}")
    else:
        warnings.append("Single source only — no cross-validation possible")

    return ValidatedRate(
        protocol=proto,
        chain=chain,
        apy_median=apy_median,
        apy_sources=sources,
        tvl_usd=tvl_usd,
        utilization=utilization,
        divergence=divergence,
        is_valid=is_valid,
        warnings=warnings,
        timestamp=datetime.now(tz=timezone.utc),
    )
