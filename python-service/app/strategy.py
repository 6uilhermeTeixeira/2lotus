from collections import defaultdict, deque
from copy import copy
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
from statistics import fmean, pstdev
from threading import Lock

from .adaptive import ADAPTIVE_MIN_CLOSED_TRADES, ADAPTIVE_MIN_PROFIT_FACTOR, ADAPTIVE_WINDOW_DAYS, select_adaptive_component
from .config import Settings
from .indicators import atr as _atr
from .indicators import ema as _ema
from .indicators import ema_series as _ema_series
from .indicators import rsi as _rsi
from .indicators import stochastic_series as _stochastic_series
from .indicators import z_score as _z
from .regime import RegimeSnapshot, classify_regime
from .schemas import CandleSnapshot, DecisionAction, DecisionRequest, DecisionResponse, FlowSnapshot, RiskCheck


@dataclass
class Setup:
    action: DecisionAction
    setup_type: str
    entry: float
    stop: float
    partial: float
    target: float
    stop_points: float
    flow_mode: str
    ema20: float
    atr14: float
    ema50: float | None = None
    breakout_level: float | None = None
    rsi14: float | None = None
    bollinger_upper: float | None = None
    bollinger_lower: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    stochastic_k: float | None = None
    stochastic_d: float | None = None
    partial_close_pct: float | None = None
    move_stop_to_breakeven: bool = True
    max_holding_bars: int | None = None
    h1_ema20: float | None = None
    h1_ema50: float | None = None
    m15_ema9: float | None = None
    m15_ema21: float | None = None
    trend_strength: float | None = None
    risk_multiplier: float = 1.0


@dataclass
class FlowSample:
    timestamp_utc: datetime
    book_imbalance: float
    tick_imbalance: float


@dataclass
class FlowAnalysis:
    ready: bool
    confirmed: bool
    contradicted: bool
    status: str
    book_z: float | None = None
    tick_z: float | None = None
    score: float | None = None


_flow_windows: dict[str, deque[FlowSample]] = defaultdict(deque)
_flow_lock = Lock()


def _flow_metrics(flow: FlowSnapshot) -> tuple[float, float] | None:
    bids = sum(level.volume for level in flow.book if level.side == "bid")
    asks = sum(level.volume for level in flow.book if level.side == "ask")
    total_book = bids + asks
    total_ticks = flow.up_volume + flow.down_volume
    if total_book <= 0 or total_ticks <= 0:
        return None
    return ((bids - asks) / total_book, (flow.up_volume - flow.down_volume) / total_ticks)


def _record_flow(request: DecisionRequest, window_seconds: int) -> deque[FlowSample] | None:
    if request.flow is None:
        return None
    metrics = _flow_metrics(request.flow)
    if metrics is None:
        return None
    key = request.bot_id or request.symbol
    with _flow_lock:
        window = _flow_windows[key]
        window.append(FlowSample(request.timestamp_utc, metrics[0], metrics[1]))
        cutoff = request.timestamp_utc - timedelta(seconds=window_seconds)
        while window and window[0].timestamp_utc < cutoff:
            window.popleft()
        return deque(window)


def _flow_analysis(
    window: deque[FlowSample] | None,
    action: DecisionAction,
    window_seconds: int,
    min_samples: int,
    z_threshold: float,
) -> FlowAnalysis:
    if window is None:
        return FlowAnalysis(False, False, False, "flow_unavailable")
    history = list(window)
    if len(history) <= min_samples:
        return FlowAnalysis(False, False, False, "flow_warmup")
    elapsed = (history[-1].timestamp_utc - history[0].timestamp_utc).total_seconds()
    if elapsed < window_seconds * 0.8:
        return FlowAnalysis(False, False, False, "flow_warmup")
    current, baseline = history[-1], history[:-1]
    book_z = _z(current.book_imbalance, [item.book_imbalance for item in baseline])
    tick_z = _z(current.tick_imbalance, [item.tick_imbalance for item in baseline])
    direction = 1 if action == DecisionAction.BUY else -1
    directed_book, directed_tick = direction * book_z, direction * tick_z
    score = (directed_book + directed_tick) / 2
    confirmed = directed_book >= z_threshold and abs(directed_tick) >= z_threshold
    contradicted = directed_book <= -z_threshold and directed_tick <= -z_threshold
    if contradicted:
        status = "flow_contradiction"
    elif confirmed and directed_tick >= z_threshold:
        status = "aggression"
    elif confirmed:
        status = "absorption"
    else:
        status = "flow_neutral"
    return FlowAnalysis(True, confirmed, contradicted, status, book_z, tick_z, score)


def _position_size(balance: float, risk_pct: float, stop_points: float, value_per_point: float) -> float:
    return (balance * risk_pct / 100) / (stop_points * value_per_point)


def _aggregate_completed_candles(
    candles: list[CandleSnapshot],
    timeframe_minutes: int,
    available_at: datetime,
) -> list[CandleSnapshot]:
    """Aggregate lower-timeframe candles without exposing an incomplete bucket."""
    buckets: dict[datetime, list[CandleSnapshot]] = defaultdict(list)
    for candle in sorted(candles, key=lambda item: item.timestamp_utc):
        timestamp = candle.timestamp_utc.astimezone(timezone.utc)
        bucket_minute = timestamp.minute - timestamp.minute % timeframe_minutes
        bucket_start = timestamp.replace(minute=bucket_minute, second=0, microsecond=0)
        if bucket_start + timedelta(minutes=timeframe_minutes) <= available_at.astimezone(timezone.utc):
            buckets[bucket_start].append(candle)
    return [
        CandleSnapshot(
            timestamp_utc=bucket_start,
            open=rows[0].open,
            high=max(row.high for row in rows),
            low=min(row.low for row in rows),
            close=rows[-1].close,
            volume=sum(row.volume for row in rows),
        )
        for bucket_start, rows in sorted(buckets.items())
    ]


def _latest_confirmed_zigzag_levels(candles: list[CandleSnapshot], span: int = 2) -> tuple[float | None, float | None]:
    """Return causal fractal pivots used as a non-repainting ZigZag proxy."""
    pivot_high: float | None = None
    pivot_low: float | None = None
    for index in range(span, len(candles) - span):
        window = candles[index - span:index + span + 1]
        current = candles[index]
        if current.high == max(candle.high for candle in window):
            pivot_high = current.high
        if current.low == min(candle.low for candle in window):
            pivot_low = current.low
    return pivot_high, pivot_low


def _happy_gold_zigzag_setup(request: DecisionRequest) -> Setup | None:
    if len(request.candles) < 43:
        return None
    m30 = _aggregate_completed_candles(request.candles, 30, request.timestamp_utc)
    if len(m30) < 7:
        return None
    pivot_high, pivot_low = _latest_confirmed_zigzag_levels(m30)
    previous_close = request.candles[-2].close
    buy_breakout = pivot_high is not None and previous_close <= pivot_high < request.ask
    sell_breakout = pivot_low is not None and previous_close >= pivot_low > request.bid
    if not buy_breakout and not sell_breakout:
        return None
    if buy_breakout and sell_breakout:
        buy_distance = request.ask - float(pivot_high)
        sell_distance = float(pivot_low) - request.bid
        buy_breakout = buy_distance >= sell_distance
        sell_breakout = not buy_breakout
    action = DecisionAction.BUY if buy_breakout else DecisionAction.SELL
    entry = request.ask if buy_breakout else request.bid
    stop_points = 240.0
    target_points = 1000.0
    stop = entry - stop_points * request.point_size if buy_breakout else entry + stop_points * request.point_size
    target = entry + target_points * request.point_size if buy_breakout else entry - target_points * request.point_size
    m30_atr = _atr(m30, min(14, len(m30)))
    return Setup(
        action=action,
        setup_type="happy_gold_zigzag_breakout",
        entry=entry,
        stop=stop,
        partial=target,
        target=target,
        stop_points=stop_points,
        flow_mode="not_applicable",
        ema20=m30[-1].close,
        atr14=m30_atr,
        breakout_level=float(pivot_high if buy_breakout else pivot_low),
        partial_close_pct=100.0,
        move_stop_to_breakeven=False,
        max_holding_bars=2,
    )


def _happy_gold_retest_setup(request: DecisionRequest, settings: Settings) -> Setup | None:
    """Confirm an M30 pivot breakout with a causal M5 retest and rejection."""
    if len(request.candles) < 43:
        return None
    candles = request.candles[-60:]
    m30 = _aggregate_completed_candles(candles, 30, request.timestamp_utc)
    if len(m30) < 7:
        return None
    pivot_high, pivot_low = _latest_confirmed_zigzag_levels(m30)
    atr_m5 = _atr(candles[:-1])
    if atr_m5 <= 0:
        return None
    latest = candles[-1]
    tolerance = atr_m5 * float(getattr(settings, "happy_gold_v2_retest_tolerance_atr", .15))
    recent_breakouts = range(max(1, len(candles) - 5), len(candles) - 1)
    buy_broken = pivot_high is not None and any(candles[index - 1].close <= pivot_high < candles[index].close for index in recent_breakouts)
    sell_broken = pivot_low is not None and any(candles[index - 1].close >= pivot_low > candles[index].close for index in recent_breakouts)
    buy_retest = bool(buy_broken and latest.low <= float(pivot_high) + tolerance and latest.close > float(pivot_high) and latest.close > latest.open)
    sell_retest = bool(sell_broken and latest.high >= float(pivot_low) - tolerance and latest.close < float(pivot_low) and latest.close < latest.open)
    if not buy_retest and not sell_retest:
        return None
    action = DecisionAction.BUY if buy_retest else DecisionAction.SELL
    entry = request.ask if buy_retest else request.bid
    breakout_level = float(pivot_high if buy_retest else pivot_low)
    extension = abs(entry - breakout_level)
    if extension > atr_m5 * float(getattr(settings, "happy_gold_v2_max_extension_atr", .35)):
        return None
    structural_stop = latest.low - .2 * atr_m5 if buy_retest else latest.high + .2 * atr_m5
    minimum_stop = atr_m5 * float(getattr(settings, "happy_gold_v2_stop_atr", 1.5))
    stop_distance = max(minimum_stop, entry - structural_stop if buy_retest else structural_stop - entry)
    stop = entry - stop_distance if buy_retest else entry + stop_distance
    target_distance = stop_distance * float(getattr(settings, "happy_gold_v2_target_r", 2.0))
    target = entry + target_distance if buy_retest else entry - target_distance
    return Setup(
        action=action,
        setup_type="happy_gold_zigzag_retest_atr",
        entry=entry,
        stop=stop,
        partial=target,
        target=target,
        stop_points=stop_distance / request.point_size,
        flow_mode="not_applicable",
        ema20=m30[-1].close,
        atr14=atr_m5,
        breakout_level=breakout_level,
        partial_close_pct=100.0,
        move_stop_to_breakeven=False,
        max_holding_bars=int(getattr(settings, "happy_gold_v2_max_holding_bars", 12)),
    )


