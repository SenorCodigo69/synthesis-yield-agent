"""Quant signals for LP tick range optimization.

Lightweight port of the trading agent's indicators and regime detection,
using pure Python (no pandas/numpy dependency). Fetches OHLCV data from
CoinGecko's free API.

Signals computed: ATR, Bollinger Bands, RSI, ADX, EMA, market regime.
"""

import logging
import math
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

COINGECKO_OHLC_URL = "https://api.coingecko.com/api/v3/coins/ethereum/ohlc"


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


# ── OHLCV fetch ───────────────────────────────────────────────


_MAX_RESPONSE_BYTES = 1_000_000  # 1 MB limit on API response
_MAX_RETRIES = 2
_RETRY_DELAY_S = 3


async def fetch_eth_ohlcv(days: int = 30) -> list[Candle]:
    """Fetch ETH/USD OHLCV from CoinGecko (free, no key).

    Returns 4h candles for 30 days, 1h for 7 days.
    Retries up to 2 times on failure with exponential backoff.
    """
    import asyncio
    import json

    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                params = {"vs_currency": "usd", "days": str(days)}
                async with session.get(COINGECKO_OHLC_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        raise RuntimeError("CoinGecko rate limited (429)")
                    if resp.status != 200:
                        raise RuntimeError(f"CoinGecko OHLC failed: {resp.status}")
                    raw = await resp.content.read(_MAX_RESPONSE_BYTES)
                    data = json.loads(raw)
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                logger.warning("CoinGecko attempt %d failed: %s, retrying in %ds", attempt + 1, e, _RETRY_DELAY_S * (attempt + 1))
                await asyncio.sleep(_RETRY_DELAY_S * (attempt + 1))
                continue
            raise RuntimeError(f"CoinGecko failed after {_MAX_RETRIES + 1} attempts: {last_err}") from last_err

        # Validate response shape
        if not isinstance(data, list):
            raise RuntimeError(f"CoinGecko returned unexpected format: {type(data).__name__}")

        candles = []
        for i, row in enumerate(data):
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                logger.warning("Skipping malformed OHLC row %d: %s", i, row)
                continue
            try:
                candles.append(Candle(
                    timestamp=float(row[0]) / 1000,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                ))
            except (TypeError, ValueError) as e:
                logger.warning("Skipping invalid OHLC row %d: %s", i, e)
                continue
        return candles

    raise RuntimeError("Unreachable")


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
    Omits HMM/Hurst (too heavy for yield agent).

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


async def compute_signals(days: int = 30) -> LPSignals:
    """Fetch OHLCV and compute all LP signals."""
    candles = await fetch_eth_ohlcv(days)
    if not candles:
        raise RuntimeError("No OHLCV data returned")

    current_price = candles[-1].close
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
