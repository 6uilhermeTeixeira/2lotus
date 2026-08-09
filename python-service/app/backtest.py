from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

from .config import Settings
from .regime import RegimeSnapshot, build_regime_timeline, regime_allows, regime_at
from .results_store import build_results_snapshot
from .schemas import AccountSnapshot, BotCatalogItem, CandleSnapshot, DecisionRequest, HistoricalReplayResponse, ReplayValidationFold, TradeResult
from .strategy import HAPPY_GOLD_COMBINED_PROFILE, WEIGHTED_PORTFOLIO_PROFILES, evaluate_decision


HISTORY_BARS = 60
MULTITIMEFRAME_HISTORY_BARS = 1000
ADAPTIVE_HISTORY_BARS = 4000
STRATEGY_VERSION = "2026-08-04-shadow-core-v1"
DEEP_HISTORY_PROFILES = {"multitimeframe_trend_pullback", HAPPY_GOLD_COMBINED_PROFILE, *WEIGHTED_PORTFOLIO_PROFILES}
HAPPY_GOLD_PROFILES = {"happy_gold_zigzag_scalper", "happy_gold_zigzag_regime_confirmed"}


def replay_settings_fingerprint(settings: Settings, bot: BotCatalogItem) -> str:
    values = {
        "strategy_version": STRATEGY_VERSION,
        "profile": bot.strategy_profile,
        "risk": settings.backtest_risk_per_trade_pct,
        "spread": settings.backtest_spread_points,
        "slippage": settings.backtest_slippage_points,
        "commission": settings.backtest_commission_per_lot,
        "session": [settings.backtest_session_start_utc, settings.backtest_session_end_utc],
        "daily_loss_r": settings.backtest_daily_loss_limit_r,
        "cooldown": settings.backtest_cooldown_bars_after_loss,
        "regime": settings.backtest_regime_enabled,
    }
    return sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _instrument_point_size(bot: BotCatalogItem, settings: Settings) -> float:
    return 0.00001 if bot.symbol.upper() == "EURUSD" else settings.backtest_point_size


@dataclass
class _OpenTrade:
    trade_id: str
    setup: str
    symbol: str
    timeframe: str
    side: str
    opened_at_utc: datetime
    volume_lots: float
    entry_price: float
    initial_stop_price: float
    stop_price: float
    partial_price: float
    target_price: float
    risk_amount: float
    point_size: float = 0.01
    value_per_point_per_lot: float = 1.0
    partial_close_pct: float = 50.0
    remaining_fraction: float = 1.0
    realized_gross_pnl: float = 0.0
    partial_hit: bool = False
    regime: str = "unknown"
    bars_held: int = 0
    max_holding_bars: int | None = None
    break_even_trigger_points: float | None = None
    break_even_offset_points: float = 0.0
    trailing_activation_points: float | None = None
    trailing_distance_points: float | None = None
    best_favorable_price: float | None = None