def _setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 23:
        return None
    closes = [c.close for c in candles]
    ema20 = _ema(closes[-21:-1])
    previous_ema = _ema(closes[-22:-2])
    atr14 = _atr(candles[:-1])
    current, previous = candles[-1], candles[-2]
    tolerance, extension = atr14 * .15, atr14 * .5
    bullish = current.close > ema20 and ema20 > previous_ema
    bearish = current.close < ema20 and ema20 < previous_ema
    if not bullish and not bearish:
        return None
    action = DecisionAction.BUY if bullish else DecisionAction.SELL
    pullback = (current.low <= ema20 + tolerance and current.close > ema20) if bullish else (current.high >= ema20 - tolerance and current.close < ema20)
    outside_inside = (previous.close < ema20 - extension and current.close > ema20) if bullish else (previous.close > ema20 + extension and current.close < ema20)
    if not pullback and not outside_inside:
        return None
    stop = (min(current.low, previous.low) - atr14 * .25) if bullish else (max(current.high, previous.high) + atr14 * .25)
    entry = request.ask if bullish else request.bid
    points = abs(entry - stop) / request.point_size
    if points <= 0:
        return None
    partial_points, target_points = points, points * 2
    partial = entry + partial_points * request.point_size if bullish else entry - partial_points * request.point_size
    target = entry + target_points * request.point_size if bullish else entry - target_points * request.point_size
    return Setup(action, "pullback" if pullback else "outside_inside", entry, stop, partial, target, points, "", ema20, atr14)


def _setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _setup(request) if request.candles else None


def _momentum_atr_setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 52:
        return None
    closes = [candle.close for candle in candles]
    current = candles[-1]
    ema20 = _ema(closes[-20:])
    previous_ema20 = _ema(closes[-21:-1])
    ema50 = _ema(closes[-50:])
    atr14 = _atr(candles, 14)
    previous = candles[-13:-1]
    upper_breakout = max(candle.high for candle in previous)
    lower_breakout = min(candle.low for candle in previous)
    bullish = current.close > upper_breakout and current.close > ema20 and ema20 > ema50 and ema20 > previous_ema20
    bearish = current.close < lower_breakout and current.close < ema20 and ema20 < ema50 and ema20 < previous_ema20
    if not bullish and not bearish:
        return None
    action = DecisionAction.BUY if bullish else DecisionAction.SELL
    entry = request.ask if bullish else request.bid
    stop_distance = atr14 * 1.2
    stop = entry - stop_distance if bullish else entry + stop_distance
    stop_points = stop_distance / request.point_size
    if stop_points <= 0:
        return None
    partial = entry + stop_distance if bullish else entry - stop_distance
    target = entry + stop_distance * 2 if bullish else entry - stop_distance * 2
    return Setup(
        action,
        "momentum_atr_breakout",
        entry,
        stop,
        partial,
        target,
        stop_points,
        "not_applicable",
        ema20,
        atr14,
        ema50,
        upper_breakout if bullish else lower_breakout,
    )


def _momentum_setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _momentum_atr_setup(request) if request.candles else None


def _rsi_bollinger_setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 22:
        return None
    closes = [candle.close for candle in candles]
    current = candles[-1]
    basis_values = closes[-20:]
    basis = fmean(basis_values)
    deviation = pstdev(basis_values)
    upper_band = basis + deviation * 2
    lower_band = basis - deviation * 2
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(candles, 14)
    bullish = current.low <= lower_band and current.close > lower_band and current.close > current.open and rsi14 <= 45
    bearish = current.high >= upper_band and current.close < upper_band and current.close < current.open and rsi14 >= 55
    if not bullish and not bearish:
        return None
    action = DecisionAction.BUY if bullish else DecisionAction.SELL
    entry = request.ask if bullish else request.bid
    stop = current.low - atr14 * .25 if bullish else current.high + atr14 * .25
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or (bullish and basis <= entry) or (bearish and basis >= entry):
        return None
    stop_points = stop_distance / request.point_size
    partial = basis
    target = entry + stop_distance * 2 if bullish else entry - stop_distance * 2
    return Setup(
        action=action,
        setup_type="rsi_bollinger_reversion",
        entry=entry,
        stop=stop,
        partial=partial,
        target=target,
        stop_points=stop_points,
        flow_mode="not_applicable",
        ema20=basis,
        atr14=atr14,
        rsi14=rsi14,
        bollinger_upper=upper_band,
        bollinger_lower=lower_band,
    )


def _rsi_bollinger_setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _rsi_bollinger_setup(request) if request.candles else None


def _macd_trend_setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 60:
        return None
    closes = [candle.close for candle in candles]
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal = _ema_series(macd, 9)
    current = candles[-1]
    ema50 = _ema(closes[-50:])
    atr14 = _atr(candles, 14)
    bullish = macd[-2] <= signal[-2] and macd[-1] > signal[-1] and current.close > ema50
    bearish = macd[-2] >= signal[-2] and macd[-1] < signal[-1] and current.close < ema50
    if not bullish and not bearish:
        return None
    action = DecisionAction.BUY if bullish else DecisionAction.SELL
    entry = request.ask if bullish else request.bid
    stop_distance = atr14
    if stop_distance <= 0:
        return None
    stop = entry - stop_distance if bullish else entry + stop_distance
    partial = entry + stop_distance if bullish else entry - stop_distance
    target = entry + stop_distance * 2 if bullish else entry - stop_distance * 2
    return Setup(
        action=action,
        setup_type="macd_trend_crossover",
        entry=entry,
        stop=stop,
        partial=partial,
        target=target,
        stop_points=stop_distance / request.point_size,
        flow_mode="not_applicable",
        ema20=_ema(closes[-20:]),
        atr14=atr14,
        ema50=ema50,
        macd_line=macd[-1],
        macd_signal=signal[-1],
        macd_histogram=macd[-1] - signal[-1],
    )


def _macd_trend_setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _macd_trend_setup(request) if request.candles else None


def _stochastic_pullback_setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 60:
        return None
    closes = [candle.close for candle in candles]
    stochastic_k, stochastic_d = _stochastic_series(candles)
    current = candles[-1]
    ema50 = _ema(closes[-50:])
    atr14 = _atr(candles, 14)
    bullish = (
        current.close > ema50
        and stochastic_k[-2] <= stochastic_d[-2]
        and stochastic_k[-1] > stochastic_d[-1]
        and min(stochastic_k[-2], stochastic_d[-2]) <= 35
    )
    bearish = (
        current.close < ema50
        and stochastic_k[-2] >= stochastic_d[-2]
        and stochastic_k[-1] < stochastic_d[-1]
        and max(stochastic_k[-2], stochastic_d[-2]) >= 65
    )
    if not bullish and not bearish:
        return None
    action = DecisionAction.BUY if bullish else DecisionAction.SELL
    entry = request.ask if bullish else request.bid
    recent = candles[-3:]
    stop = min(candle.low for candle in recent) - atr14 * .25 if bullish else max(candle.high for candle in recent) + atr14 * .25
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None
    partial = entry + stop_distance if bullish else entry - stop_distance
    target = entry + stop_distance * 2 if bullish else entry - stop_distance * 2
    return Setup(
        action=action,
        setup_type="stochastic_trend_pullback",
        entry=entry,
        stop=stop,
        partial=partial,
        target=target,
        stop_points=stop_distance / request.point_size,
        flow_mode="not_applicable",
        ema20=_ema(closes[-20:]),
        atr14=atr14,
        ema50=ema50,
        stochastic_k=stochastic_k[-1],
        stochastic_d=stochastic_d[-1],
    )


def _stochastic_pullback_setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _stochastic_pullback_setup(request) if request.candles else None


