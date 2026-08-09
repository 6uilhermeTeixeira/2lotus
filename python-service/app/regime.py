from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean

from .schemas import CandleSnapshot


TREND_PROFILES = {
    "fimath_ema20_flow",
    "momentum_atr_breakout",
    "macd_trend_crossover",
    "stochastic_trend_pullback",
    "stochastic_sell_session",
}
RANGE_PROFILES = {"rsi_bollinger_reversion"}


@dataclass(frozen=True)
class RegimeSnapshot:
    name: str
    direction: str
    reason: str
    adx: float | None = None
    atr_percentile: float | None = None
    ema50: float | None = None
    ema200: float | None = None


def _ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def _hourly_candles(candles: list[CandleSnapshot]) -> list[CandleSnapshot]:
    buckets: dict[object, list[CandleSnapshot]] = {}
    for candle in candles:
        hour = candle.timestamp_utc.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(candle)
    output: list[CandleSnapshot] = []
    for hour, group in sorted(buckets.items()):
        group.sort(key=lambda candle: candle.timestamp_utc)
        expected = [hour + timedelta(minutes=5 * index) for index in range(12)]
        if len(group) != 12 or [candle.timestamp_utc for candle in group] != expected:
            continue
        output.append(
            CandleSnapshot(
                timestamp_utc=hour,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum(candle.volume for candle in group),
            )
        )
    return output


def _atr_series(candles: list[CandleSnapshot], period: int = 14) -> list[float]:
    if len(candles) <= period:
        return []
    ranges = [
        max(
            candles[index].high - candles[index].low,
            abs(candles[index].high - candles[index - 1].close),
            abs(candles[index].low - candles[index - 1].close),
        )
        for index in range(1, len(candles))
    ]
    atr = fmean(ranges[:period])
    output = [atr]
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
        output.append(atr)
    return output


def _adx(candles: list[CandleSnapshot], period: int = 14) -> float | None:
    if len(candles) < period * 2 + 1:
        return None
    true_ranges: list[float] = []
    positive_dm: list[float] = []
    negative_dm: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
        up, down = current.high - previous.high, previous.low - current.low
        positive_dm.append(up if up > down and up > 0 else 0.0)
        negative_dm.append(down if down > up and down > 0 else 0.0)
    tr = sum(true_ranges[:period])
    plus = sum(positive_dm[:period])
    minus = sum(negative_dm[:period])
    dx_values: list[float] = []
    for index in range(period, len(true_ranges)):
        tr = tr - tr / period + true_ranges[index]
        plus = plus - plus / period + positive_dm[index]
        minus = minus - minus / period + negative_dm[index]
        plus_di = 100 * plus / tr if tr else 0.0
        minus_di = 100 * minus / tr if tr else 0.0
        denominator = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    if len(dx_values) < period:
        return None
    adx = fmean(dx_values[:period])
    for value in dx_values[period:]:
        adx = (adx * (period - 1) + value) / period
    return adx


def _classify_hourly(hourly: list[CandleSnapshot], settings: object) -> RegimeSnapshot:
    if len(hourly) < 205:
        return RegimeSnapshot("insufficient_history", "none", "requires_200_complete_h1_candles")
    closes = [candle.close for candle in hourly]
    ema50 = _ema(closes[-50:], 50)
    previous_ema50 = _ema(closes[-51:-1], 50)
    ema200 = _ema(closes[-200:], 200)
    adx = _adx(hourly[-250:])
    atr_values = _atr_series(hourly[-320:])
    if adx is None or not atr_values:
        return RegimeSnapshot("insufficient_history", "none", "indicators_not_ready")
    current_atr = atr_values[-1]
    baseline = atr_values[-100:]
    below = sum(value < current_atr for value in baseline)
    equal = sum(value == current_atr for value in baseline)
    atr_percentile = (below + equal * 0.5) / len(baseline) * 100
    direction = "buy" if ema50 > ema200 and ema50 > previous_ema50 else "sell" if ema50 < ema200 and ema50 < previous_ema50 else "none"
    if atr_percentile >= settings.backtest_regime_high_volatility_percentile:
        name, reason = "high_volatility", "atr_percentile_above_limit"
    elif adx >= settings.backtest_regime_trend_adx and direction != "none":
        name, reason = "trend", "adx_and_h1_ema_alignment"
    elif adx <= settings.backtest_regime_range_adx:
        name, direction, reason = "range", "both", "low_adx"
    else:
        name, direction, reason = "transition", "none", "regime_not_confirmed"
    return RegimeSnapshot(name, direction, reason, adx, atr_percentile, ema50, ema200)


def classify_regime(candles: list[CandleSnapshot], settings: object) -> RegimeSnapshot:
    return _classify_hourly(_hourly_candles(candles), settings)


def build_regime_timeline(candles: list[CandleSnapshot], settings: object) -> tuple[list[datetime], list[RegimeSnapshot]]:
    hourly = _hourly_candles(candles)
    timestamps: list[datetime] = []
    snapshots: list[RegimeSnapshot] = []
    for index in range(204, len(hourly)):
        timestamps.append(hourly[index].timestamp_utc + timedelta(hours=1))
        snapshots.append(_classify_hourly(hourly[max(0, index - 320):index + 1], settings))
    return timestamps, snapshots


def regime_at(timeline: tuple[list[datetime], list[RegimeSnapshot]], timestamp: datetime) -> RegimeSnapshot:
    timestamps, snapshots = timeline
    index = bisect_right(timestamps, timestamp) - 1
    return snapshots[index] if index >= 0 else RegimeSnapshot("insufficient_history", "none", "requires_200_complete_h1_candles")


def regime_allows(strategy_profile: str, side: str, regime: RegimeSnapshot) -> tuple[bool, str]:
    if strategy_profile in TREND_PROFILES:
        allowed = regime.name == "trend" and regime.direction == side
        return allowed, "trend_direction_confirmed" if allowed else f"trend_required:{regime.name}:{regime.direction}"
    if strategy_profile in RANGE_PROFILES:
        allowed = regime.name == "range"
        return allowed, "range_confirmed" if allowed else f"range_required:{regime.name}"
    return False, "strategy_has_no_regime_policy"
