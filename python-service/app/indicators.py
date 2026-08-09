from statistics import fmean, pstdev

from .schemas import CandleSnapshot


def ema(values: list[float], period: int = 20) -> float:
    result = values[0]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def atr(candles: list[CandleSnapshot], period: int = 14) -> float:
    ranges = [
        max(candle.high - candle.low, abs(candle.high - candles[index - 1].close), abs(candle.low - candles[index - 1].close))
        for index, candle in enumerate(candles[-period:], len(candles) - period)
    ]
    return fmean(ranges)


def z_score(value: float, values: list[float]) -> float:
    deviation = pstdev(values)
    return 0.0 if deviation == 0 else (value - fmean(values)) / deviation


def rsi(values: list[float], period: int = 14) -> float:
    changes = [current - previous for previous, current in zip(values[-period - 1:-1], values[-period:])]
    average_gain = fmean(max(change, 0.0) for change in changes)
    average_loss = fmean(max(-change, 0.0) for change in changes)
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def stochastic_series(candles: list[CandleSnapshot], period: int = 14, smooth: int = 3) -> tuple[list[float], list[float]]:
    raw_k: list[float] = []
    for index in range(period - 1, len(candles)):
        window = candles[index - period + 1:index + 1]
        lowest = min(candle.low for candle in window)
        highest = max(candle.high for candle in window)
        raw_k.append(50.0 if highest == lowest else (candles[index].close - lowest) / (highest - lowest) * 100)
    smooth_k = [fmean(raw_k[max(0, index - smooth + 1):index + 1]) for index in range(len(raw_k))]
    signal_d = [fmean(smooth_k[max(0, index - smooth + 1):index + 1]) for index in range(len(smooth_k))]
    return smooth_k, signal_d