def _aggregate_complete(candles: list[CandleSnapshot], minutes: int) -> list[CandleSnapshot]:
    grouped: list[CandleSnapshot] = []
    bucket: list[CandleSnapshot] = []
    bucket_time: datetime | None = None
    expected = minutes // 5
    for candle in candles:
        current_bucket = candle.timestamp_utc.replace(
            minute=(candle.timestamp_utc.minute // minutes) * minutes,
            second=0,
            microsecond=0,
        )
        if bucket_time is not None and current_bucket != bucket_time:
            if len(bucket) == expected:
                grouped.append(CandleSnapshot(
                    timestamp_utc=bucket_time,
                    open=bucket[0].open,
                    high=max(row.high for row in bucket),
                    low=min(row.low for row in bucket),
                    close=bucket[-1].close,
                    volume=sum(row.volume for row in bucket),
                ))
            bucket = []
        bucket_time = current_bucket
        bucket.append(candle)
    if bucket_time is not None and len(bucket) == expected:
        grouped.append(CandleSnapshot(
            timestamp_utc=bucket_time,
            open=bucket[0].open,
            high=max(row.high for row in bucket),
            low=min(row.low for row in bucket),
            close=bucket[-1].close,
            volume=sum(row.volume for row in bucket),
        ))
    return grouped


def _multitimeframe_trend_pullback_setup(request: DecisionRequest) -> Setup | None:
    candles = request.candles
    if len(candles) < 600 or not time(13, 0) <= request.timestamp_utc.astimezone(timezone.utc).time() < time(16, 0):
        return None
    hourly = _aggregate_complete(candles, 60)
    fifteen_minute = _aggregate_complete(candles, 15)
    if len(hourly) < 51 or len(fifteen_minute) < 22:
        return None
    hourly_closes = [row.close for row in hourly]
    m15_closes = [row.close for row in fifteen_minute]
    h1_ema20 = _ema(hourly_closes, 20)
    h1_previous_ema20 = _ema(hourly_closes[:-1], 20)
    h1_ema50 = _ema(hourly_closes, 50)
    h1_atr14 = _atr(hourly, 14)
    m15_ema9 = _ema(m15_closes, 9)
    m15_previous_ema9 = _ema(m15_closes[:-1], 9)
    m15_ema21 = _ema(m15_closes, 21)
    closes = [row.close for row in candles]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    atr14 = _atr(candles, 14)
    current = candles[-1]
    strength = abs(h1_ema20 - h1_ema50) / h1_atr14 if h1_atr14 else 0.0
    bullish = (
        strength >= .7
        and h1_ema20 > h1_ema50
        and h1_ema20 > h1_previous_ema20
        and m15_ema9 > m15_ema21
        and m15_ema9 > m15_previous_ema9
        and ema9 > ema21
        and current.low <= ema9
        and current.close > ema9
        and current.close > current.open
    )
    if not bullish:
        return None
    action = DecisionAction.BUY
    entry = request.ask
    stop_distance = atr14 * 1.2
    stop = entry - stop_distance
    target = entry + stop_distance * .75
    return Setup(
        action=action,
        setup_type="multitimeframe_trend_pullback",
        entry=entry,
        stop=stop,
        partial=target,
        target=target,
        stop_points=stop_distance / request.point_size,
        flow_mode="not_applicable",
        ema20=ema9,
        atr14=atr14,
        ema50=ema21,
        partial_close_pct=100.0,
        move_stop_to_breakeven=False,
        max_holding_bars=12,
        h1_ema20=h1_ema20,
        h1_ema50=h1_ema50,
        m15_ema9=m15_ema9,
        m15_ema21=m15_ema21,
        trend_strength=strength,
        risk_multiplier=.5,
    )


def _multitimeframe_setup_for_candle(request: DecisionRequest) -> Setup | None:
    return _multitimeframe_trend_pullback_setup(request) if request.candles else None


def _candidate_response(
    request: DecisionRequest,
    settings: Settings,
    setup: Setup,
    checks: list[RiskCheck],
    flow: FlowAnalysis,
    *,
    executable: bool,
    reason: str,
    strategy_profile: str,
) -> DecisionResponse:
    effective_risk_pct = settings.risk_per_trade_pct * setup.risk_multiplier
    size = round(_position_size(request.account.balance, effective_risk_pct, setup.stop_points, request.value_per_point_per_lot), 2)
    candle_timestamp = request.candles[-1].timestamp_utc
    metadata: dict[str, str | int | float | bool] = {
        "strategy_profile": strategy_profile,
        "candidate": True,
        "candle_timestamp_utc": candle_timestamp.isoformat(),
        "setup_type": setup.setup_type,
        "flow_status": flow.status,
        "flow_ready": flow.ready,
        "flow_confirmed": flow.confirmed,
        "flow_contradicted": flow.contradicted,
        "flow_policy": settings.fimath_flow_policy,
        "ema20": round(setup.ema20, 5),
        "atr14": round(setup.atr14, 5),
        "suggested_size": size,
        "suggested_action": setup.action.value,
        "risk_multiplier": setup.risk_multiplier,
        "effective_risk_pct": round(effective_risk_pct, 5),
    }
    if flow.book_z is not None:
        metadata["book_z"] = round(flow.book_z, 5)
    if flow.tick_z is not None:
        metadata["tick_z"] = round(flow.tick_z, 5)
    if flow.score is not None:
        metadata["flow_score"] = round(flow.score, 5)
    if setup.ema50 is not None:
        metadata["ema50"] = round(setup.ema50, 5)
    if setup.breakout_level is not None:
        metadata["breakout_level"] = round(setup.breakout_level, 5)
    if setup.rsi14 is not None:
        metadata["rsi14"] = round(setup.rsi14, 5)
    if setup.bollinger_upper is not None:
        metadata["bollinger_upper"] = round(setup.bollinger_upper, 5)
    if setup.bollinger_lower is not None:
        metadata["bollinger_lower"] = round(setup.bollinger_lower, 5)
    if setup.macd_line is not None:
        metadata["macd_line"] = round(setup.macd_line, 5)
    if setup.macd_signal is not None:
        metadata["macd_signal"] = round(setup.macd_signal, 5)
    if setup.macd_histogram is not None:
        metadata["macd_histogram"] = round(setup.macd_histogram, 5)
    if setup.stochastic_k is not None:
        metadata["stochastic_k"] = round(setup.stochastic_k, 5)
    if setup.stochastic_d is not None:
        metadata["stochastic_d"] = round(setup.stochastic_d, 5)
    for name in ("h1_ema20", "h1_ema50", "m15_ema9", "m15_ema21", "trend_strength"):
        value = getattr(setup, name)
        if value is not None:
            metadata[name] = round(value, 5)
    if setup.max_holding_bars is not None:
        metadata["max_holding_bars"] = setup.max_holding_bars
    partial_close_pct = setup.partial_close_pct if setup.partial_close_pct is not None else settings.fimath_partial_pct
    return DecisionResponse(
        action=setup.action if executable else DecisionAction.HOLD,
        reason=reason,
        size=size if executable else 0.0,
        risk_amount=round(request.account.balance * effective_risk_pct / 100, 2),
        entry_price=setup.entry,
        stop_loss_price=setup.stop,
        take_profit_price=setup.target,
        stop_loss_points=round(setup.stop_points, 2),
        take_profit_points=round(abs(setup.target - setup.entry) / request.point_size, 2),
        partial_take_profit_price=setup.partial,
        final_take_profit_price=setup.target,
        partial_close_pct=partial_close_pct,
        move_stop_to_breakeven=setup.move_stop_to_breakeven,
        risk_checks=checks,
        metadata=metadata,
    )


def _evaluate_fimath(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    flow_window = _record_flow(request, settings.fimath_flow_window_seconds)
    setup = _setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="fimath_setup", passed=setup is not None, detail="ema20 pullback/outside-inside"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="fimath_setup_not_confirmed", risk_checks=checks)
    flow = _flow_analysis(
        flow_window,
        setup.action,
        settings.fimath_flow_window_seconds,
        settings.fimath_flow_min_samples,
        settings.fimath_flow_z_threshold,
    )
    policy = settings.fimath_flow_policy
    flow_eligible = flow.ready and not flow.contradicted and (policy == "veto" or flow.confirmed)
    checks.append(RiskCheck(name="flow_ready", passed=flow.ready, detail=flow.status))
    checks.append(RiskCheck(name="flow_veto", passed=not flow.contradicted, detail=flow.status))
    checks.append(RiskCheck(name="flow_policy", passed=flow_eligible, detail=f"policy={policy}"))
    if not flow.ready:
        return _candidate_response(request, settings, setup, checks, flow, executable=False, reason=flow.status, strategy_profile=strategy_profile)
    if flow.contradicted:
        return _candidate_response(request, settings, setup, checks, flow, executable=False, reason="flow_contradiction", strategy_profile=strategy_profile)
    if policy == "required" and not flow.confirmed:
        return _candidate_response(request, settings, setup, checks, flow, executable=False, reason="flow_not_confirmed", strategy_profile=strategy_profile)
    executable = settings.fimath_mode == "live" and settings.enable_live_decisions and flow_eligible
    checks.append(RiskCheck(name="fimath_live_enabled", passed=executable, detail=f"mode={settings.fimath_mode}"))
    return _candidate_response(
        request,
        settings,
        setup,
        checks,
        flow,
        executable=executable,
        reason="fimath_validated" if executable else "fimath_shadow_signal",
        strategy_profile=strategy_profile,
    )


def _evaluate_momentum_atr(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _momentum_setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="momentum_atr_setup", passed=setup is not None, detail="ema20/ema50 + breakout_12 + atr14"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="momentum_atr_setup_not_confirmed", risk_checks=checks)
    flow = FlowAnalysis(True, False, False, "not_applicable")
    executable = settings.enable_live_decisions and settings.fimath_mode == "live"
    checks.append(RiskCheck(name="shadow_mode", passed=not executable, detail=f"mode={settings.fimath_mode}"))
    return _candidate_response(
        request,
        settings,
        setup,
        checks,
        flow,
        executable=executable,
        reason="momentum_atr_validated" if executable else "momentum_atr_shadow_signal",
        strategy_profile=strategy_profile,
    )


def _evaluate_rsi_bollinger(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _rsi_bollinger_setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="rsi_bollinger_setup", passed=setup is not None, detail="bollinger_20_2 + rsi14 + rejection"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="rsi_bollinger_setup_not_confirmed", risk_checks=checks)
    flow = FlowAnalysis(True, False, False, "not_applicable")
    executable = settings.enable_live_decisions and settings.fimath_mode == "live"
    checks.append(RiskCheck(name="shadow_mode", passed=not executable, detail=f"mode={settings.fimath_mode}"))
    return _candidate_response(
        request,
        settings,
        setup,
        checks,
        flow,
        executable=executable,
        reason="rsi_bollinger_validated" if executable else "rsi_bollinger_shadow_signal",
        strategy_profile=strategy_profile,
    )


def _evaluate_macd_trend(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _macd_trend_setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="macd_trend_setup", passed=setup is not None, detail="macd_12_26_9 crossover + ema50 trend"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="macd_trend_setup_not_confirmed", risk_checks=checks)
    flow = FlowAnalysis(True, False, False, "not_applicable")
    executable = settings.enable_live_decisions and settings.fimath_mode == "live"
    checks.append(RiskCheck(name="shadow_mode", passed=not executable, detail=f"mode={settings.fimath_mode}"))
    return _candidate_response(
        request,
        settings,
        setup,
        checks,
        flow,
        executable=executable,
        reason="macd_trend_validated" if executable else "macd_trend_shadow_signal",
        strategy_profile=strategy_profile,
    )


def _evaluate_stochastic_pullback(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _stochastic_pullback_setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="stochastic_pullback_setup", passed=setup is not None, detail="stochastic_14_3_3 crossover + ema50 trend"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="stochastic_pullback_setup_not_confirmed", risk_checks=checks)
    flow = FlowAnalysis(True, False, False, "not_applicable")
    executable = settings.enable_live_decisions and settings.fimath_mode == "live"
    checks.append(RiskCheck(name="shadow_mode", passed=not executable, detail=f"mode={settings.fimath_mode}"))
    return _candidate_response(
        request,
        settings,
        setup,
        checks,
        flow,
        executable=executable,
        reason="stochastic_pullback_validated" if executable else "stochastic_pullback_shadow_signal",
        strategy_profile=strategy_profile,
    )


def _evaluate_stochastic_sell_session(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    response = _evaluate_stochastic_pullback(request, settings, strategy_profile)
    if not response.metadata.get("candidate"):
        return response

    suggested_action = str(response.metadata.get("suggested_action", ""))
    before_entry_cutoff = request.timestamp_utc.astimezone(timezone.utc).time() < time(18, 0)
    sell_only = suggested_action == DecisionAction.SELL.value
    response.risk_checks.extend(
        [
            RiskCheck(name="experimental_sell_only", passed=sell_only, detail=f"suggested_action={suggested_action}"),
            RiskCheck(name="experimental_entry_cutoff", passed=before_entry_cutoff, detail="entry_before=18:00_UTC"),
        ]
    )
    response.metadata["experiment"] = "sell_only_entry_before_18utc"
    if not sell_only or not before_entry_cutoff:
        response.action = DecisionAction.HOLD
        response.reason = "experimental_side_or_session_block"
        response.size = 0.0
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
        response.metadata["experimental_block_reason"] = "buy_side" if not sell_only else "entry_at_or_after_18utc"
        return response

    response.action = DecisionAction.HOLD
    response.reason = "stochastic_sell_session_shadow_signal"
    response.size = 0.0
    response.metadata["experimental_shadow_lock"] = True
    response.risk_checks.append(
        RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled")
    )
    return response


def _evaluate_multitimeframe_trend_pullback(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _multitimeframe_setup_for_candle(request)
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == settings.default_symbol.upper(), detail=f"expected={settings.default_symbol}"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == settings.default_timeframe.upper(), detail=f"expected={settings.default_timeframe}"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="multitimeframe_setup", passed=setup is not None, detail="bullish_h1_ema20_50_strength_0.7 + bullish_m15_ema9_21 + m5_pullback + 13_16utc"),
    ]
    if not all(check.passed for check in checks[:3]):
        return DecisionResponse(action=DecisionAction.HOLD, reason="market_filter_mismatch", risk_checks=checks)
    if setup is None:
        return DecisionResponse(action=DecisionAction.HOLD, reason="multitimeframe_setup_not_confirmed", risk_checks=checks)
    response = _candidate_response(
        request,
        settings,
        setup,
        checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason="multitimeframe_trend_pullback_shadow_signal",
        strategy_profile=strategy_profile,
    )
    response.metadata["experimental_shadow_lock"] = True
    response.metadata["research_status"] = "exploratory_requires_forward_validation"
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


def _evaluate_happy_gold_zigzag_scalper(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    setup = _happy_gold_zigzag_setup(request)
    session_time = request.timestamp_utc.astimezone(timezone.utc).time()
    in_research_session = time(3, 0) <= session_time < time(19, 0)
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=M30"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="research_session", passed=in_research_session, detail="03:00-19:00_UTC_proxy"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
        RiskCheck(name="zigzag_breakout", passed=setup is not None, detail="confirmed_M30_fractal_breakout"),
    ]
    if not all(check.passed for check in checks[:5]):
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_market_or_risk_filter_mismatch",
            risk_checks=checks,
            metadata={"strategy_profile": strategy_profile, "candidate": False, "experimental_shadow_lock": True},
        )
    if setup is None:
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_zigzag_breakout_not_confirmed",
            risk_checks=checks,
            metadata={"strategy_profile": strategy_profile, "candidate": False, "experimental_shadow_lock": True},
        )
    response = _candidate_response(
        request,
        settings,
        setup,
        checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason="happy_gold_zigzag_scalper_shadow_signal",
        strategy_profile=strategy_profile,
    )
    fixed_lot = .10
    response.risk_amount = round(setup.stop_points * request.value_per_point_per_lot * fixed_lot, 2)
    break_even_trigger = float(getattr(settings, "happy_gold_break_even_trigger_points", 40))
    break_even_offset = float(getattr(settings, "happy_gold_break_even_offset_points", 0))
    trailing_activation = float(getattr(settings, "happy_gold_trailing_activation_points", 120))
    trailing_distance = float(getattr(settings, "happy_gold_trailing_distance_points", 60))
    max_holding_bars = int(getattr(settings, "happy_gold_max_holding_bars", 2))
    response.metadata.update(
        {
            "suggested_size": fixed_lot,
            "signal_timeframe": "M30",
            "execution_timeframe": "M5",
            "fixed_stop_points": 240,
            "fixed_target_points": 1000,
            "break_even_trigger_points": break_even_trigger,
            "break_even_offset_points": break_even_offset,
            "trailing_activation_points": trailing_activation,
            "trailing_distance_points": trailing_distance,
            "max_holding_bars": max_holding_bars,
            "break_even_and_trailing": "active_research_proxy",
            "source_sample_trades": 773,
            "experimental_shadow_lock": True,
            "research_status": "reverse_engineered_proxy_requires_tick_forward_validation",
        }
    )
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


