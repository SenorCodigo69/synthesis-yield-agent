"""Protocol health monitor — pre-execution health checks.

Combines circuit breaker state, rate validation, and protocol-specific
checks into a single health verdict per protocol.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from src.circuit_breakers import BreakerTrip, CircuitBreakers
from src.models import GasPrice, HealthStatus, SpendingScope, ValidatedRate

logger = logging.getLogger(__name__)


@dataclass
class ProtocolHealth:
    """Health assessment for a single protocol."""
    protocol: str
    status: HealthStatus
    checks_passed: int
    checks_total: int
    issues: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def is_safe_to_deposit(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    @property
    def should_withdraw(self) -> bool:
        return self.status == HealthStatus.CRITICAL


@dataclass
class SystemHealth:
    """Overall system health across all protocols."""
    protocols: dict[str, ProtocolHealth]
    breaker_trips: list[BreakerTrip]
    gas_ok: bool
    depeg_ok: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def is_operational(self) -> bool:
        """System is operational if no critical breakers and gas is ok."""
        return (
            self.depeg_ok
            and self.gas_ok
            and not any(t.severity == "critical" for t in self.breaker_trips)
        )

    @property
    def safe_protocols(self) -> list[str]:
        """Protocols safe for new deposits."""
        return [
            name for name, health in self.protocols.items()
            if health.is_safe_to_deposit
        ]

    @property
    def critical_protocols(self) -> list[str]:
        """Protocols that need emergency withdrawal."""
        return [
            name for name, health in self.protocols.items()
            if health.should_withdraw
        ]


class HealthMonitor:
    """Runs comprehensive health checks before execution.

    Combines:
    1. Circuit breaker state (depeg, gas, TVL crash)
    2. Rate validation (cross-source agreement)
    3. Spending scope compliance (TVL, utilization, APY sanity)
    """

    def __init__(self, breakers: CircuitBreakers, scope: SpendingScope):
        self.breakers = breakers
        self.scope = scope

    def check_system_health(
        self,
        rates: list[ValidatedRate],
        gas: GasPrice,
        usdc_price: Decimal = Decimal("1.0"),
    ) -> SystemHealth:
        """Run all health checks and return system health report."""
        # Run circuit breakers
        trips = self.breakers.check_all(rates, gas, usdc_price)

        # Check gas and depeg at system level
        gas_ok = gas.total_gwei <= self.breakers.gas_freeze_gwei
        depeg_ok = abs(usdc_price - Decimal("1.0")) <= self.breakers.depeg_threshold

        frozen_protos = self.breakers.get_frozen_protocols(trips)
        all_frozen = "__all__" in frozen_protos

        # Per-protocol health
        protocol_health: dict[str, ProtocolHealth] = {}
        for rate in rates:
            proto_name = rate.protocol.value
            health = self._check_protocol(rate, trips, frozen_protos, all_frozen)
            protocol_health[proto_name] = health

        system = SystemHealth(
            protocols=protocol_health,
            breaker_trips=trips,
            gas_ok=gas_ok,
            depeg_ok=depeg_ok,
        )

        # Log summary
        safe = len(system.safe_protocols)
        critical = len(system.critical_protocols)
        status = "OPERATIONAL" if system.is_operational else "DEGRADED"
        logger.info(
            f"System health: {status} | {safe} safe, {critical} critical | "
            f"gas={'OK' if gas_ok else 'FROZEN'} | "
            f"depeg={'OK' if depeg_ok else 'ALERT'}"
        )

        return system

    def _check_protocol(
        self,
        rate: ValidatedRate,
        trips: list[BreakerTrip],
        frozen_protos: set[str],
        all_frozen: bool,
    ) -> ProtocolHealth:
        """Run health checks for a single protocol."""
        proto_name = rate.protocol.value
        issues: list[str] = []
        checks_passed = 0
        checks_total = 0

        # Check 1: Rate validation
        checks_total += 1
        if rate.is_valid:
            checks_passed += 1
        else:
            issues.append("Rate cross-validation failed")

        # Check 2: TVL minimum
        checks_total += 1
        if rate.tvl_usd >= self.scope.min_protocol_tvl_usd:
            checks_passed += 1
        else:
            issues.append(
                f"TVL ${rate.tvl_usd:,.0f} below ${self.scope.min_protocol_tvl_usd:,.0f} minimum"
            )

        # Check 3: Utilization cap
        checks_total += 1
        if rate.utilization <= self.scope.max_utilization:
            checks_passed += 1
        else:
            issues.append(
                f"Utilization {rate.utilization:.1%} above {self.scope.max_utilization:.1%} cap"
            )

        # Check 4: APY sanity
        checks_total += 1
        if rate.apy_median / Decimal("100") <= self.scope.max_apy_sanity:
            checks_passed += 1
        else:
            issues.append(
                f"APY {rate.apy_median:.1f}% exceeds {self.scope.max_apy_sanity * 100:.0f}% sanity cap"
            )

        # Check 5: Not frozen by circuit breaker
        checks_total += 1
        if not all_frozen and proto_name not in frozen_protos:
            checks_passed += 1
        else:
            issues.append("Frozen by circuit breaker")

        # Check 6: No critical breaker trips for this protocol
        checks_total += 1
        proto_trips = [
            t for t in trips
            if t.protocol == proto_name and t.severity == "critical"
        ]
        if not proto_trips:
            checks_passed += 1
        else:
            for t in proto_trips:
                issues.append(f"Breaker: {t.message}")

        # Determine status
        if checks_passed == checks_total:
            status = HealthStatus.HEALTHY
        elif any("Breaker" in i or "Frozen" in i for i in issues):
            status = HealthStatus.CRITICAL
        elif checks_passed >= checks_total - 1:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.CRITICAL

        return ProtocolHealth(
            protocol=proto_name,
            status=status,
            checks_passed=checks_passed,
            checks_total=checks_total,
            issues=issues,
        )