def _timeframe_delta(timeframe: str) -> timedelta:
    normalized = timeframe.upper()
    if normalized.startswith("M") and normalized[1:].isdigit():
        return timedelta(minutes=int(normalized[1:]))
    if normalized.startswith("H") and normalized[1:].isdigit():
        return timedelta(hours=int(normalized[1:]))
    raise ValueError(f"unsupported replay timeframe: {timeframe}")


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _replay_settings(settings: Settings, bot: BotCatalogItem) -> SimpleNamespace:
    return SimpleNamespace(
        default_symbol=bot.symbol,
        default_timeframe=bot.timeframe,
        max_spread_points=settings.max_spread_points,
        risk_per_trade_pct=settings.backtest_risk_per_trade_pct,
        strategy_profile=bot.strategy_profile,
        fimath_flow_window_seconds=settings.fimath_flow_window_seconds,
        fimath_flow_min_samples=settings.fimath_flow_min_samples,
        fimath_flow_z_threshold=settings.fimath_flow_z_threshold,
        fimath_flow_policy=settings.fimath_flow_policy,
        fimath_mode="shadow",
        enable_live_decisions=False,
        fimath_partial_pct=settings.fimath_partial_pct,
        backtest_regime_trend_adx=getattr(settings, "backtest_regime_trend_adx", 25),
        backtest_regime_range_adx=getattr(settings, "backtest_regime_range_adx", 20),
        backtest_regime_high_volatility_percentile=getattr(settings, "backtest_regime_high_volatility_percentile", 90),
        portfolio_regime_snapshot=None,
        portfolio_risk_state={},
        happy_gold_regime_snapshot=None,
        happy_gold_break_even_trigger_points=getattr(settings, "happy_gold_break_even_trigger_points", 40),
        happy_gold_break_even_offset_points=getattr(settings, "happy_gold_break_even_offset_points", 0),
        happy_gold_trailing_activation_points=getattr(settings, "happy_gold_trailing_activation_points", 120),
        happy_gold_trailing_distance_points=getattr(settings, "happy_gold_trailing_distance_points", 60),
        happy_gold_max_holding_bars=getattr(settings, "happy_gold_max_holding_bars", 2),
        happy_gold_combined_session_start_utc=getattr(settings, "happy_gold_combined_session_start_utc", "06:00"),
        happy_gold_combined_session_end_utc=getattr(settings, "happy_gold_combined_session_end_utc", "19:00"),
    )


def _pnl_for_fill(trade: _OpenTrade, exit_price: float, fraction: float) -> float:
    direction = 1 if trade.side == "buy" else -1
    price_points = direction * (exit_price - trade.entry_price) / trade.point_size
    return price_points * trade.value_per_point_per_lot * trade.volume_lots * fraction


def _exit_fill(price: float, side: str, slippage_price: float) -> float:
    return price - slippage_price if side == "buy" else price + slippage_price


def _finish_trade(
    trade: _OpenTrade,
    timestamp_utc: datetime,
    exit_price: float,
    settings: Settings,
    reason: str,
) -> TradeResult:
    gross_pnl = trade.realized_gross_pnl + _pnl_for_fill(trade, exit_price, trade.remaining_fraction)
    fees = settings.backtest_commission_per_lot * trade.volume_lots
    net_pnl = gross_pnl - fees
    outcome_r = net_pnl / trade.risk_amount if trade.risk_amount else 0.0
    return TradeResult(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        timeframe=trade.timeframe,
        setup=f"{trade.setup}_historical_replay",
        side=trade.side,
        opened_at_utc=trade.opened_at_utc,
        closed_at_utc=timestamp_utc,
        volume_lots=trade.volume_lots,
        entry_price=trade.entry_price,
        exit_price=exit_price,
        stop_loss_price=trade.initial_stop_price,
        take_profit_price=trade.target_price,
        risk_amount=trade.risk_amount,
        gross_pnl=round(gross_pnl, 2),
        fees=round(fees, 2),
        net_pnl=round(net_pnl, 2),
        rr_realized=round(outcome_r, 4),
        note=f"Replay histórico OHLC; saída={reason}; regime={trade.regime}; nenhuma ordem foi enviada.",
    )