def _evaluate_happy_gold_zigzag_regime_confirmed(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    """Preserve Bot 12 geometry while confirming ambiguous regimes causally."""
    # The H1 classifier needs deep history, while the reconstructed ZigZag uses
    # the same 60-bar signal window as the Bot 12 replay. Keeping the windows
    # separate avoids changing the base pivots and prevents quadratic work.
    signal_request = request.model_copy(update={"candles": request.candles[-60:]})
    response = _evaluate_happy_gold_zigzag_scalper(signal_request, settings, strategy_profile)
    response.metadata["selective_regime_confirmation"] = True
    if not response.metadata.get("candidate"):
        return response

    override = getattr(settings, "happy_gold_regime_snapshot", None)
    regime = override if isinstance(override, RegimeSnapshot) else classify_regime(request.candles, settings)
    side = str(response.metadata.get("suggested_action", ""))
    breakout_level = float(response.metadata.get("breakout_level", 0))
    last_closed_price = request.candles[-1].close
    close_confirmed = last_closed_price > breakout_level if side == "buy" else last_closed_price < breakout_level
    directional_alignment = regime.direction == side

    if regime.name in {"range", "trend"}:
        allowed = True
        confirmation_reason = f"base_breakout_allowed:{regime.name}"
    elif regime.name in {"transition", "high_volatility"}:
        allowed = close_confirmed and directional_alignment
        confirmation_reason = (
            f"selective_confirmation:{regime.name}:close={str(close_confirmed).lower()}:"
            f"direction={regime.direction}:side={side}"
        )
    else:
        allowed = False
        confirmation_reason = f"unsupported_or_unready_regime:{regime.name}"

    response.metadata.update(
        {
            "strategy_profile": strategy_profile,
            "zigzag_regime": regime.name,
            "zigzag_regime_direction": regime.direction,
            "zigzag_regime_adx": regime.adx,
            "zigzag_regime_atr_percentile": regime.atr_percentile,
            "zigzag_close_confirmed": close_confirmed,
            "zigzag_direction_aligned": directional_alignment,
            "zigzag_confirmation_reason": confirmation_reason,
            "research_status": "parallel_selective_confirmation_forward_validation_required",
        }
    )
    response.risk_checks.append(RiskCheck(name="zigzag_selective_regime_confirmation", passed=allowed, detail=confirmation_reason))
    if not allowed:
        response.action = DecisionAction.HOLD
        response.reason = "happy_gold_selective_confirmation_block"
        response.size = 0.0
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
        return response

    response.reason = "happy_gold_zigzag_regime_confirmed_shadow_signal"
    return response


def _evaluate_happy_gold_zigzag_retest_atr(request: DecisionRequest, settings: Settings, strategy_profile: str) -> DecisionResponse:
    """V2: trade only trend-aligned M30 breakouts that survive an M5 retest."""
    setup = _happy_gold_retest_setup(request, settings)
    session_time = request.timestamp_utc.astimezone(timezone.utc).time()
    in_session = time(7, 0) <= session_time < time(16, 0)
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    override = getattr(settings, "happy_gold_regime_snapshot", None)
    regime = override if isinstance(override, RegimeSnapshot) else classify_regime(request.candles, settings)
    side = setup.action.value if setup is not None else "none"
    trend_aligned = setup is not None and regime.name == "trend" and regime.direction == side
    volatility_allowed = regime.atr_percentile is None or 10 <= regime.atr_percentile <= 90
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=M30; confirmation=M5"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="research_session", passed=in_session, detail="07:00-16:00_UTC"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
        RiskCheck(name="zigzag_breakout_retest", passed=setup is not None, detail="M30_confirmed_pivot + M5_close_breakout + M5_retest_rejection"),
        RiskCheck(name="trend_alignment", passed=trend_aligned, detail=f"regime={regime.name};direction={regime.direction};side={side}"),
        RiskCheck(name="volatility_band", passed=volatility_allowed, detail=f"atr_percentile={regime.atr_percentile};allowed=10-90"),
    ]
    if not all(check.passed for check in checks):
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_v2_filter_or_retest_block",
            risk_checks=checks,
            metadata={
                "strategy_profile": strategy_profile,
                "candidate": False,
                "experimental_shadow_lock": True,
                "zigzag_regime": regime.name,
                "zigzag_regime_direction": regime.direction,
            },
        )
    assert setup is not None
    response = _candidate_response(
        request,
        settings,
        setup,
        checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason="happy_gold_zigzag_retest_atr_shadow_signal",
        strategy_profile=strategy_profile,
    )
    break_even_points = setup.stop_points * float(getattr(settings, "happy_gold_v2_break_even_r", .8))
    trailing_activation_points = setup.stop_points * float(getattr(settings, "happy_gold_v2_trailing_activation_r", 1.2))
    trailing_distance_points = setup.atr14 * float(getattr(settings, "happy_gold_v2_trailing_atr", .8)) / request.point_size
    response.metadata.update(
        {
            "signal_timeframe": "M30",
            "confirmation_timeframe": "M5",
            "entry_model": "breakout_retest_rejection",
            "stop_model": "max_1.5_atr_or_retest_structure",
            "target_r": float(getattr(settings, "happy_gold_v2_target_r", 2.0)),
            "break_even_trigger_points": break_even_points,
            "break_even_offset_points": 0.0,
            "trailing_activation_points": trailing_activation_points,
            "trailing_distance_points": trailing_distance_points,
            "max_holding_bars": int(getattr(settings, "happy_gold_v2_max_holding_bars", 12)),
            "zigzag_regime": regime.name,
            "zigzag_regime_direction": regime.direction,
            "zigzag_regime_atr_percentile": regime.atr_percentile if regime.atr_percentile is not None else -1,
            "experimental_shadow_lock": True,
            "research_status": "v2_candidate_requires_walk_forward_and_tick_forward_validation",
        }
    )
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


