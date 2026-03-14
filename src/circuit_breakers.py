"""Circuit breakers — emergency safety system for the yield agent.

Monitors for dangerous conditions and triggers protective actions:
- USDC depeg: deviation > 0.5% from $1.00 → emergency withdraw
- TVL crash: > 30% drop in 1h → withdraw from that protocol
- Gas spike: > 200 gwei → freeze all moves
- Rate divergence: > 2% between sources → pause actions
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from src.models import GasPrice, SpendingScope, ValidatedRate

logger = logging.getLogger(__name__)


class BreakerType(str, Enum):
    DEPEG = "depeg"
    TVL_CRASH = "tvl_crash"
    GAS_FREEZE = "gas_freeze"
    RATE_DIVERGENCE = "rate_divergence"


class BreakerAction(str, Enum):
    EMERGENCY_WITHDRAW_ALL = "emergency_withdraw_all"
    EMERGENCY_WITHDRAW_PROTOCOL = "emergency_withdraw_protocol"
    FREEZE_ALL = "freeze_all"
    PAUSE_PROTOCOL = "pause_protocol"


@dataclass
class BreakerTrip:
    """A circuit breaker that has tripped."""
    breaker: BreakerType
    action: BreakerAction
    severity: str  # "critical", "warning"
    message: str
    protocol: str | None = None  # Which protocol is affected (None = all)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class CircuitBreakers:
    """Monitors for dangerous conditions and emits trip signals.

    Does NOT execute actions directly — returns trips that the executor
    or agent loop must handle. This keeps the safety logic pure and testable.
    """

    def __init__(self, config: dict | None = None):
        cb = (config or {}).get("circuit_breakers", {})
        self.depeg_threshold = Decimal(str(cb.get("depeg_threshold", 0.005)))
        self.tvl_drop_1h_pct = Decimal(str(cb.get("tvl_drop_1h_pct", 0.30)))
        self.tvl_drop_24h_pct = Decimal(str(cb.get("tvl_drop_24h_pct", 0.10)))
        self.gas_freeze_gwei = cb.get("gas_freeze_gwei", 200)
        self.rate_divergence_block = Decimal(str(cb.get("rate_divergence_block", 0.02)))

        # TVL history for crash detection: protocol -> list of (timestamp, tvl)
        self._tvl_history: dict[str, list[tuple[datetime, Decimal]]] = {}

    def check_all(
        self,
        rates: list[ValidatedRate],
        gas: GasPrice,
        usdc_price: Decimal = Decimal("1.0"),
    ) -> list[BreakerTrip]:
        """Run all circuit breaker checks. Returns list of trips (empty = all clear)."""
        trips: list[BreakerTrip] = []

        trips.extend(self.check_depeg(usdc_price))
        trips.extend(self.check_gas_freeze(gas))
        trips.extend(self.check_rate_divergence(rates))
        trips.extend(self.check_tvl_crash(rates))

        if trips:
            critical = sum(1 for t in trips if t.severity == "critical")
            logger.warning(
                f"CIRCUIT BREAKERS: {len(trips)} trips ({critical} critical)"
            )
            for trip in trips:
                logger.warning(f"  [{trip.severity.upper()}] {trip.message}")
        else:
            logger.info("Circuit breakers: all clear")

        return trips

    def check_depeg(self, usdc_price: Decimal) -> list[BreakerTrip]:
        """Check if USDC has depegged beyond threshold."""
        deviation = abs(usdc_price - Decimal("1.0"))
        if deviation > self.depeg_threshold:
            return [BreakerTrip(
                breaker=BreakerType.DEPEG,
                action=BreakerAction.EMERGENCY_WITHDRAW_ALL,
                severity="critical",
                message=(
                    f"USDC DEPEG: price ${usdc_price:.4f}, "
                    f"deviation {deviation:.4f} > {self.depeg_threshold:.4f} threshold — "
                    f"EMERGENCY WITHDRAW ALL"
                ),
            )]
        return []

    def check_gas_freeze(self, gas: GasPrice) -> list[BreakerTrip]:
        """Check if gas is too high for any moves."""
        if gas.total_gwei > self.gas_freeze_gwei:
            return [BreakerTrip(
                breaker=BreakerType.GAS_FREEZE,
                action=BreakerAction.FREEZE_ALL,
                severity="critical",
                message=(
                    f"GAS FREEZE: {gas.total_gwei:.1f} gwei > "
                    f"{self.gas_freeze_gwei} ceiling — all moves frozen"
                ),
            )]
        return []

    def check_rate_divergence(self, rates: list[ValidatedRate]) -> list[BreakerTrip]:
        """Check for dangerous rate divergence between sources."""
        trips = []
        for rate in rates:
            if len(rate.apy_sources) < 2:
                continue
            values = list(rate.apy_sources.values())
            divergence = abs(max(values) - min(values))
            # Divergence threshold is in decimal (0.02 = 2%), but APY values are in % (e.g., 3.5%)
            if divergence > self.rate_divergence_block * 100:
                trips.append(BreakerTrip(
                    breaker=BreakerType.RATE_DIVERGENCE,
                    action=BreakerAction.PAUSE_PROTOCOL,
                    severity="warning",
                    message=(
                        f"{rate.protocol.value}: rate divergence {divergence:.2f}% > "
                        f"{self.rate_divergence_block * 100:.1f}% — pausing protocol"
                    ),
                    protocol=rate.protocol.value,
                ))
        return trips

    def check_tvl_crash(self, rates: list[ValidatedRate]) -> list[BreakerTrip]:
        """Check for sudden TVL drops (capital flight).

        Records current TVL and compares against recent history.
        """
        trips = []
        now = datetime.now(tz=timezone.utc)

        for rate in rates:
            proto = rate.protocol.value

            # Record current TVL
            if proto not in self._tvl_history:
                self._tvl_history[proto] = []
            self._tvl_history[proto].append((now, rate.tvl_usd))

            # Prune entries older than 25 hours
            cutoff_25h = now.timestamp() - 25 * 3600
            self._tvl_history[proto] = [
                (ts, tvl) for ts, tvl in self._tvl_history[proto]
                if ts.timestamp() > cutoff_25h
            ]

            history = self._tvl_history[proto]
            if len(history) < 2:
                continue

            # Check 1h window
            one_hour_ago = now.timestamp() - 3600
            recent_entries = [
                (ts, tvl) for ts, tvl in history
                if ts.timestamp() <= one_hour_ago
            ]
            if recent_entries:
                oldest_in_1h = recent_entries[0][1]
                if oldest_in_1h > 0:
                    drop_1h = (oldest_in_1h - rate.tvl_usd) / oldest_in_1h
                    if drop_1h > self.tvl_drop_1h_pct:
                        trips.append(BreakerTrip(
                            breaker=BreakerType.TVL_CRASH,
                            action=BreakerAction.EMERGENCY_WITHDRAW_PROTOCOL,
                            severity="critical",
                            message=(
                                f"{proto}: TVL crashed {drop_1h:.1%} in 1h "
                                f"(${oldest_in_1h:,.0f} -> ${rate.tvl_usd:,.0f}) — "
                                f"EMERGENCY WITHDRAW"
                            ),
                            protocol=proto,
                        ))

        return trips

    def record_tvl(self, protocol: str, tvl_usd: Decimal) -> None:
        """Manually record a TVL data point (for testing or external feeds)."""
        now = datetime.now(tz=timezone.utc)
        if protocol not in self._tvl_history:
            self._tvl_history[protocol] = []
        self._tvl_history[protocol].append((now, tvl_usd))

    def has_critical_trips(self, trips: list[BreakerTrip]) -> bool:
        """Check if any trips require emergency action."""
        return any(t.severity == "critical" for t in trips)

    def requires_emergency_withdraw(self, trips: list[BreakerTrip]) -> bool:
        """Check if any trips require emergency withdrawal."""
        return any(
            t.action in (BreakerAction.EMERGENCY_WITHDRAW_ALL, BreakerAction.EMERGENCY_WITHDRAW_PROTOCOL)
            for t in trips
        )

    def get_frozen_protocols(self, trips: list[BreakerTrip]) -> set[str]:
        """Get set of protocols that should be frozen (no new deposits)."""
        frozen = set()
        for trip in trips:
            if trip.action == BreakerAction.FREEZE_ALL:
                return {"__all__"}
            if trip.action in (
                BreakerAction.PAUSE_PROTOCOL,
                BreakerAction.EMERGENCY_WITHDRAW_PROTOCOL,
            ):
                if trip.protocol:
                    frozen.add(trip.protocol)
        return frozen