def _advance_trade(trade: _OpenTrade, candle: CandleSnapshot, settings: Settings) -> TradeResult | None:
    trade.bars_held += 1
    spread_price = settings.backtest_spread_points * trade.point_size
    slippage_price = settings.backtest_slippage_points * trade.point_size
    buy = trade.side == "buy"
    quote_high = candle.high if buy else candle.high + spread_price
    quote_low = candle.low if buy else candle.low + spread_price
    stopped = quote_low <= trade.stop_price if buy else quote_high >= trade.stop_price
    if stopped:
        return _finish_trade(trade, candle.timestamp_utc, _exit_fill(trade.stop_price, trade.side, slippage_price), settings, "stop")
    partial_reached = quote_high >= trade.partial_price if buy else quote_low <= trade.partial_price
    if not trade.partial_hit and partial_reached:
        fraction = trade.partial_close_pct / 100
        partial_fill = _exit_fill(trade.partial_price, trade.side, slippage_price)
        if fraction >= 1:
            return _finish_trade(trade, candle.timestamp_utc, partial_fill, settings, "target")
        trade.realized_gross_pnl += _pnl_for_fill(trade, partial_fill, fraction)
        trade.remaining_fraction -= fraction
        trade.partial_hit = True
        trade.stop_price = trade.entry_price
        return None
    target_reached = quote_high >= trade.target_price if buy else quote_low <= trade.target_price
    if trade.partial_hit and target_reached:
        return _finish_trade(trade, candle.timestamp_utc, _exit_fill(trade.target_price, trade.side, slippage_price), settings, "target")
    if trade.break_even_trigger_points is not None or trade.trailing_activation_points is not None:
        favorable_price = quote_high if buy else quote_low
        if trade.best_favorable_price is None:
            trade.best_favorable_price = favorable_price
        elif buy:
            trade.best_favorable_price = max(trade.best_favorable_price, favorable_price)
        else:
            trade.best_favorable_price = min(trade.best_favorable_price, favorable_price)
        direction = 1 if buy else -1
        favorable_points = direction * (trade.best_favorable_price - trade.entry_price) / trade.point_size
        next_stop = trade.stop_price
        if trade.break_even_trigger_points is not None and favorable_points >= trade.break_even_trigger_points:
            break_even_stop = trade.entry_price + direction * trade.break_even_offset_points * trade.point_size
            next_stop = max(next_stop, break_even_stop) if buy else min(next_stop, break_even_stop)
        if (
            trade.trailing_activation_points is not None
            and trade.trailing_distance_points is not None
            and favorable_points >= trade.trailing_activation_points
        ):
            trailing_stop = trade.best_favorable_price - direction * trade.trailing_distance_points * trade.point_size
            next_stop = max(next_stop, trailing_stop) if buy else min(next_stop, trailing_stop)
        trade.stop_price = next_stop
    if trade.max_holding_bars is not None and trade.bars_held >= trade.max_holding_bars:
        return _force_close(trade, candle, settings, "max_holding_bars")
    return None


def _force_close(trade: _OpenTrade, candle: CandleSnapshot, settings: Settings, reason: str) -> TradeResult:
    spread_price = settings.backtest_spread_points * trade.point_size
    slippage_price = settings.backtest_slippage_points * trade.point_size
    quote = candle.close if trade.side == "buy" else candle.close + spread_price
    return _finish_trade(trade, candle.timestamp_utc + _timeframe_delta(trade.timeframe), _exit_fill(quote, trade.side, slippage_price), settings, reason)


def _risk_diagnostics(trades: list[TradeResult]) -> tuple[float, float, int, float]:
    closed = sorted((trade for trade in trades if trade.closed_at_utc is not None), key=lambda trade: trade.closed_at_utc)
    outcomes = [float(trade.rr_realized or 0) for trade in closed]
    expectancy = sum(outcomes) / len(outcomes) if outcomes else 0.0
    equity = peak = max_drawdown = 0.0
    loss_streak = max_loss_streak = 0
    for outcome in outcomes:
        equity += outcome
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        loss_streak = loss_streak + 1 if outcome < 0 else 0
        max_loss_streak = max(max_loss_streak, loss_streak)
    return round(expectancy, 4), round(max_drawdown, 2), max_loss_streak, round(sum(trade.fees for trade in closed), 2)


def _validation_folds(trades: list[TradeResult], start: datetime, end: datetime, fold_days: int) -> list[ReplayValidationFold]:
    folds: list[ReplayValidationFold] = []
    cursor = start
    while cursor < end:
        fold_end = min(cursor + timedelta(days=fold_days), end)
        rows = [trade for trade in trades if trade.closed_at_utc is not None and cursor <= trade.opened_at_utc < fold_end]
        snapshot = build_results_snapshot([trade.model_dump(mode="json") for trade in rows], "rolling-validation")
        outcomes = [float(trade.rr_realized or 0) for trade in rows]
        expectancy = sum(outcomes) / len(outcomes) if outcomes else 0.0
        folds.append(
            ReplayValidationFold(
                period_start_utc=cursor,
                period_end_utc=fold_end,
                trades_count=len(rows),
                expectancy_r=round(expectancy, 4),
                profit_factor=snapshot.summary.profit_factor,
                net_pnl=snapshot.summary.net_pnl,
                positive=snapshot.summary.net_pnl > 0,
            )
        )
        cursor = fold_end
    return folds