HAPPY_GOLD_TICK_IMPULSE_PROFILE = "happy_gold_tick_impulse_shadow"
HAPPY_GOLD_ZIGZAG_TICK_CONTEXT_PROFILE = "happy_gold_zigzag_tick_context_shadow"
HAPPY_GOLD_ZIGZAG_STRUCTURAL_TICK_PROFILE = "happy_gold_zigzag_structural_tick_shadow"
HAPPY_GOLD_ZIGZAG_NORMALIZED_RANGE_PROFILE = "happy_gold_zigzag_normalized_range_shadow"
HAPPY_GOLD_MULTIWINDOW_TICK_SCORE_PROFILE = "happy_gold_multiwindow_tick_score_shadow"
HAPPY_GOLD_PIP_SIZE = 0.01


def _evaluate_happy_gold_tick_impulse(request: DecisionRequest, settings: Settings) -> DecisionResponse:
    threshold_pips = float(getattr(settings, "happy_gold_impulse_30s_threshold_pips", 95.5))
    ready = request.indicators.get("impulse_30s_ready") == 1
    move_points = float(request.indicators.get("impulse_30s_move_points", 0))
    move_pips = move_points * request.point_size / HAPPY_GOLD_PIP_SIZE
    session_start = time.fromisoformat(str(getattr(settings, "happy_gold_impulse_session_start_utc", "00:00")))
    session_end = time.fromisoformat(str(getattr(settings, "happy_gold_impulse_session_end_utc", "17:00")))
    session_time = request.timestamp_utc.astimezone(timezone.utc).time()
    in_session = session_start <= session_time < session_end
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=ticks_30s"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="impulse_window_ready", passed=ready, detail="window=30s"),
        RiskCheck(name="impulse_threshold", passed=ready and abs(move_pips) >= threshold_pips, detail=f"absolute_min_pips={threshold_pips}"),
        RiskCheck(name="research_session", passed=in_session, detail=f"{session_start.isoformat()}-{session_end.isoformat()}_UTC"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
    ]
    if not all(check.passed for check in checks):
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_tick_impulse_filter_block",
            risk_checks=checks,
            metadata={
                "strategy_profile": HAPPY_GOLD_TICK_IMPULSE_PROFILE,
                "candidate": False,
                "impulse_30s_move_points": round(move_points, 6),
                "impulse_30s_move_pips": round(move_pips, 6),
                "impulse_30s_threshold_pips": threshold_pips,
                "experimental_shadow_lock": True,
            },
        )

    action = DecisionAction.BUY if move_pips > 0 else DecisionAction.SELL
    entry = request.ask if action == DecisionAction.BUY else request.bid
    stop_pips = 240.0
    target_pips = 1000.0
    stop_distance = stop_pips * HAPPY_GOLD_PIP_SIZE
    target_distance = target_pips * HAPPY_GOLD_PIP_SIZE
    stop_points = stop_distance / request.point_size
    direction = 1 if action == DecisionAction.BUY else -1
    setup = Setup(
        action=action,
        setup_type="happy_gold_tick_impulse_30s",
        entry=entry,
        stop=entry - direction * stop_distance,
        partial=entry + direction * target_distance,
        target=entry + direction * target_distance,
        stop_points=stop_points,
        flow_mode="not_applicable",
        ema20=(request.bid + request.ask) / 2,
        atr14=0.0,
        partial_close_pct=100.0,
        move_stop_to_breakeven=False,
    )
    response = _candidate_response(
        request,
        settings,
        setup,
        checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason="happy_gold_tick_impulse_shadow_signal",
        strategy_profile=HAPPY_GOLD_TICK_IMPULSE_PROFILE,
    )
    fixed_lot = 0.10
    signal_timestamp = request.timestamp_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    response.risk_amount = round(stop_points * request.value_per_point_per_lot * fixed_lot, 2)
    response.metadata.update(
        {
            "suggested_size": fixed_lot,
            "signal_timeframe": "T30S",
            "impulse_30s_move_points": round(move_points, 6),
            "impulse_30s_move_pips": round(move_pips, 6),
            "impulse_30s_threshold_pips": threshold_pips,
            "signal_timestamp_utc": signal_timestamp.isoformat(),
            "fixed_stop_pips": int(stop_pips),
            "fixed_target_pips": int(target_pips),
            "record_paper_trade": False,
            "experimental_shadow_lock": True,
            "research_status": "tick_forward_validation_required",
            "source_entry_sample": 30,
            "source_control_sample": 146,
        }
    )
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


def _evaluate_happy_gold_zigzag_tick_context(
    request: DecisionRequest, settings: Settings
) -> DecisionResponse:
    setup = _happy_gold_zigzag_setup(request)
    ready = request.indicators.get("impulse_900s_ready") == 1
    move_points = float(request.indicators.get("impulse_900s_move_points", 0))
    move_pips = move_points * request.point_size / HAPPY_GOLD_PIP_SIZE
    threshold_pips = float(
        getattr(settings, "happy_gold_zigzag_tick_context_900s_threshold_pips", 800)
    )
    setup_direction = 0 if setup is None else (1 if setup.action == DecisionAction.BUY else -1)
    aligned_move_pips = move_pips * setup_direction
    session_time = request.timestamp_utc.astimezone(timezone.utc).time()
    in_session = time(3, 0) <= session_time < time(19, 0)
    same_symbol_positions = [
        position for position in request.positions
        if position.symbol.upper() == request.symbol.upper()
    ]
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=M30+ticks_900s"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="research_session", passed=in_session, detail="03:00-19:00_UTC_proxy"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
        RiskCheck(name="zigzag_breakout", passed=setup is not None, detail="confirmed_M30_fractal_breakout"),
        RiskCheck(name="tick_context_window_ready", passed=ready, detail="window=900s"),
        RiskCheck(
            name="tick_context_directional_threshold",
            passed=setup is not None and ready and aligned_move_pips >= threshold_pips,
            detail=f"aligned_min_pips={threshold_pips}",
        ),
    ]
    metadata = {
        "strategy_profile": HAPPY_GOLD_ZIGZAG_TICK_CONTEXT_PROFILE,
        "candidate": False,
        "impulse_900s_move_points": round(move_points, 6),
        "impulse_900s_move_pips": round(move_pips, 6),
        "impulse_900s_aligned_move_pips": round(aligned_move_pips, 6),
        "impulse_900s_threshold_pips": threshold_pips,
        "experimental_shadow_lock": True,
    }
    if not all(check.passed for check in checks):
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_zigzag_tick_context_filter_block",
            risk_checks=checks,
            metadata=metadata,
        )

    assert setup is not None
    response = _candidate_response(
        request,
        settings,
        setup,
        checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason="happy_gold_zigzag_tick_context_shadow_signal",
        strategy_profile=HAPPY_GOLD_ZIGZAG_TICK_CONTEXT_PROFILE,
    )
    response.risk_amount = round(setup.stop_points * request.value_per_point_per_lot * .10, 2)
    response.metadata.update(metadata | {
        "candidate": True,
        "suggested_size": .10,
        "signal_timeframe": "M30+T900S",
        "fixed_stop_pips": 240,
        "fixed_target_pips": 1000,
        "record_paper_trade": False,
        "research_status": "retrospective_candidate_requires_full_tick_replay_and_forward_validation",
        "source_entry_sample": 123,
        "source_hybrid_sample": 15,
    })
    response.risk_checks.append(
        RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled")
    )
    return response


