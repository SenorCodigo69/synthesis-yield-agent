"""Quant signals for LP tick range optimization.

Derives all data from on-chain pool reads (sqrtPriceX96 from slot0).
No external API dependencies — reads directly from the WETH-USDC pool
on Base via public RPC.

For historical candles: stores periodic price snapshots in SQLite,
builds candles from the snapshot history.

Signals computed: ATR, Bollinger Bands, RSI, ADX, EMA, market regime.
"""

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ── On-chain constants ────────────────────────────────────────

# WETH-USDC 0.05% pool on Base mainnet
POOL_ADDRESS = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
SLOT0_SELECTOR = "0x3850c7bd"

WETH_DECIMALS = 18
USDC_DECIMALS = 6

# Public Base RPCs (same rotation as dashboard)
BASE_RPCS = [
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://base.drpc.org",
    "https://mainnet.base.org",
]

# Default snapshot DB path
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "lp_snapshots.db"


# ── Data types ────────────────────────────────────────────────


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float


@dataclass
class LPSignals:
    """Bundled quant signals for the tick range optimizer."""

    current_price: float
    atr: float
    atr_pct: float  # ATR as fraction of price
    bb_upper: float
    bb_lower: float
    bb_width_pct: float  # BB width as fraction of price
    rsi: float
    adx: float
    regime: str  # "bull", "bear", "sideways"
    regime_confidence: float
    trend_direction: str  # "up", "down", "flat"
    timestamp: float


# ── On-chain price reader ─────────────────────────────────────