def run_historical_replay(
    bot: BotCatalogItem,
    candles: list[CandleSnapshot],
    settings: Settings,
    *,
    days: int = 30,
) -> HistoricalReplayResponse:
    if not candles:
        raise ValueError("historical replay requires candles")
    ordered = sorted(candles, key=lambda candle: candle.timestamp_utc)
    interval = _timeframe_delta(bot.timeframe)
    period_end = ordered[-1].timestamp_utc + interval
    requested_start = period_end - timedelta(days=days)
    first_visible = next((index for index, candle in enumerate(ordered) if candle.timestamp_utc >= requested_start), len(ordered) - 1)
    history_bars = (
        ADAPTIVE_HISTORY_BARS if bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE or bot.strategy_profile in WEIGHTED_PORTFOLIO_PROFILES
        else MULTITIMEFRAME_HISTORY_BARS if bot.strategy_profile in DEEP_HISTORY_PROFILES
        else HISTORY_BARS
    )
    evaluation_start = max(history_bars - 1, first_visible)
    replay_settings = _replay_settings(settings, bot)
    regime_timeline = build_regime_timeline(ordered, settings) if settings.backtest_regime_enabled else ([], [])
    session_start = _parse_time(settings.backtest_session_start_utc)
    session_end = _parse_time(settings.backtest_session_end_utc)
    if bot.strategy_profile in HAPPY_GOLD_PROFILES:
        session_start, session_end = time(3, 0), time(19, 0)
    if bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE:
        session_start = _parse_time(replay_settings.happy_gold_combined_session_start_utc)
        session_end = _parse_time(replay_settings.happy_gold_combined_session_end_utc)
    point_size = _instrument_point_size(bot, settings)
    balance = settings.backtest_balance
    open_trade: _OpenTrade | None = None
    previous_candle: CandleSnapshot | None = None
    trades: list[TradeResult] = []
    candidates = eligible = forced_session_closes = 0
    regime_counts: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    trades_per_day: defaultdict[object, int] = defaultdict(int)
    daily_outcome_r: defaultdict[object, float] = defaultdict(float)
    cooldown_until_index = -1

    for index in range(evaluation_start, len(ordered)):
        candle = ordered[index]
        if open_trade is not None and previous_candle is not None and candle.timestamp_utc.date() != previous_candle.timestamp_utc.date():
            closed = _force_close(open_trade, previous_candle, settings, "session_gap")
            trades.append(closed)
            balance += closed.net_pnl
            daily_outcome_r[closed.opened_at_utc.date()] += float(closed.rr_realized or 0)
            if (closed.rr_realized or 0) < 0:
                cooldown_until_index = index + settings.backtest_cooldown_bars_after_loss
            open_trade = None
            forced_session_closes += 1

        if open_trade is not None:
            closed = _advance_trade(open_trade, candle, settings)
            if closed is not None:
                trades.append(closed)
                balance += closed.net_pnl
                daily_outcome_r[closed.opened_at_utc.date()] += float(closed.rr_realized or 0)
                if (closed.rr_realized or 0) < 0:
                    cooldown_until_index = index + settings.backtest_cooldown_bars_after_loss
                open_trade = None

        candle_end = candle.timestamp_utc + interval
        if open_trade is not None and candle_end.time() >= session_end:
            closed = _force_close(open_trade, candle, settings, "session_end")
            trades.append(closed)
            balance += closed.net_pnl
            daily_outcome_r[closed.opened_at_utc.date()] += float(closed.rr_realized or 0)
            if (closed.rr_realized or 0) < 0:
                cooldown_until_index = index + settings.backtest_cooldown_bars_after_loss
            open_trade = None
            forced_session_closes += 1

        history = ordered[max(0, index - history_bars + 1):index + 1]
        request = DecisionRequest(
            bot_id=f"replay-{bot.id}",
            request_id=f"replay-{bot.id}-{int(candle.timestamp_utc.timestamp())}",
            timestamp_utc=candle_end,
            symbol=bot.symbol,
            timeframe=bot.timeframe,
            bid=candle.close,
            ask=candle.close + settings.backtest_spread_points * point_size,
            spread_points=settings.backtest_spread_points,
            point_size=point_size,
            value_per_point_per_lot=settings.backtest_value_per_point_per_lot,
            candles=history,
            positions=[],
            account=AccountSnapshot(balance=balance, equity=balance, margin_free=balance),
        )
        if (bot.strategy_profile in WEIGHTED_PORTFOLIO_PROFILES or bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE) and settings.backtest_regime_enabled:
            replay_settings.portfolio_regime_snapshot = regime_at(regime_timeline, candle_end)
        if bot.strategy_profile in {"happy_gold_zigzag_regime_confirmed", HAPPY_GOLD_COMBINED_PROFILE} and settings.backtest_regime_enabled:
            replay_settings.happy_gold_regime_snapshot = regime_at(regime_timeline, candle_end)
        response = evaluate_decision(request, replay_settings, bot.strategy_profile)
        if not response.metadata.get("candidate"):
            previous_candle = candle
            continue
        candidates += 1
        side = str(response.metadata["suggested_action"])
        regime: RegimeSnapshot = regime_at(regime_timeline, candle_end) if settings.backtest_regime_enabled else RegimeSnapshot("disabled", side, "regime_filter_disabled")
        regime_counts[regime.name] += 1
        portfolio_component = str(response.metadata.get("portfolio_component", ""))
        if bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE or bot.strategy_profile in WEIGHTED_PORTFOLIO_PROFILES:
            regime_allowed, regime_reason = True, "strategy_uses_internal_adaptive_component_regime"
        elif bot.strategy_profile in HAPPY_GOLD_PROFILES:
            regime_allowed, regime_reason = True, "strategy_uses_internal_m30_breakout_regime"
        elif bot.strategy_profile == "multitimeframe_trend_pullback" or portfolio_component == "bot-7":
            regime_allowed, regime_reason = True, "strategy_uses_internal_h1_m15_regime"
        elif bot.strategy_profile == "portfolio_6_7" and portfolio_component == "bot-6":
            regime_allowed, regime_reason = regime_allows("stochastic_sell_session", side, regime) if settings.backtest_regime_enabled else (True, "regime_filter_disabled")
        else:
            regime_allowed, regime_reason = regime_allows(bot.strategy_profile, side, regime) if settings.backtest_regime_enabled else (True, "regime_filter_disabled")
        if not regime_allowed:
            blocked_reasons[regime_reason] += 1
            previous_candle = candle
            continue
        day = candle_end.date()
        session_allowed = session_start <= candle_end.time() < session_end
        daily_trade_cap = min(settings.backtest_max_trades_per_day, 2) if bot.strategy_profile in DEEP_HISTORY_PROFILES else settings.backtest_max_trades_per_day
        trade_cap_allowed = True if bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE else trades_per_day[day] < daily_trade_cap
        risk_allowed = trade_cap_allowed and daily_outcome_r[day] > -settings.backtest_daily_loss_limit_r
        cooldown_allowed = index > cooldown_until_index
        if not session_allowed:
            blocked_reasons["outside_liquid_session"] += 1
        elif not risk_allowed:
            blocked_reasons["daily_risk_limit"] += 1
        elif not cooldown_allowed:
            blocked_reasons["loss_cooldown"] += 1
        elif open_trade is not None:
            blocked_reasons["position_already_open"] += 1
        elif any(value is None for value in (response.entry_price, response.stop_loss_price, response.partial_take_profit_price, response.final_take_profit_price)):
            blocked_reasons["candidate_missing_prices"] += 1
        else:
            eligible += 1
            slippage_price = settings.backtest_slippage_points * point_size
            raw_entry = float(response.entry_price)
            entry_fill = raw_entry + slippage_price if side == "buy" else raw_entry - slippage_price
            open_trade = _OpenTrade(
                trade_id=f"replay:{bot.id}:{candle.timestamp_utc.isoformat()}:{side}",
                setup=str(response.metadata.get("setup_type", bot.strategy_profile)),
                symbol=bot.symbol,
                timeframe=bot.timeframe,
                side=side,
                opened_at_utc=candle_end,
                volume_lots=float(response.metadata.get("suggested_size", 0)),
                entry_price=entry_fill,
                initial_stop_price=float(response.stop_loss_price),
                stop_price=float(response.stop_loss_price),
                partial_price=float(response.partial_take_profit_price),
                target_price=float(response.final_take_profit_price),
                risk_amount=float(response.risk_amount),
                point_size=point_size,
                value_per_point_per_lot=settings.backtest_value_per_point_per_lot,
                partial_close_pct=settings.fimath_partial_pct,
                max_holding_bars=int(response.metadata["max_holding_bars"]) if "max_holding_bars" in response.metadata else None,
                break_even_trigger_points=float(response.metadata["break_even_trigger_points"]) if "break_even_trigger_points" in response.metadata else None,
                break_even_offset_points=float(response.metadata.get("break_even_offset_points", 0)),
                trailing_activation_points=float(response.metadata["trailing_activation_points"]) if "trailing_activation_points" in response.metadata else None,
                trailing_distance_points=float(response.metadata["trailing_distance_points"]) if "trailing_distance_points" in response.metadata else None,
                regime=(f"{regime.name}:{regime.direction}:adx={regime.adx:.2f}:atr_pct={regime.atr_percentile:.1f}" if regime.adx is not None and regime.atr_percentile is not None else f"{regime.name}:{regime.direction}"),
            )
            open_trade.partial_close_pct = response.partial_close_pct
            trades_per_day[day] += 1
        previous_candle = candle

    if open_trade is not None:
        closed = _force_close(open_trade, ordered[-1], settings, "data_end")
        trades.append(closed)
        forced_session_closes += 1

    visible_candles = ordered[first_visible:]
    period_start = visible_candles[0].timestamp_utc
    snapshot = build_results_snapshot([trade.model_dump(mode="json") for trade in trades], f"historical-replay:{bot.id}:{days}d")
    expectancy, max_drawdown, max_loss_streak, total_costs = _risk_diagnostics(trades)
    folds = _validation_folds(trades, period_start, period_end, settings.backtest_validation_fold_days)
    coverage_days = (period_end - period_start).total_seconds() / 86400
    populated_folds = [fold for fold in folds if fold.trades_count > 0]
    positive_fold_ratio = sum(fold.positive for fold in populated_folds) / len(populated_folds) if populated_folds else 0.0
    if coverage_days < 90:
        validation_status = "insufficient_history"
        validation_reason = "requires_at_least_90_days_before_strategy_selection"
    elif expectancy > 0 and snapshot.summary.profit_factor >= 1.15 and positive_fold_ratio >= 0.6:
        validation_status = "promising_not_live"
        validation_reason = "passes_initial_rolling_stability_gate_but_still_requires_forward_test"
    else:
        validation_status = "rejected"
        validation_reason = "failed_expectancy_profit_factor_or_temporal_stability_gate"
    assumptions = [
        "Cada decisão usa somente informações disponíveis no fechamento do candle; candles futuros nunca entram no cálculo.",
        "O regime usa H1 completo, ADX(14), EMA50/EMA200 e percentil de ATR, calculados sem look-ahead.",
        f"Entradas são limitadas à sessão {settings.backtest_session_start_utc}–{settings.backtest_session_end_utc} UTC e posições são encerradas diariamente.",
        "Compras usam ask na entrada e bid na saída; vendas usam bid na entrada e ask na saída.",
        f"O replay aplica spread de {settings.backtest_spread_points:g} pontos, slippage de {settings.backtest_slippage_points:g} pontos por fill e comissão configurada.",
        f"O tamanho de um ponto usado para {bot.symbol} é {point_size:g}; o valor por ponto/lote permanece o da configuração do harness.",
        "Quando stop e alvo aparecem no mesmo candle, o stop tem precedência; M1/ticks continuam sendo preferíveis para resolver a sequência intrabar.",
    ]
    if bot.strategy_profile == "fimath_ema20_flow":
        assumptions.append("O replay do Fimath valida somente o gatilho técnico; livro de ofertas e ticks históricos não estão disponíveis.")
    if bot.strategy_profile in WEIGHTED_PORTFOLIO_PROFILES:
        weights = WEIGHTED_PORTFOLIO_PROFILES[bot.strategy_profile]
        assumptions.append(f"A variante mantém posição única e usa {weights['bot-7']:.0%} do risco-base no Bot 7 e {weights['bot-6']:.0%} no Bot 6; os pesos não representam posições simultâneas.")
        assumptions.append("O Bot 6 somente é admitido quando o regime H1 causal confirma tendência de baixa; o mesmo bloqueio existe no runtime shadow.")
    if bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE:
        assumptions.append("O meta-portfólio mantém Bot 13 e Portfólio 7/6 100/100 independentes, usa risco-base integral para qualquer componente e nunca soma posições.")
        assumptions.append("Sinais conflitantes são bloqueados; quando há concordância, o ZigZag confirmado tem prioridade. Não há limite de quantidade de entradas; perda diária de 2R, posição única e cooldown continuam compartilhados.")
    if bot.strategy_profile in HAPPY_GOLD_PROFILES:
        assumptions.append("A estratégia agrega M5 em M30 completo e usa pivôs fractais confirmados como aproximação causal do ZigZag proprietário; não há repaint nem antecipação do pivô.")
    if bot.strategy_profile in {"happy_gold_zigzag_scalper", "happy_gold_zigzag_regime_confirmed"}:
        assumptions.append("SL de 240 pontos, TP de 1.000 pontos e lote 0,10 reproduzem a assinatura observada; o replay ativa break-even em +40 pontos no preço de entrada e trailing após +120 pontos, distante 60 pontos.")
        assumptions.append("O trailing OHLC é atualizado pelo extremo favorável de um candle e só pode encerrar a partir do candle seguinte; isso evita presumir uma sequência intrabar favorável. Ticks continuam necessários para reproduzir saídas em segundos.")
    if bot.strategy_profile == "happy_gold_zigzag_regime_confirmed":
        assumptions.append("O Bot 13 mantém o rompimento-base em range/tendência; transição e alta volatilidade exigem fechamento M5 além do pivô e direção H1 alinhada ao sinal.")
    if period_start > requested_start + interval:
        assumptions.append(f"A base não cobre os {days} dias completos; o replay começa em {period_start.isoformat()}.")
    chart_start = period_end - timedelta(days=min(days, 30))
    chart_candles = [candle for candle in visible_candles if candle.timestamp_utc >= chart_start]
    if days > 30:
        assumptions.append("As métricas usam todo o período solicitado; o gráfico limita os candles aos 30 dias mais recentes para manter a página responsiva.")
    return HistoricalReplayResponse(
        bot_id=bot.id,
        symbol=bot.symbol,
        timeframe=bot.timeframe,
        risk_per_trade_pct=settings.backtest_risk_per_trade_pct,
        strategy_version=STRATEGY_VERSION,
        settings_fingerprint=replay_settings_fingerprint(settings, bot),
        period_start_utc=period_start,
        period_end_utc=period_end,
        candles_evaluated=len(visible_candles),
        candidates_detected=candidates,
        candidates_eligible=eligible,
        candidates_blocked=candidates - eligible,
        regime_counts=dict(regime_counts),
        blocked_reasons=dict(blocked_reasons),
        expectancy_r=expectancy,
        max_drawdown_r=max_drawdown,
        max_consecutive_losses=max_loss_streak,
        forced_session_closes=forced_session_closes,
        total_costs=total_costs,
        validation_status=validation_status,
        validation_reason=validation_reason,
        validation_folds=folds,
        assumptions=assumptions,
        summary=snapshot.summary,
        daily_pnl=snapshot.daily_pnl,
        candles=chart_candles,
        trades=trades,
    )