def _evaluate_happy_gold_zigzag_structural_tick(
    request: DecisionRequest,
    settings: Settings,
    strategy_profile: str = HAPPY_GOLD_ZIGZAG_STRUCTURAL_TICK_PROFILE,
    normalized_range_max: float | None = None,
) -> DecisionResponse:
    setup = _happy_gold_zigzag_setup(request)
    ready = request.indicators.get("impulse_900s_ready") == 1
    move_points = float(request.indicators.get("impulse_900s_move_points", 0))
    move_pips = move_points * request.point_size / HAPPY_GOLD_PIP_SIZE
    direction = 0 if setup is None else (1 if setup.action == DecisionAction.BUY else -1)
    aligned_context_pips = direction * move_pips
    closes = [candle.close for candle in request.candles]
    ema20 = _ema(closes[-20:]) if len(closes) >= 20 else closes[-1]
    directional_ema20_gap_pips = direction * (closes[-1] - ema20) / HAPPY_GOLD_PIP_SIZE
    pivot_extension_pips = (
        direction * (setup.entry - float(setup.breakout_level)) / HAPPY_GOLD_PIP_SIZE
        if setup is not None and setup.breakout_level is not None else 0.0
    )
    context_threshold = float(getattr(settings, "happy_gold_structural_context_900s_threshold_pips", 1000))
    ema_gap_threshold = float(getattr(settings, "happy_gold_structural_ema20_gap_pips", 900))
    max_extension = float(getattr(settings, "happy_gold_structural_max_pivot_extension_pips", 50))
    completed = request.candles[:-1]
    normalized_prior_m5_range: float | None = None
    if len(completed) >= 15:
        history = completed[-15:]
        true_ranges = [
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
            for previous, candle in zip(history[-15:-1], history[-14:])
        ]
        atr14 = fmean(true_ranges) if true_ranges else 0.0
        if atr14:
            normalized_prior_m5_range = (history[-1].high - history[-1].low) / atr14
    timestamp = request.timestamp_utc.astimezone(timezone.utc)
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=M30+M5+ticks_900s"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="research_session", passed=time(3, 0) <= timestamp.time() < time(19, 0), detail="03:00-19:00_UTC_proxy"),
        RiskCheck(name="weekday_filter", passed=timestamp.weekday() != 4, detail="friday_excluded_by_holdout_selector"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
        RiskCheck(name="zigzag_breakout", passed=setup is not None, detail="confirmed_M30_fractal_breakout"),
        RiskCheck(name="tick_context_window_ready", passed=ready, detail="window=900s"),
        RiskCheck(name="tick_context_directional_threshold", passed=ready and aligned_context_pips >= context_threshold, detail=f"aligned_min_pips={context_threshold}"),
        RiskCheck(name="ema20_directional_gap", passed=setup is not None and directional_ema20_gap_pips >= ema_gap_threshold, detail=f"aligned_min_pips={ema_gap_threshold}"),
        RiskCheck(name="pivot_extension", passed=setup is not None and pivot_extension_pips <= max_extension, detail=f"max_pips={max_extension}"),
    ]
    if normalized_range_max is not None:
        normalized_max_spread = float(
            getattr(settings, "happy_gold_normalized_range_max_spread_points", 15)
        )
        checks.append(RiskCheck(
            name="normalized_prior_m5_range",
            passed=normalized_prior_m5_range is not None and normalized_prior_m5_range <= normalized_range_max,
            detail=f"max={normalized_range_max}",
        ))
        checks.append(RiskCheck(
            name="normalized_range_spread_limit",
            passed=request.spread_points <= normalized_max_spread,
            detail=f"max={normalized_max_spread}",
        ))
    metadata = {
        "strategy_profile": strategy_profile,
        "candidate": False,
        "impulse_900s_move_pips": round(move_pips, 6),
        "impulse_900s_aligned_move_pips": round(aligned_context_pips, 6),
        "directional_ema20_gap_pips": round(directional_ema20_gap_pips, 6),
        "pivot_extension_pips": round(pivot_extension_pips, 6),
        "normalized_prior_m5_range": (
            round(normalized_prior_m5_range, 6) if normalized_prior_m5_range is not None else None
        ),
        "experimental_shadow_lock": True,
    }
    reason_prefix = (
        "happy_gold_zigzag_structural_tick"
        if strategy_profile == HAPPY_GOLD_ZIGZAG_STRUCTURAL_TICK_PROFILE
        else strategy_profile
    )
    if not all(check.passed for check in checks):
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason=f"{reason_prefix}_filter_block",
            risk_checks=checks,
            metadata=metadata,
        )
    assert setup is not None
    research_stop_pips = 60.0
    setup.stop_points = research_stop_pips * HAPPY_GOLD_PIP_SIZE / request.point_size
    setup.stop = setup.entry - direction * research_stop_pips * HAPPY_GOLD_PIP_SIZE
    response = _candidate_response(
        request, settings, setup, checks,
        FlowAnalysis(True, False, False, "not_applicable"),
        executable=False,
        reason=f"{reason_prefix}_shadow_signal" if strategy_profile == HAPPY_GOLD_ZIGZAG_STRUCTURAL_TICK_PROFILE else f"{reason_prefix}_signal",
        strategy_profile=strategy_profile,
    )
    response.risk_amount = round(setup.stop_points * request.value_per_point_per_lot * .10, 2)
    response.metadata.update(metadata | {
        "candidate": True,
        "suggested_size": .10,
        "signal_timeframe": "M30+M5+T900S",
        "fixed_stop_pips": int(research_stop_pips),
        "fixed_target_pips": 1000,
        "break_even_trigger_pips": 40,
        "break_even_offset_pips": 1,
        "trailing_activation_pips": 40,
        "trailing_distance_pips": 20,
        "max_holding_seconds": 60,
        "exit_research_win_rate_pct": 66.667,
        "exit_research_profit_factor": 3.092,
        "tick_shadow_lifecycle": True,
        "record_paper_trade": False,
        "research_status": "small_sample_holdout_candidate_requires_forward_validation",
        "annual_replay_signals": 27,
    })
    if normalized_range_max is not None:
        response.metadata.update({
            "normalized_range_max": normalized_range_max,
            "oos_2026_trades": 75,
            "oos_2026_win_rate_pct": 57.333,
            "oos_2026_profit_factor": 2.485,
            "oos_2026_max_drawdown": 34.22,
            "fresh_oos_2026_apr_aug_trades": 39,
            "fresh_oos_2026_apr_aug_win_rate_pct": 51.282,
            "fresh_oos_2026_apr_aug_profit_factor": 1.613,
            "fresh_oos_2026_apr_aug_max_drawdown": 37.51,
            "execution_stress_profitable_scenarios": 16,
            "execution_stress_total_scenarios": 16,
            "operational_max_spread_points": normalized_max_spread,
            "operational_latency_target_ms": 500,
            "research_status": "walk_forward_candidate_requires_live_broker_tick_forward_validation",
        })
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


def _evaluate_happy_gold_multiwindow_tick_score(
    request: DecisionRequest, settings: Settings
) -> DecisionResponse:
    thresholds = {5: 15.0, 10: 32.5, 30: 76.0, 60: 79.0, 300: 324.5, 900: 372.35}
    ready = {seconds: request.indicators.get(f"impulse_{seconds}s_ready") == 1 for seconds in thresholds}
    moves = {
        seconds: float(request.indicators.get(f"impulse_{seconds}s_move_points", 0)) * request.point_size / HAPPY_GOLD_PIP_SIZE
        for seconds in thresholds
    }
    direction = 1 if moves[60] > 0 else -1
    aligned = {seconds: direction * move for seconds, move in moves.items()}
    passed = {seconds: ready[seconds] and aligned[seconds] >= threshold for seconds, threshold in thresholds.items()}
    score = sum(passed.values())
    timestamp = request.timestamp_utc.astimezone(timezone.utc)
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    core_passed = all(passed[seconds] for seconds in (60, 300, 900))
    checks = [
        RiskCheck(name="symbol_match", passed=request.symbol.upper() == "XAUUSD", detail="expected=XAUUSD"),
        RiskCheck(name="timeframe_match", passed=request.timeframe.upper() == "M5", detail="expected=M5; signal=ticks_5_10_30_60_300_900s"),
        RiskCheck(name="spread_limit", passed=request.spread_points <= settings.max_spread_points, detail=f"max={settings.max_spread_points}"),
        RiskCheck(name="research_session", passed=time(0, 0) <= timestamp.time() < time(19, 0), detail="00:00-19:00_UTC"),
        RiskCheck(name="single_position", passed=not same_symbol_positions, detail="max_positions_for_symbol=1"),
        RiskCheck(name="core_windows", passed=core_passed, detail="aligned_60s_300s_900s"),
        RiskCheck(name="multiwindow_score", passed=score >= 4, detail=f"score={score}/6; min=4"),
        RiskCheck(
            name="exhaustive_replay_gate",
            passed=bool(getattr(settings, "happy_gold_multiwindow_shadow_enabled", False)),
            detail="disabled_after_2026_tick_replay_pf_0.806",
        ),
    ]
    metadata = {
        "strategy_profile": HAPPY_GOLD_MULTIWINDOW_TICK_SCORE_PROFILE,
        "candidate": False,
        "multiwindow_score": score,
        "experimental_shadow_lock": True,
    }
    for seconds, threshold in thresholds.items():
        metadata[f"impulse_{seconds}s_threshold_pips"] = threshold
        metadata[f"impulse_{seconds}s_aligned_move_pips"] = round(aligned[seconds], 6)
    if not all(check.passed for check in checks):
        return DecisionResponse(action=DecisionAction.HOLD, reason="happy_gold_multiwindow_tick_score_block", risk_checks=checks, metadata=metadata)

    action = DecisionAction.BUY if direction > 0 else DecisionAction.SELL
    entry = request.ask if action == DecisionAction.BUY else request.bid
    stop_pips = 60.0
    target_pips = 1000.0
    stop_points = stop_pips * HAPPY_GOLD_PIP_SIZE / request.point_size
    setup = Setup(
        action=action, setup_type="happy_gold_multiwindow_tick_score", entry=entry,
        stop=entry - direction * stop_pips * HAPPY_GOLD_PIP_SIZE,
        partial=entry + direction * target_pips * HAPPY_GOLD_PIP_SIZE,
        target=entry + direction * target_pips * HAPPY_GOLD_PIP_SIZE,
        stop_points=stop_points, flow_mode="not_applicable", ema20=(request.bid + request.ask) / 2,
        atr14=0.0, partial_close_pct=100.0, move_stop_to_breakeven=False,
    )
    response = _candidate_response(
        request, settings, setup, checks, FlowAnalysis(True, False, False, "not_applicable"),
        executable=False, reason="happy_gold_multiwindow_tick_score_shadow_signal",
        strategy_profile=HAPPY_GOLD_MULTIWINDOW_TICK_SCORE_PROFILE,
    )
    response.risk_amount = round(stop_points * request.value_per_point_per_lot * .10, 2)
    response.metadata.update(metadata | {
        "candidate": True, "suggested_size": .10, "signal_timeframe": "T5S-T900S",
        "fixed_stop_pips": 60, "fixed_target_pips": 1000,
        "break_even_trigger_pips": 40, "break_even_offset_pips": 1,
        "trailing_activation_pips": 40, "trailing_distance_pips": 20,
        "max_holding_seconds": 60, "tick_shadow_lifecycle": True,
        "record_paper_trade": False,
        "research_status": "rejected_by_exhaustive_tick_replay",
        "exhaustive_replay_trades": 43610,
        "exhaustive_replay_win_rate_pct": 45.971,
        "exhaustive_replay_profit_factor": 0.806,
        "paired_entry_sample": 95, "paired_control_sample": 470,
    })
    response.risk_checks.append(RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"))
    return response


def _apply_portfolio_allocation(
    response: DecisionResponse,
    request: DecisionRequest,
    settings: Settings,
    *,
    component: str,
    weight: float,
) -> DecisionResponse:
    suggested_size = _position_size(
        request.account.balance,
        settings.risk_per_trade_pct * weight,
        float(response.stop_loss_points or 0),
        request.value_per_point_per_lot,
    )
    response.action = DecisionAction.HOLD
    response.reason = "portfolio_6_7_shadow_signal"
    response.size = 0.0
    response.risk_amount = round(request.account.balance * settings.risk_per_trade_pct * weight / 100, 2)
    response.metadata.update(
        {
            "strategy_profile": "portfolio_6_7",
            "portfolio_component": component,
            "portfolio_weight_pct": round(weight * 100, 2),
            "risk_multiplier": weight,
            "effective_risk_pct": round(settings.risk_per_trade_pct * weight, 5),
            "suggested_size": round(suggested_size, 2),
            "portfolio_priority": "bot-7_then_bot-6",
            "portfolio_shared_position_limit": 1,
            "portfolio_shared_daily_loss_limit_r": 2,
            "experimental_shadow_lock": True,
            "research_status": "integrated_replay_and_forward_validation_required",
        }
    )
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    exposure_available = not same_symbol_positions
    response.risk_checks.extend(
        [
            RiskCheck(name="portfolio_shared_exposure", passed=exposure_available, detail="max_positions_for_symbol=1"),
            RiskCheck(name="portfolio_component_allocation", passed=True, detail=f"{component}={weight:.2f}"),
            RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
        ]
    )
    if not exposure_available:
        response.reason = "portfolio_shared_exposure_block"
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
    return response


def _evaluate_portfolio_6_7(request: DecisionRequest, settings: Settings) -> DecisionResponse:
    # Both components are evaluated independently. Bot 7 is selected first when
    # both produce a candidate on the same candle, preventing opposite exposure.
    bot_7 = _evaluate_multitimeframe_trend_pullback(request, settings, "portfolio_6_7")
    bot_6 = _evaluate_stochastic_sell_session(request, settings, "portfolio_6_7")
    bot_7_candidate = bool(bot_7.metadata.get("candidate"))
    bot_6_candidate = bool(bot_6.metadata.get("candidate"))

    if bot_7_candidate:
        bot_7.metadata["portfolio_conflict_detected"] = bot_6_candidate
        return _apply_portfolio_allocation(bot_7, request, settings, component="bot-7", weight=.75)
    if bot_6_candidate:
        bot_6.metadata["portfolio_conflict_detected"] = False
        return _apply_portfolio_allocation(bot_6, request, settings, component="bot-6", weight=.25)

    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="portfolio_components_not_confirmed",
        risk_checks=[
            RiskCheck(name="bot_7_component", passed=False, detail=bot_7.reason),
            RiskCheck(name="bot_6_component", passed=False, detail=bot_6.reason),
            RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
        ],
        metadata={
            "strategy_profile": "portfolio_6_7",
            "candidate": False,
            "portfolio_priority": "bot-7_then_bot-6",
            "portfolio_bot_7_reason": bot_7.reason,
            "portfolio_bot_6_reason": bot_6.reason,
            "portfolio_shared_position_limit": 1,
            "portfolio_shared_daily_loss_limit_r": 2,
            "experimental_shadow_lock": True,
        },
    )