async def read_pool_price() -> float:
    """Read current ETH price (USDC per ETH) from the WETH-USDC pool on Base.

    Reads slot0().sqrtPriceX96 and converts to human-readable price.
    Tries multiple RPCs with rotation on failure.
    """
    import aiohttp

    for i, rpc_url in enumerate(BASE_RPCS):
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": POOL_ADDRESS, "data": SLOT0_SELECTOR}, "latest"],
                    "id": 1,
                }
                async with session.post(
                    rpc_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

            if "error" in data:
                raise RuntimeError(data["error"].get("message", str(data["error"])))

            result = data["result"]
            sqrt_price_x96 = int(result[:66], 16)  # First 32 bytes
            # price = (sqrtPriceX96 / 2^96)^2 * 10^(token0_decimals - token1_decimals)
            # For WETH(18)/USDC(6): price_usdc_per_eth = (sqrtP/2^96)^2 * 10^12
            sqrt_p = sqrt_price_x96 / (2**96)
            price = sqrt_p * sqrt_p * (10 ** (WETH_DECIMALS - USDC_DECIMALS))

            if price <= 0 or not math.isfinite(price):
                raise RuntimeError(f"Invalid price from pool: {price}")

            return price

        except Exception as e:
            logger.warning("RPC %s failed: %s", rpc_url, e)
            if i == len(BASE_RPCS) - 1:
                raise RuntimeError(f"All {len(BASE_RPCS)} RPCs failed reading pool price") from e
            continue

    raise RuntimeError("Unreachable")


# ── Snapshot DB ───────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON price_snapshots(timestamp);
"""


def _get_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def store_snapshot(price: float, ts: float | None = None, db_path: Path | None = None) -> None:
    """Store a price snapshot."""
    conn = _get_db(db_path)
    conn.execute("INSERT INTO price_snapshots (timestamp, price) VALUES (?, ?)", (ts or time.time(), price))
    conn.commit()
    conn.close()


def get_snapshots(hours: int = 168, db_path: Path | None = None) -> list[tuple[float, float]]:
    """Get price snapshots from the last N hours. Returns [(timestamp, price), ...]."""
    conn = _get_db(db_path)
    cutoff = time.time() - hours * 3600
    rows = conn.execute(
        "SELECT timestamp, price FROM price_snapshots WHERE timestamp > ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()
    conn.close()
    return rows


def snapshots_to_candles(snapshots: list[tuple[float, float]], interval_s: int = 3600) -> list[Candle]:
    """Aggregate price snapshots into OHLC candles.

    Args:
        snapshots: [(timestamp, price), ...] sorted by timestamp.
        interval_s: Candle interval in seconds (default 1 hour).
    """
    if not snapshots:
        return []

    candles = []
    bucket_start = (snapshots[0][0] // interval_s) * interval_s
    bucket_prices: list[float] = []

    for ts, price in snapshots:
        if ts >= bucket_start + interval_s:
            # Close current candle
            if bucket_prices:
                candles.append(Candle(
                    timestamp=bucket_start,
                    open=bucket_prices[0],
                    high=max(bucket_prices),
                    low=min(bucket_prices),
                    close=bucket_prices[-1],
                ))
            # Advance to the correct bucket
            bucket_start = (ts // interval_s) * interval_s
            bucket_prices = []

        bucket_prices.append(price)

    # Final candle
    if bucket_prices:
        candles.append(Candle(
            timestamp=bucket_start,
            open=bucket_prices[0],
            high=max(bucket_prices),
            low=min(bucket_prices),
            close=bucket_prices[-1],
        ))

    return candles


# ── Pure Python indicators ────────────────────────────────────


def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average."""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    result = [0.0] * len(values)
    if not values:
        return result
    k = 2.0 / (period + 1)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _sma(values: list[float], period: int) -> list[float]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("SMA period must be positive")
    result = [0.0] * len(values)
    for i in range(len(values)):
        if i < period - 1:
            result[i] = sum(values[: i + 1]) / (i + 1)
        else:
            result[i] = sum(values[i - period + 1 : i + 1]) / period
    return result


def compute_atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range — measures volatility."""
    if len(candles) < period + 1:
        return 0.0
    tr_vals = []
    for i in range(1, len(candles)):
        hl = candles[i].high - candles[i].low
        hc = abs(candles[i].high - candles[i - 1].close)
        lc = abs(candles[i].low - candles[i - 1].close)
        tr_vals.append(max(hl, hc, lc))
    ema_vals = _ema(tr_vals, period)
    return ema_vals[-1] if ema_vals else 0.0


def compute_bollinger(candles: list[Candle], period: int = 20, num_std: float = 2.0) -> tuple[float, float, float]:
    """Bollinger Bands. Returns (upper, middle, lower)."""
    closes = [c.close for c in candles]
    if len(closes) < period:
        p = closes[-1] if closes else 0
        return p, p, p
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return middle + num_std * std, middle, middle - num_std * std


def compute_rsi(candles: list[Candle], period: int = 14) -> float:
    """Relative Strength Index."""
    closes = [c.close for c in candles]
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = _ema(gains, period)[-1]
    avg_loss = _ema(losses, period)[-1]
    if avg_loss == 0 and avg_gain == 0:
        return 50.0  # No movement = neutral
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_adx(candles: list[Candle], period: int = 14) -> float:
    """Average Directional Index — trend strength."""
    if len(candles) < period + 2:
        return 0.0

    plus_dm_vals, minus_dm_vals, tr_vals = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm_vals.append(up if up > down and up > 0 else 0)
        minus_dm_vals.append(down if down > up and down > 0 else 0)

        hl = candles[i].high - candles[i].low
        hc = abs(candles[i].high - candles[i - 1].close)
        lc = abs(candles[i].low - candles[i - 1].close)
        tr_vals.append(max(hl, hc, lc))

    atr_vals = _ema(tr_vals, period)
    plus_di_vals = _ema(plus_dm_vals, period)
    minus_di_vals = _ema(minus_dm_vals, period)

    dx_vals = []
    for i in range(len(atr_vals)):
        if atr_vals[i] == 0:
            dx_vals.append(0)
            continue
        pdi = 100 * plus_di_vals[i] / atr_vals[i]
        mdi = 100 * minus_di_vals[i] / atr_vals[i]
        di_sum = pdi + mdi
        if di_sum == 0:
            dx_vals.append(0)
        else:
            dx_vals.append(100 * abs(pdi - mdi) / di_sum)

    adx_vals = _ema(dx_vals, period)
    return adx_vals[-1] if adx_vals else 0.0


# ── Regime detection ──────────────────────────────────────────

_ADX_TRENDING = 25
_ADX_RANGING = 18
_SMA_SHORT = 20
_SMA_LONG = 50
_EMA_FAST = 9
_EMA_SLOPE_PERIODS = 5


def detect_regime(candles: list[Candle]) -> tuple[str, float, str]:
    """Detect market regime from OHLCV candles.

    Ported from finance_agent/src/regime.py — simplified weighted-vote system.
    Requires at least 60 candles for meaningful signals.

    Returns: (regime, confidence, trend_direction)
    """
    if len(candles) < 60:
        return "sideways", 0.0, "flat"

    closes = [c.close for c in candles]
    close_val = closes[-2]  # second-to-last (latest complete candle)

    if close_val == 0:
        return "sideways", 0.0, "flat"

    # Compute indicators
    adx_val = compute_adx(candles)
    atr_val = compute_atr(candles)
    volatility_pct = atr_val / close_val

    sma20 = _sma(closes, _SMA_SHORT)[-2]
    sma50 = _sma(closes, _SMA_LONG)[-2]
    ema_fast = _ema(closes, _EMA_FAST)
    ema_current = ema_fast[-2]
    ema_prev = ema_fast[-2 - _EMA_SLOPE_PERIODS] if len(ema_fast) > _EMA_SLOPE_PERIODS + 2 else ema_current
    ema_slope_pct = (ema_current - ema_prev) / ema_prev if ema_prev != 0 else 0

    # Weighted votes
    bull_score = 0.0
    bear_score = 0.0
    trending_score = 0.0

    # ADX
    if adx_val > _ADX_TRENDING:
        trending_score += 1.0
    elif adx_val < _ADX_RANGING:
        trending_score -= 1.0

    # Price structure
    if close_val > sma20 and sma20 > sma50:
        bull_score += 2.0
        trending_score += 0.5
    elif close_val < sma20 and sma20 < sma50:
        bear_score += 2.0
        trending_score += 0.5
    elif close_val > sma20 and close_val > sma50:
        bull_score += 1.0
    elif close_val < sma20 and close_val < sma50:
        bear_score += 1.0

    # Volatility
    if volatility_pct > 0.005:
        trending_score += 0.5

    # EMA slope
    if ema_slope_pct > 0.001:
        bull_score += 1.5
        trending_score += 0.5
    elif ema_slope_pct < -0.001:
        bear_score += 1.5
        trending_score += 0.5

    # Classification
    max_directional = 5.5
    total_directional = bull_score + bear_score

    if trending_score < 0 or (adx_val < _ADX_RANGING and total_directional < 2.0):
        regime = "sideways"
        confidence = min(0.9, 0.4 + max(0.0, -trending_score) * 0.15 + max(0.0, (_ADX_RANGING - adx_val) / _ADX_RANGING) * 0.4)
    elif bull_score > bear_score:
        regime = "bull"
        net = bull_score - bear_score
        confidence = min(0.9, 0.35 + (net / max_directional) * 0.55)
    elif bear_score > bull_score:
        regime = "bear"
        net = bear_score - bull_score
        confidence = min(0.9, 0.35 + (net / max_directional) * 0.55)
    else:
        regime = "sideways"
        confidence = 0.35

    # Trend direction
    if ema_slope_pct > 0.001:
        trend_direction = "up"
    elif ema_slope_pct < -0.001:
        trend_direction = "down"
    else:
        trend_direction = "flat"

    return regime, round(confidence, 4), trend_direction


# ── Bundle all signals ────────────────────────────────────────


async def compute_signals(db_path: Path | None = None, candle_interval_s: int = 3600) -> LPSignals:
    """Read pool price on-chain, store snapshot, build candles from history, compute signals.

    This is fully on-chain — no external API. Requires accumulated snapshots
    in the DB for meaningful indicator values (60+ candles = 60+ hours of snapshots).

    If insufficient history, returns signals with current price but zero/default
    indicator values (regime=sideways, confidence=0).
    """
    # 1. Read current price from pool
    current_price = await read_pool_price()

    # 2. Store snapshot
    store_snapshot(current_price, db_path=db_path)

    # 3. Build candles from snapshot history
    snapshots = get_snapshots(hours=168, db_path=db_path)  # 7 days
    candles = snapshots_to_candles(snapshots, interval_s=candle_interval_s)

    # 4. Compute signals (graceful degradation if insufficient history)
    if len(candles) < 15:
        logger.info("Only %d candles available (need 60+ for full signals), using defaults", len(candles))
        return LPSignals(
            current_price=current_price,
            atr=0,
            atr_pct=0,
            bb_upper=current_price,
            bb_lower=current_price,
            bb_width_pct=0,
            rsi=50,
            adx=0,
            regime="sideways",
            regime_confidence=0,
            trend_direction="flat",
            timestamp=time.time(),
        )

    atr_val = compute_atr(candles)
    bb_upper, bb_mid, bb_lower = compute_bollinger(candles)
    rsi_val = compute_rsi(candles)
    adx_val = compute_adx(candles)
    regime, confidence, trend_dir = detect_regime(candles)

    return LPSignals(
        current_price=current_price,
        atr=atr_val,
        atr_pct=atr_val / current_price if current_price > 0 else 0,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_width_pct=(bb_upper - bb_lower) / current_price if current_price > 0 else 0,
        rsi=rsi_val,
        adx=adx_val,
        regime=regime,
        regime_confidence=confidence,
        trend_direction=trend_dir,
        timestamp=time.time(),
    )