WEIGHTED_PORTFOLIO_PROFILES = {
    "portfolio_6_7_100_100": {"bot-7": 1.00, "bot-6": 1.00},
}

HAPPY_GOLD_COMBINED_PROFILE = "happy_gold_portfolio_100_100"


def _portfolio_regime(request: DecisionRequest, settings: Settings) -> RegimeSnapshot:
    override = getattr(settings, "portfolio_regime_snapshot", None)
    if isinstance(override, RegimeSnapshot):
        return override
    return classify_regime(request.candles, settings)


def _portfolio_runtime_risk_check(request: DecisionRequest, settings: Settings) -> tuple[bool, str]:
    state = getattr(settings, "portfolio_risk_state", {})
    trades_started = int(state.get("trades_started", 0))
    daily_outcome_r = float(state.get("daily_outcome_r", 0))
    cooldown_until_raw = state.get("cooldown_until_utc")
    cooldown_until = datetime.fromisoformat(str(cooldown_until_raw)) if cooldown_until_raw else None
    daily_trade_cap = getattr(settings, "portfolio_daily_trade_cap", 2)
    if daily_trade_cap is not None and trades_started >= int(daily_trade_cap):
        return False, f"daily_trade_cap:{trades_started}"
    if daily_outcome_r <= -2:
        return False, f"daily_loss_limit:{daily_outcome_r:.4f}"
    if cooldown_until is not None and request.timestamp_utc < cooldown_until:
        return False, f"loss_cooldown_until:{cooldown_until.isoformat()}"
    return True, "portfolio_daily_risk_available"


def _apply_weighted_portfolio_allocation(
    response: DecisionResponse,
    request: DecisionRequest,
    settings: Settings,
    *,
    profile: str,
    component: str,
    weight: float,
) -> DecisionResponse:
    suggested_size = _position_size(
        request.account.balance,
        settings.risk_per_trade_pct * weight,
        float(response.stop_loss_points or 0),
        request.value_per_point_per_lot,
    )
    response.action = DecisionAction.HOLD
    response.reason = f"{profile}_shadow_signal"
    response.size = 0.0
    response.risk_amount = round(request.account.balance * settings.risk_per_trade_pct * weight / 100, 2)
    risk_state = getattr(settings, "portfolio_risk_state", {})
    response.metadata.update(
        {
            "strategy_profile": profile,
            "portfolio_component": component,
            "portfolio_weight_pct": round(weight * 100, 2),
            "risk_multiplier": weight,
            "effective_risk_pct": round(settings.risk_per_trade_pct * weight, 5),
            "suggested_size": round(suggested_size, 2),
            "portfolio_priority": "bot-7_then_bot-6",
            "portfolio_shared_position_limit": 1,
            "portfolio_shared_daily_trade_limit": 2,
            "portfolio_shared_daily_loss_limit_r": 2,
            "portfolio_risk_state_json": json.dumps(risk_state, sort_keys=True, separators=(",", ":")),
            "experimental_shadow_lock": True,
            "research_status": "parallel_forward_validation_required",
        }
    )
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    exposure_available = not same_symbol_positions
    daily_risk_available, daily_risk_reason = _portfolio_runtime_risk_check(request, settings)
    response.risk_checks.extend(
        [
            RiskCheck(name="portfolio_shared_exposure", passed=exposure_available, detail="max_positions_for_symbol=1"),
            RiskCheck(name="portfolio_daily_risk_state", passed=daily_risk_available, detail=daily_risk_reason),
            RiskCheck(name="portfolio_component_allocation", passed=True, detail=f"{component}={weight:.2f}"),
            RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
        ]
    )
    if not exposure_available or not daily_risk_available:
        response.reason = "weighted_portfolio_risk_block"
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
        response.metadata["portfolio_risk_block_reason"] = "shared_exposure" if not exposure_available else daily_risk_reason
    return response


def _evaluate_weighted_portfolio_6_7(request: DecisionRequest, settings: Settings, profile: str) -> DecisionResponse:
    weights = WEIGHTED_PORTFOLIO_PROFILES[profile]
    bot_7_request = request.model_copy(update={"candles": request.candles[-1000:]})
    bot_6_request = request.model_copy(update={"candles": request.candles[-60:]})
    bot_7 = _evaluate_multitimeframe_trend_pullback(bot_7_request, settings, profile)
    bot_6 = _evaluate_stochastic_sell_session(bot_6_request, settings, profile)
    bot_7_candidate = bool(bot_7.metadata.get("candidate"))
    bot_6_candidate = bool(bot_6.metadata.get("candidate"))

    if bot_7_candidate:
        bot_7.metadata["portfolio_conflict_detected"] = bot_6_candidate
        return _apply_weighted_portfolio_allocation(
            bot_7, request, settings, profile=profile, component="bot-7", weight=weights["bot-7"]
        )
    if bot_6_candidate:
        regime = _portfolio_regime(request, settings)
        regime_allowed = regime.name == "trend" and regime.direction == "sell"
        regime_reason = "trend_sell_confirmed" if regime_allowed else f"trend_sell_required:{regime.name}:{regime.direction}"
        bot_6.metadata.update(
            {
                "portfolio_regime": regime.name,
                "portfolio_regime_direction": regime.direction,
                "portfolio_regime_adx": regime.adx,
                "portfolio_regime_atr_percentile": regime.atr_percentile,
                "portfolio_regime_reason": regime_reason,
            }
        )
        bot_6.risk_checks.append(RiskCheck(name="portfolio_bot_6_sell_regime", passed=regime_allowed, detail=regime_reason))
        if not regime_allowed:
            bot_6.action = DecisionAction.HOLD
            bot_6.reason = "weighted_portfolio_bot_6_regime_block"
            bot_6.size = 0.0
            bot_6.metadata.update(
                {
                    "strategy_profile": profile,
                    "portfolio_component": "bot-6",
                    "base_candidate": True,
                    "candidate": False,
                    "experimental_shadow_lock": True,
                }
            )
            return bot_6
        return _apply_weighted_portfolio_allocation(
            bot_6, request, settings, profile=profile, component="bot-6", weight=weights["bot-6"]
        )

    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="weighted_portfolio_components_not_confirmed",
        risk_checks=[
            RiskCheck(name="bot_7_component", passed=False, detail=bot_7.reason),
            RiskCheck(name="bot_6_component", passed=False, detail=bot_6.reason),
            RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
        ],
        metadata={
            "strategy_profile": profile,
            "candidate": False,
            "portfolio_priority": "bot-7_then_bot-6",
            "portfolio_bot_7_reason": bot_7.reason,
            "portfolio_bot_6_reason": bot_6.reason,
            "portfolio_shared_position_limit": 1,
            "portfolio_shared_daily_trade_limit": 2,
            "portfolio_shared_daily_loss_limit_r": 2,
            "experimental_shadow_lock": True,
        },
    )


def _evaluate_happy_gold_combined_portfolio(request: DecisionRequest, settings: Settings) -> DecisionResponse:
    """Coordinate Bot 13 and the 100/100 portfolio under one shared risk budget."""
    profile = HAPPY_GOLD_COMBINED_PROFILE
    combined_settings = copy(settings)
    combined_settings.portfolio_daily_trade_cap = None
    weighted = _evaluate_weighted_portfolio_6_7(request, combined_settings, "portfolio_6_7_100_100")
    zigzag = _evaluate_happy_gold_zigzag_regime_confirmed(request, combined_settings, profile)
    if zigzag.metadata.get("candidate"):
        zigzag = _apply_weighted_portfolio_allocation(
            zigzag,
            request,
            combined_settings,
            profile="portfolio_6_7_100_100",
            component="bot-13",
            weight=1.0,
        )

    weighted_candidate = bool(weighted.metadata.get("candidate"))
    zigzag_candidate = bool(zigzag.metadata.get("candidate"))
    weighted_side = str(weighted.metadata.get("suggested_action", ""))
    zigzag_side = str(zigzag.metadata.get("suggested_action", ""))
    if weighted_candidate and zigzag_candidate and weighted_side != zigzag_side:
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_portfolio_signal_conflict",
            risk_checks=[
                RiskCheck(name="combined_signal_agreement", passed=False, detail=f"portfolio={weighted_side};zigzag={zigzag_side}"),
                RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
            ],
            metadata={
                "strategy_profile": profile,
                "candidate": False,
                "combined_portfolio_candidate": True,
                "combined_zigzag_candidate": True,
                "combined_priority": "zigzag_confirmed_then_portfolio_7_6",
                "experimental_shadow_lock": True,
            },
        )

    selected = zigzag if zigzag_candidate else weighted if weighted_candidate else None
    if selected is None:
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="happy_gold_portfolio_components_not_confirmed",
            risk_checks=[
                RiskCheck(name="portfolio_7_6_component", passed=False, detail=weighted.reason),
                RiskCheck(name="zigzag_regime_component", passed=False, detail=zigzag.reason),
                RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
            ],
            metadata={
                "strategy_profile": profile,
                "candidate": False,
                "combined_priority": "zigzag_confirmed_then_portfolio_7_6",
                "combined_portfolio_reason": weighted.reason,
                "combined_zigzag_reason": zigzag.reason,
                "experimental_shadow_lock": True,
            },
        )

    component = "bot-13" if zigzag_candidate else str(weighted.metadata.get("portfolio_component", "portfolio-7-6"))
    session_start = time.fromisoformat(str(getattr(settings, "happy_gold_combined_session_start_utc", "06:00")))
    session_end = time.fromisoformat(str(getattr(settings, "happy_gold_combined_session_end_utc", "19:00")))
    session_time = request.timestamp_utc.astimezone(timezone.utc).time()
    if not session_start <= session_time < session_end:
        selected.reason = "happy_gold_portfolio_session_block"
        selected.metadata.update({"strategy_profile": profile, "base_candidate": True, "candidate": False})
        selected.risk_checks.append(RiskCheck(name="combined_session", passed=False, detail=f"{session_start.isoformat(timespec='minutes')}-{session_end.isoformat(timespec='minutes')}_UTC"))
        return selected
    selected.reason = "happy_gold_portfolio_100_100_shadow_signal"
    selected.metadata.update(
        {
            "strategy_profile": profile,
            "combined_component": component,
            "combined_priority": "zigzag_confirmed_then_portfolio_7_6",
            "combined_portfolio_candidate": weighted_candidate,
            "combined_zigzag_candidate": zigzag_candidate,
            "combined_shared_risk_pct": settings.risk_per_trade_pct,
            "combined_shared_position_limit": 1,
            "combined_daily_trade_cap": "unlimited",
            "combined_session_utc": f"{session_start.isoformat(timespec='minutes')}-{session_end.isoformat(timespec='minutes')}",
            "experimental_shadow_lock": True,
            "research_status": "combined_portfolio_requires_independent_walk_forward_validation",
        }
    )
    return selected


def _adaptive_regime(request: DecisionRequest, settings: Settings) -> RegimeSnapshot:
    override = getattr(settings, "adaptive_regime_snapshot", None)
    if isinstance(override, RegimeSnapshot):
        return override
    return classify_regime(request.candles, settings)


def _adaptive_component_response(
    request: DecisionRequest,
    settings: Settings,
    component: str,
) -> tuple[DecisionResponse, RegimeSnapshot, bool, str]:
    # The external H1 regime needs roughly 4,000 M5 candles, while the component
    # setups need at most 1,000. Keeping the full window out of their indicator
    # loops makes the adaptive replay and polling materially cheaper.
    component_history_bars = 1000 if component == "bot-7" else 60
    component_request = request.model_copy(update={"candles": request.candles[-component_history_bars:]})
    if component == "bot-7":
        response = _evaluate_multitimeframe_trend_pullback(component_request, settings, "adaptive_portfolio_3_6_7")
        regime = _adaptive_regime(request, settings) if response.metadata.get("candidate") else RegimeSnapshot("not_evaluated", "none", "setup_not_confirmed")
        return response, regime, True, "bot_7_internal_h1_m15_regime"
    if component == "bot-6":
        response = _evaluate_stochastic_sell_session(component_request, settings, "adaptive_portfolio_3_6_7")
        if not response.metadata.get("candidate"):
            return response, RegimeSnapshot("not_evaluated", "none", "setup_not_confirmed"), False, "setup_not_confirmed"
        regime = _adaptive_regime(request, settings)
        allowed = regime.name == "trend" and regime.direction == "sell"
        return response, regime, allowed, "trend_sell_confirmed" if allowed else f"trend_sell_required:{regime.name}:{regime.direction}"
    response = _evaluate_rsi_bollinger(component_request, settings, "adaptive_portfolio_3_6_7")
    if not response.metadata.get("candidate"):
        return response, RegimeSnapshot("not_evaluated", "none", "setup_not_confirmed"), False, "setup_not_confirmed"
    regime = _adaptive_regime(request, settings)
    allowed = regime.name == "range" and regime.atr_percentile is not None and regime.atr_percentile >= 25
    return response, regime, allowed, "range_atr_25_confirmed" if allowed else f"range_atr_25_required:{regime.name}:{regime.atr_percentile}"


def _evaluate_adaptive_portfolio_3_6_7(request: DecisionRequest, settings: Settings) -> DecisionResponse:
    health = getattr(settings, "adaptive_component_health", {})
    active_component = getattr(settings, "adaptive_active_component", None) or select_adaptive_component(health)
    common_metadata = {
        "strategy_profile": "adaptive_portfolio_3_6_7",
        "adaptive_window_days": ADAPTIVE_WINDOW_DAYS,
        "adaptive_min_closed_trades": ADAPTIVE_MIN_CLOSED_TRADES,
        "adaptive_min_profit_factor": ADAPTIVE_MIN_PROFIT_FACTOR,
        "adaptive_component_health_json": json.dumps(health, sort_keys=True, separators=(",", ":")),
        "adaptive_active_component": active_component or "none",
        "adaptive_selection_frozen_utc_day": request.timestamp_utc.astimezone(timezone.utc).date().isoformat(),
        "portfolio_shared_position_limit": 1,
        "portfolio_shared_daily_loss_limit_r": 2,
        "experimental_shadow_lock": True,
    }
    if active_component not in {"bot-3", "bot-6", "bot-7"}:
        return DecisionResponse(
            action=DecisionAction.HOLD,
            reason="adaptive_health_gate_not_confirmed",
            risk_checks=[
                RiskCheck(name="adaptive_health_gate", passed=False, detail="no_component_passed_120d_expectancy_and_profit_factor"),
                RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
            ],
            metadata={**common_metadata, "candidate": False},
        )

    response, regime, regime_allowed, regime_reason = _adaptive_component_response(request, settings, active_component)
    response.metadata.update(common_metadata)
    response.metadata.update(
        {
            "portfolio_component": active_component,
            "portfolio_weight_pct": 100.0,
            "risk_multiplier": 1.0,
            "effective_risk_pct": round(settings.risk_per_trade_pct, 5),
            "adaptive_regime": regime.name,
            "adaptive_regime_direction": regime.direction,
            "adaptive_regime_adx": regime.adx,
            "adaptive_regime_atr_percentile": regime.atr_percentile,
            "adaptive_regime_reason": regime_reason,
            "research_status": "integrated_replay_and_forward_validation_required",
        }
    )
    response.risk_checks.append(RiskCheck(name="adaptive_component_regime", passed=regime_allowed, detail=regime_reason))
    if not response.metadata.get("candidate"):
        return response
    if not regime_allowed:
        response.action = DecisionAction.HOLD
        response.reason = "adaptive_component_regime_block"
        response.size = 0.0
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
        return response

    suggested_size = _position_size(
        request.account.balance,
        settings.risk_per_trade_pct,
        float(response.stop_loss_points or 0),
        request.value_per_point_per_lot,
    )
    response.action = DecisionAction.HOLD
    response.reason = "adaptive_portfolio_3_6_7_shadow_signal"
    response.size = 0.0
    response.risk_amount = round(request.account.balance * settings.risk_per_trade_pct / 100, 2)
    response.metadata["suggested_size"] = round(suggested_size, 2)
    same_symbol_positions = [position for position in request.positions if position.symbol.upper() == request.symbol.upper()]
    exposure_available = not same_symbol_positions
    response.risk_checks.extend(
        [
            RiskCheck(name="portfolio_shared_exposure", passed=exposure_available, detail="max_positions_for_symbol=1"),
            RiskCheck(name="experimental_shadow_lock", passed=True, detail="execution_permanently_disabled"),
        ]
    )
    if not exposure_available:
        response.reason = "adaptive_portfolio_shared_exposure_block"
        response.metadata["base_candidate"] = True
        response.metadata["candidate"] = False
    return response


def evaluate_decision(request: DecisionRequest, settings: Settings, strategy_profile: str | None = None) -> DecisionResponse:
    profile = strategy_profile or settings.strategy_profile
    if profile == "stochastic_sell_session":
        return _evaluate_stochastic_sell_session(request, settings, profile)
    if profile == "multitimeframe_trend_pullback":
        return _evaluate_multitimeframe_trend_pullback(request, settings, profile)
    if profile == "happy_gold_zigzag_scalper":
        return _evaluate_happy_gold_zigzag_scalper(request, settings, profile)
    if profile == "happy_gold_zigzag_regime_confirmed":
        return _evaluate_happy_gold_zigzag_regime_confirmed(request, settings, profile)
    if profile in WEIGHTED_PORTFOLIO_PROFILES:
        return _evaluate_weighted_portfolio_6_7(request, settings, profile)
    if profile == HAPPY_GOLD_COMBINED_PROFILE:
        return _evaluate_happy_gold_combined_portfolio(request, settings)
    if profile == HAPPY_GOLD_TICK_IMPULSE_PROFILE:
        return _evaluate_happy_gold_tick_impulse(request, settings)
    if profile == HAPPY_GOLD_ZIGZAG_TICK_CONTEXT_PROFILE:
        return _evaluate_happy_gold_zigzag_tick_context(request, settings)
    if profile == HAPPY_GOLD_ZIGZAG_STRUCTURAL_TICK_PROFILE:
        return _evaluate_happy_gold_zigzag_structural_tick(request, settings)
    if profile == HAPPY_GOLD_ZIGZAG_NORMALIZED_RANGE_PROFILE:
        return _evaluate_happy_gold_zigzag_structural_tick(
            request,
            settings,
            strategy_profile=profile,
            normalized_range_max=float(getattr(settings, "happy_gold_normalized_range_max", 1.876955192)),
        )
    if profile == HAPPY_GOLD_MULTIWINDOW_TICK_SCORE_PROFILE:
        return _evaluate_happy_gold_multiwindow_tick_score(request, settings)
    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="unsupported_strategy_profile",
        risk_checks=[RiskCheck(name="strategy_profile", passed=False, detail=profile)],
        metadata={"strategy_profile": profile},
    )


def reset_strategy_state() -> None:
    """Clear per-process flow windows for deterministic tests."""
    with _flow_lock:
        _flow_windows.clear()
