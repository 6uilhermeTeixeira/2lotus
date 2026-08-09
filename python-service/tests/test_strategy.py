import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.schemas import AccountSnapshot, CandleSnapshot, DecisionAction, DecisionRequest, FlowSnapshot, OrderBookLevel, PositionSnapshot
from app.regime import RegimeSnapshot
from app.strategy import FlowSample, _flow_analysis, evaluate_decision, reset_strategy_state


def settings(**overrides: object) -> SimpleNamespace:
    values = {
        "default_symbol": "XAUUSD",
        "default_timeframe": "M5",
        "max_spread_points": 50.0,
        "risk_per_trade_pct": 1.0,
        "strategy_profile": "fimath_ema20_flow",
        "fimath_flow_window_seconds": 300,
        "fimath_flow_min_samples": 30,
        "fimath_flow_z_threshold": 1.0,
        "fimath_flow_policy": "veto",
        "fimath_mode": "shadow",
        "enable_live_decisions": False,
        "fimath_partial_pct": 50.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def bullish_request(timestamp: datetime, ask: float = 102.1) -> DecisionRequest:
    candles = []
    for index in range(22):
        close = 100 + index * 0.1
        candles.append(
            CandleSnapshot(
                timestamp_utc=timestamp - timedelta(minutes=(22 - index) * 5),
                open=close - 0.05,
                high=close + 0.25,
                low=close - 0.25,
                close=close,
                volume=100,
            )
        )
    candles.append(
        CandleSnapshot(
            timestamp_utc=timestamp,
            open=101.7,
            high=102.3,
            low=100.7,
            close=102.0,
            volume=120,
        )
    )
    return DecisionRequest(
        bot_id="bot-1",
        request_id=f"request-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(seconds=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=102.0,
        ask=ask,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
        flow=FlowSnapshot(
            book=[
                OrderBookLevel(side="bid", price=102, volume=100),
                OrderBookLevel(side="ask", price=102.1, volume=100),
            ],
            up_ticks=2,
            down_ticks=1,
            up_volume=2,
            down_volume=1,
        ),
    )


def momentum_request(timestamp: datetime) -> DecisionRequest:
    candles = []
    for index in range(51):
        close = 100 + index * 0.08
        candles.append(
            CandleSnapshot(
                timestamp_utc=timestamp - timedelta(minutes=(52 - index) * 5),
                open=close - 0.03,
                high=close + 0.12,
                low=close - 0.12,
                close=close,
                volume=100,
            )
        )
    previous_high = max(candle.high for candle in candles[-12:])
    candles.append(
        CandleSnapshot(
            timestamp_utc=timestamp,
            open=previous_high - 0.05,
            high=previous_high + 0.35,
            low=previous_high - 0.10,
            close=previous_high + 0.25,
            volume=130,
        )
    )
    return DecisionRequest(
        bot_id="bot-2",
        request_id=f"momentum-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(seconds=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=candles[-1].close,
        ask=candles[-1].close + 0.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def happy_gold_breakout_request(timestamp: datetime) -> DecisionRequest:
    candles: list[CandleSnapshot] = []
    bucket_highs = [101.0, 102.0, 106.0, 103.0, 102.0, 101.5, 102.5]
    bucket_lows = [99.0, 99.5, 100.0, 99.8, 99.6, 99.4, 99.7]
    start = timestamp - timedelta(hours=3, minutes=30)
    for bucket, (high, low) in enumerate(zip(bucket_highs, bucket_lows)):
        for row in range(6):
            close = 100.5 + bucket * .05 + row * .01
            candles.append(
                CandleSnapshot(
                    timestamp_utc=start + timedelta(minutes=bucket * 30 + row * 5),
                    open=close - .02,
                    high=high if row == 2 else close + .1,
                    low=low if row == 3 else close - .1,
                    close=close,
                    volume=100,
                )
            )
    candles[-1].close = 105.8
    candles.append(
        CandleSnapshot(
            timestamp_utc=timestamp,
            open=105.8,
            high=106.2,
            low=105.7,
            close=106.1,
            volume=120,
        )
    )
    return DecisionRequest(
        bot_id="bot-12",
        request_id=f"happy-gold-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(minutes=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=106.1,
        ask=106.2,
        spread_points=10,
        point_size=.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def happy_gold_retest_request(timestamp: datetime) -> DecisionRequest:
    request = happy_gold_breakout_request(timestamp)
    request.bot_id = "bot-15"
    request.candles.append(
        CandleSnapshot(
            timestamp_utc=timestamp + timedelta(minutes=5),
            open=106.01,
            high=106.08,
            low=105.98,
            close=106.04,
            volume=110,
        )
    )
    request.timestamp_utc = timestamp + timedelta(minutes=10)
    request.bid = 106.04
    request.ask = 106.05
    request.spread_points = 1
    return request
def mean_reversion_request(timestamp: datetime) -> DecisionRequest:
    candles = []
    for index in range(21):
        close = 103 - index * 0.15
        candles.append(
            CandleSnapshot(
                timestamp_utc=timestamp - timedelta(minutes=(22 - index) * 5),
                open=close + 0.05,
                high=close + 0.20,
                low=close - 0.20,
                close=close,
                volume=100,
            )
        )
    candles.append(
        CandleSnapshot(
            timestamp_utc=timestamp,
            open=99.0,
            high=100.4,
            low=97.0,
            close=100.2,
            volume=140,
        )
    )
    return DecisionRequest(
        bot_id="bot-3",
        request_id=f"mean-reversion-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(seconds=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=100.2,
        ask=100.3,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def macd_trend_request(timestamp: datetime) -> DecisionRequest:
    closes = [100 + index * 0.03 for index in range(57)]
    closes.extend([closes[-1] - 0.05, closes[-1] - 0.10, closes[-1] + 0.40])
    candles = [
        CandleSnapshot(
            timestamp_utc=timestamp - timedelta(minutes=(len(closes) - index - 1) * 5),
            open=close - 0.03,
            high=close + 0.15,
            low=close - 0.15,
            close=close,
            volume=100 + index,
        )
        for index, close in enumerate(closes)
    ]
    return DecisionRequest(
        bot_id="bot-4",
        request_id=f"macd-trend-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(seconds=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=candles[-1].close,
        ask=candles[-1].close + 0.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def stochastic_pullback_request(timestamp: datetime) -> DecisionRequest:
    closes = [100 + index * 0.03 for index in range(56)]
    closes.extend([closes[-1] - 0.2, closes[-1] - 0.4, closes[-1] - 0.6, closes[-1] + 0.2])
    candles = [
        CandleSnapshot(
            timestamp_utc=timestamp - timedelta(minutes=(len(closes) - index - 1) * 5),
            open=close - 0.05,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            volume=100 + index,
        )
        for index, close in enumerate(closes)
    ]
    return DecisionRequest(
        bot_id="bot-5",
        request_id=f"stochastic-pullback-{timestamp.timestamp()}",
        timestamp_utc=timestamp + timedelta(seconds=5),
        symbol="XAUUSD",
        timeframe="M5",
        bid=candles[-1].close,
        ask=candles[-1].close + 0.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def stochastic_sell_request(timestamp: datetime) -> DecisionRequest:
    request = stochastic_pullback_request(timestamp)
    request.bot_id = "bot-6"
    pivot = 200.0
    for candle in request.candles:
        old_open, old_high, old_low, old_close = candle.open, candle.high, candle.low, candle.close
        candle.open = pivot - old_open
        candle.high = pivot - old_low
        candle.low = pivot - old_high
        candle.close = pivot - old_close
    request.bid = request.candles[-1].close
    request.ask = request.bid + 0.1
    return request


def multitimeframe_sell_request(decision_time: datetime) -> DecisionRequest:
    first = decision_time - timedelta(minutes=5 * 1000)
    candles = []
    for index in range(1000):
        close = 220 - index * .03
        candles.append(CandleSnapshot(
            timestamp_utc=first + timedelta(minutes=index * 5),
            open=close + .02,
            high=close + .08,
            low=close - .08,
            close=close,
            volume=100,
        ))
    current = candles[-1]
    current.open = current.close + .18
    current.high = current.open + .08
    current.low = current.close - .08
    return DecisionRequest(
        bot_id="bot-7",
        request_id=f"multitimeframe-{decision_time.timestamp()}",
        timestamp_utc=decision_time,
        symbol="XAUUSD",
        timeframe="M5",
        bid=current.close,
        ask=current.close + .1,
        spread_points=10,
        point_size=.01,
        value_per_point_per_lot=1,
        candles=candles,
        positions=[],
        account=AccountSnapshot(balance=10_000, equity=10_000, margin_free=10_000),
    )


def multitimeframe_buy_request(decision_time: datetime) -> DecisionRequest:
    request = multitimeframe_sell_request(decision_time)
    pivot = 200.0
    for candle in request.candles:
        old_open, old_high, old_low, old_close = candle.open, candle.high, candle.low, candle.close
        candle.open = pivot - old_open
        candle.high = pivot - old_low
        candle.low = pivot - old_high
        candle.close = pivot - old_close
    request.bid = request.candles[-1].close
    request.ask = request.bid + .1
    return request


class FlowAnalysisTests(unittest.TestCase):
    def test_strong_directional_flow_is_confirmed(self) -> None:
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        samples = deque(
            FlowSample(start + timedelta(seconds=index * 10), (index % 3 - 1) * 0.01, (index % 5 - 2) * 0.01)
            for index in range(30)
        )
        samples.append(FlowSample(start + timedelta(seconds=300), 0.8, 0.8))

        result = _flow_analysis(samples, DecisionAction.BUY, 300, 30, 1.0)

        self.assertTrue(result.ready)
        self.assertTrue(result.confirmed)
        self.assertFalse(result.contradicted)
        self.assertEqual(result.status, "aggression")

    def test_strong_opposing_flow_vetoes_candidate(self) -> None:
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        samples = deque(
            FlowSample(start + timedelta(seconds=index * 10), (index % 3 - 1) * 0.01, (index % 5 - 2) * 0.01)
            for index in range(30)
        )
        samples.append(FlowSample(start + timedelta(seconds=300), -0.8, -0.8))

        result = _flow_analysis(samples, DecisionAction.BUY, 300, 30, 1.0)

        self.assertTrue(result.ready)
        self.assertFalse(result.confirmed)
        self.assertTrue(result.contradicted)
        self.assertEqual(result.status, "flow_contradiction")


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_strategy_state()

    def retired_warmup_candidate_keeps_prices_and_telemetry(self) -> None:
        request = bullish_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings())

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "flow_warmup")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertIsNotNone(response.entry_price)
        self.assertIsNotNone(response.stop_loss_price)

    def setup_uses_current_quote_instead_of_stale_candle_cache(self) -> None:
        candle_time = datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc)
        first = evaluate_decision(bullish_request(candle_time, ask=102.1), settings())
        second_request = bullish_request(candle_time, ask=105.0)
        second_request.timestamp_utc += timedelta(seconds=5)

        second = evaluate_decision(second_request, settings())

        self.assertEqual(first.entry_price, 102.1)
        self.assertEqual(second.entry_price, 105.0)

    def retired_momentum_profile_records_shadow_breakout_candidate(self) -> None:
        request = momentum_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "momentum_atr_breakout")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "momentum_atr_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["strategy_profile"], "momentum_atr_breakout")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertIn("ema50", response.metadata)
        self.assertIn("breakout_level", response.metadata)

    def retired_rsi_bollinger_profile_records_independent_shadow_candidate(self) -> None:
        request = mean_reversion_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "rsi_bollinger_reversion")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "rsi_bollinger_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["strategy_profile"], "rsi_bollinger_reversion")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertIn("rsi14", response.metadata)
        self.assertIn("bollinger_lower", response.metadata)

    def retired_macd_trend_profile_records_independent_shadow_candidate(self) -> None:
        request = macd_trend_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "macd_trend_crossover")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "macd_trend_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["strategy_profile"], "macd_trend_crossover")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertIn("macd_line", response.metadata)
        self.assertIn("macd_signal", response.metadata)
        self.assertGreater(response.metadata["macd_histogram"], 0)

    def retired_stochastic_pullback_profile_records_independent_shadow_candidate(self) -> None:
        request = stochastic_pullback_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "stochastic_trend_pullback")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "stochastic_pullback_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["strategy_profile"], "stochastic_trend_pullback")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertGreater(response.metadata["stochastic_k"], response.metadata["stochastic_d"])
        self.assertIn("ema50", response.metadata)

    def test_stochastic_sell_experiment_blocks_buy_candidates(self) -> None:
        request = stochastic_pullback_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-6"

        response = evaluate_decision(request, settings(), "stochastic_sell_session")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "experimental_side_or_session_block")
        self.assertFalse(response.metadata["candidate"])
        self.assertTrue(response.metadata["base_candidate"])
        self.assertEqual(response.metadata["experimental_block_reason"], "buy_side")

    def test_stochastic_sell_experiment_accepts_sell_before_18_utc(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "stochastic_sell_session")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "stochastic_sell_session_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["suggested_action"], "sell")
        self.assertEqual(response.metadata["experiment"], "sell_only_entry_before_18utc")
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_stochastic_sell_experiment_stays_shadow_when_global_live_is_enabled(self) -> None:
        configured = settings()
        configured.enable_live_decisions = True
        configured.fimath_mode = "live"
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, configured, "stochastic_sell_session")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.size, 0)
        self.assertEqual(response.reason, "stochastic_sell_session_shadow_signal")
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_stochastic_sell_experiment_blocks_entries_at_18_utc(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 18, 20, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "stochastic_sell_session")

        self.assertEqual(response.reason, "experimental_side_or_session_block")
        self.assertFalse(response.metadata["candidate"])
        self.assertEqual(response.metadata["experimental_block_reason"], "entry_at_or_after_18utc")

    def test_multitimeframe_profile_aligns_h1_m15_and_m5_in_shadow(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "multitimeframe_trend_pullback")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "multitimeframe_trend_pullback_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertGreaterEqual(response.metadata["trend_strength"], .7)
        self.assertEqual(response.metadata["risk_multiplier"], .5)
        self.assertEqual(response.metadata["effective_risk_pct"], .5)
        self.assertEqual(response.risk_amount, 50)
        self.assertAlmostEqual(response.take_profit_points / response.stop_loss_points, .75, places=2)
        self.assertEqual(response.partial_close_pct, 100)
        self.assertEqual(response.metadata["max_holding_bars"], 12)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_multitimeframe_profile_blocks_entries_outside_research_session(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "multitimeframe_trend_pullback")

        self.assertEqual(response.reason, "multitimeframe_setup_not_confirmed")
        self.assertFalse(response.metadata.get("candidate", False))

    def test_multitimeframe_profile_rejects_sells_in_reduced_loss_experiment(self) -> None:
        request = multitimeframe_sell_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "multitimeframe_trend_pullback")

        self.assertEqual(response.reason, "multitimeframe_setup_not_confirmed")
        self.assertFalse(response.metadata.get("candidate", False))

    def retired_portfolio_6_7_allocates_75_percent_to_bot_7(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-8"

        response = evaluate_decision(request, settings(), "portfolio_6_7")

        self.assertEqual(response.reason, "portfolio_6_7_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["portfolio_component"], "bot-7")
        self.assertEqual(response.metadata["portfolio_weight_pct"], 75)
        self.assertEqual(response.metadata["effective_risk_pct"], .75)
        self.assertEqual(response.risk_amount, 75)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def retired_portfolio_6_7_allocates_25_percent_to_bot_6(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-8"

        response = evaluate_decision(request, settings(), "portfolio_6_7")

        self.assertEqual(response.reason, "portfolio_6_7_shadow_signal")
        self.assertEqual(response.metadata["portfolio_component"], "bot-6")
        self.assertEqual(response.metadata["portfolio_weight_pct"], 25)
        self.assertEqual(response.risk_amount, 25)

    def retired_portfolio_6_7_blocks_new_signal_with_existing_xauusd_exposure(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-8"
        request.positions = [PositionSnapshot(symbol="XAUUSD", side="sell", volume=.1, entry_price=100)]

        response = evaluate_decision(request, settings(), "portfolio_6_7")

        self.assertEqual(response.reason, "portfolio_shared_exposure_block")
        self.assertFalse(response.metadata["candidate"])
        self.assertTrue(response.metadata["base_candidate"])

    def retired_adaptive_portfolio_selects_bot_7_with_frozen_daily_health(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-9"
        configured = settings(
            adaptive_active_component="bot-7",
            adaptive_component_health={"bot-7": {"closed_trades": 12, "expectancy_r": .2, "profit_factor": 1.5, "eligible": True}},
            adaptive_regime_snapshot=RegimeSnapshot("transition", "none", "test", adx=23, atr_percentile=40),
            enable_live_decisions=True,
            fimath_mode="live",
        )

        response = evaluate_decision(request, configured, "adaptive_portfolio_3_6_7")

        self.assertEqual(response.reason, "adaptive_portfolio_3_6_7_shadow_signal")
        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["portfolio_component"], "bot-7")
        self.assertEqual(response.metadata["portfolio_weight_pct"], 100)
        self.assertEqual(response.risk_amount, 100)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def retired_adaptive_portfolio_requires_sell_trend_for_bot_6(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-9"
        configured = settings(
            adaptive_active_component="bot-6",
            adaptive_component_health={},
            adaptive_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=40),
        )

        response = evaluate_decision(request, configured, "adaptive_portfolio_3_6_7")

        self.assertEqual(response.reason, "adaptive_component_regime_block")
        self.assertFalse(response.metadata["candidate"])
        self.assertTrue(response.metadata["base_candidate"])

    def retired_adaptive_portfolio_requires_range_and_atr_25_for_bot_3(self) -> None:
        request = mean_reversion_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-9"
        configured = settings(
            adaptive_active_component="bot-3",
            adaptive_component_health={},
            adaptive_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=30),
        )

        response = evaluate_decision(request, configured, "adaptive_portfolio_3_6_7")

        self.assertEqual(response.reason, "adaptive_portfolio_3_6_7_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["portfolio_component"], "bot-3")

    def retired_adaptive_portfolio_stays_flat_without_eligible_health(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-9"

        response = evaluate_decision(request, settings(adaptive_component_health={}), "adaptive_portfolio_3_6_7")

        self.assertEqual(response.reason, "adaptive_health_gate_not_confirmed")
        self.assertFalse(response.metadata["candidate"])

    def retired_bot_10_uses_75_percent_for_bot_7_and_stays_shadow(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-10"
        configured = settings(enable_live_decisions=True, fimath_mode="live", portfolio_risk_state={})

        response = evaluate_decision(request, configured, "portfolio_6_7_75_50")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "portfolio_6_7_75_50_shadow_signal")
        self.assertEqual(response.metadata["portfolio_component"], "bot-7")
        self.assertEqual(response.metadata["portfolio_weight_pct"], 75)
        self.assertEqual(response.risk_amount, 75)

    def retired_bot_10_uses_50_percent_for_bot_6_in_sell_trend(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-10"
        configured = settings(
            portfolio_risk_state={},
            portfolio_regime_snapshot=RegimeSnapshot("trend", "sell", "test", adx=30, atr_percentile=40),
        )

        response = evaluate_decision(request, configured, "portfolio_6_7_75_50")

        self.assertEqual(response.reason, "portfolio_6_7_75_50_shadow_signal")
        self.assertEqual(response.metadata["portfolio_component"], "bot-6")
        self.assertEqual(response.metadata["portfolio_weight_pct"], 50)
        self.assertEqual(response.risk_amount, 50)

    def test_bot_11_uses_100_and_100_percent_weights(self) -> None:
        bot_7_request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        bot_7_request.bot_id = "bot-11"
        bot_6_request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        bot_6_request.bot_id = "bot-11"
        configured = settings(
            portfolio_risk_state={},
            portfolio_regime_snapshot=RegimeSnapshot("trend", "sell", "test", adx=30, atr_percentile=40),
        )

        bot_7 = evaluate_decision(bot_7_request, configured, "portfolio_6_7_100_100")
        bot_6 = evaluate_decision(bot_6_request, configured, "portfolio_6_7_100_100")

        self.assertEqual(bot_7.metadata["portfolio_weight_pct"], 100)
        self.assertEqual(bot_7.risk_amount, 100)
        self.assertEqual(bot_6.metadata["portfolio_weight_pct"], 100)
        self.assertEqual(bot_6.risk_amount, 100)

    def test_bot_16_can_select_zigzag_without_adding_portfolio_risk(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-16"
        configured = settings(
            portfolio_risk_state={"trades_started": 99, "daily_outcome_r": 0},
            portfolio_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=40),
            happy_gold_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=40),
        )

        response = evaluate_decision(request, configured, "happy_gold_portfolio_100_100")

        self.assertEqual(response.reason, "happy_gold_portfolio_100_100_shadow_signal")
        self.assertEqual(response.metadata["combined_component"], "bot-13")
        self.assertEqual(response.metadata["combined_shared_risk_pct"], 1)
        self.assertEqual(response.metadata["portfolio_weight_pct"], 100)
        self.assertEqual(response.risk_amount, 100)
        self.assertEqual(response.metadata["combined_daily_trade_cap"], "unlimited")

    def retired_weighted_portfolio_blocks_bot_6_without_sell_trend(self) -> None:
        request = stochastic_sell_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        request.bot_id = "bot-10"
        configured = settings(
            portfolio_risk_state={},
            portfolio_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=40),
        )

        response = evaluate_decision(request, configured, "portfolio_6_7_75_50")

        self.assertEqual(response.reason, "weighted_portfolio_bot_6_regime_block")
        self.assertFalse(response.metadata["candidate"])

    def retired_weighted_portfolio_applies_persisted_daily_trade_cap(self) -> None:
        request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-10"
        configured = settings(portfolio_risk_state={"trades_started": 2, "daily_outcome_r": 0})

        response = evaluate_decision(request, configured, "portfolio_6_7_75_50")

        self.assertEqual(response.reason, "weighted_portfolio_risk_block")
        self.assertFalse(response.metadata["candidate"])
        self.assertIn("daily_trade_cap", response.metadata["portfolio_risk_block_reason"])

    def test_bot_12_reconstructs_fixed_geometry_and_stays_shadow(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))

        response = evaluate_decision(request, settings(), "happy_gold_zigzag_scalper")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_zigzag_scalper_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertEqual(response.metadata["suggested_size"], .1)
        self.assertEqual(response.stop_loss_points, 240)
        self.assertEqual(response.take_profit_points, 1000)
        self.assertAlmostEqual(response.entry_price - response.stop_loss_price, 2.4)
        self.assertAlmostEqual(response.take_profit_price - response.entry_price, 10.0)
        self.assertEqual(response.risk_amount, 24)
        self.assertEqual(response.metadata["break_even_trigger_points"], 40)
        self.assertEqual(response.metadata["break_even_offset_points"], 0)
        self.assertEqual(response.metadata["trailing_activation_points"], 120)
        self.assertEqual(response.metadata["trailing_distance_points"], 60)
        self.assertEqual(response.metadata["max_holding_bars"], 2)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_bot_12_blocks_existing_xauusd_position(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.positions = [PositionSnapshot(symbol="XAUUSD", side="buy", volume=.1, entry_price=105)]

        response = evaluate_decision(request, settings(), "happy_gold_zigzag_scalper")

        self.assertEqual(response.reason, "happy_gold_market_or_risk_filter_mismatch")
        self.assertFalse(response.metadata["candidate"])

    def test_bot_13_keeps_base_breakout_in_range(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-13"

        response = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=40)),
            "happy_gold_zigzag_regime_confirmed",
        )

        self.assertEqual(response.reason, "happy_gold_zigzag_regime_confirmed_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["zigzag_confirmation_reason"], "base_breakout_allowed:range")
        self.assertEqual(response.metadata["suggested_size"], .1)
        self.assertEqual(response.stop_loss_points, 240)
        self.assertEqual(response.take_profit_points, 1000)

    def test_bot_13_requires_close_and_direction_in_ambiguous_regime(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-13"

        aligned = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("high_volatility", "buy", "test", adx=30, atr_percentile=95)),
            "happy_gold_zigzag_regime_confirmed",
        )
        opposing = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("high_volatility", "sell", "test", adx=30, atr_percentile=95)),
            "happy_gold_zigzag_regime_confirmed",
        )

        self.assertTrue(aligned.metadata["candidate"])
        self.assertTrue(aligned.metadata["zigzag_close_confirmed"])
        self.assertTrue(aligned.metadata["zigzag_direction_aligned"])
        self.assertEqual(opposing.reason, "happy_gold_selective_confirmation_block")
        self.assertFalse(opposing.metadata["candidate"])
        self.assertTrue(opposing.metadata["base_candidate"])

    def test_bot_13_rejects_intrabar_only_breakout_in_high_volatility(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-13"
        request.candles[-1].close = 105.99
        request.bid = 105.99
        request.ask = 106.01

        response = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("high_volatility", "buy", "test", adx=30, atr_percentile=95)),
            "happy_gold_zigzag_regime_confirmed",
        )

        self.assertEqual(response.reason, "happy_gold_selective_confirmation_block")
        self.assertFalse(response.metadata["zigzag_close_confirmed"])
        self.assertTrue(response.metadata["zigzag_direction_aligned"])

    def retired_bot_15_requires_retest_and_uses_atr_risk_geometry(self) -> None:
        request = happy_gold_retest_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))

        response = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("trend", "buy", "test", adx=30, atr_percentile=50)),
            "happy_gold_zigzag_retest_atr",
        )

        self.assertEqual(response.reason, "happy_gold_zigzag_retest_atr_shadow_signal")
        self.assertTrue(response.metadata["candidate"])
        self.assertEqual(response.metadata["entry_model"], "breakout_retest_rejection")
        self.assertAlmostEqual(float(response.take_profit_points), float(response.stop_loss_points) * 2, delta=.02)
        self.assertEqual(response.metadata["max_holding_bars"], 12)

    def retired_bot_15_blocks_range_even_with_valid_retest(self) -> None:
        request = happy_gold_retest_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))

        response = evaluate_decision(
            request,
            settings(happy_gold_regime_snapshot=RegimeSnapshot("range", "both", "test", adx=18, atr_percentile=50)),
            "happy_gold_zigzag_retest_atr",
        )

        self.assertEqual(response.reason, "happy_gold_v2_filter_or_retest_block")
        self.assertFalse(response.metadata["candidate"])

    def retired_fifteen_profiles_keep_independent_bot_scoped_state(self) -> None:
        candle_time = datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc)
        portfolio_request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        portfolio_request.bot_id = "bot-8"
        adaptive_request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        adaptive_request.bot_id = "bot-9"
        bot_10_request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        bot_10_request.bot_id = "bot-10"
        bot_11_request = multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc))
        bot_11_request.bot_id = "bot-11"
        bot_12_request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        bot_13_request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        bot_13_request.bot_id = "bot-13"
        bot_15_request = happy_gold_retest_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        bot_16_request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        bot_16_request.bot_id = "bot-16"
        cases = [
            (bullish_request(candle_time), "fimath_ema20_flow"),
            (momentum_request(candle_time), "momentum_atr_breakout"),
            (mean_reversion_request(candle_time), "rsi_bollinger_reversion"),
            (macd_trend_request(candle_time), "macd_trend_crossover"),
            (stochastic_pullback_request(candle_time), "stochastic_trend_pullback"),
            (stochastic_sell_request(candle_time), "stochastic_sell_session"),
            (multitimeframe_buy_request(datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)), "multitimeframe_trend_pullback"),
            (portfolio_request, "portfolio_6_7"),
            (adaptive_request, "adaptive_portfolio_3_6_7"),
            (bot_10_request, "portfolio_6_7_75_50"),
            (bot_11_request, "portfolio_6_7_100_100"),
            (bot_12_request, "happy_gold_zigzag_scalper"),
            (bot_13_request, "happy_gold_zigzag_regime_confirmed"),
            (bot_15_request, "happy_gold_zigzag_retest_atr"),
            (bot_16_request, "happy_gold_portfolio_100_100"),
        ]

        responses = [
            evaluate_decision(
                request,
                settings(happy_gold_regime_snapshot=RegimeSnapshot("trend", "buy", "test", adx=30, atr_percentile=40)),
                profile,
            )
            for request, profile in cases
        ]

        self.assertEqual({request.bot_id for request, _ in cases}, {"bot-1", "bot-2", "bot-3", "bot-4", "bot-5", "bot-6", "bot-7", "bot-8", "bot-9", "bot-10", "bot-11", "bot-12", "bot-13", "bot-15", "bot-16"})
        self.assertEqual(
            {response.metadata["strategy_profile"] for response in responses},
            {"fimath_ema20_flow", "momentum_atr_breakout", "rsi_bollinger_reversion", "macd_trend_crossover", "stochastic_trend_pullback", "stochastic_sell_session", "multitimeframe_trend_pullback", "portfolio_6_7", "adaptive_portfolio_3_6_7", "portfolio_6_7_75_50", "portfolio_6_7_100_100", "happy_gold_zigzag_scalper", "happy_gold_zigzag_regime_confirmed", "happy_gold_zigzag_retest_atr", "happy_gold_portfolio_100_100"},
        )
        self.assertTrue(all(response.action == DecisionAction.HOLD for response in responses))

    def test_unknown_profile_fails_closed(self) -> None:
        response = evaluate_decision(
            bullish_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc)),
            settings(),
            "unknown",
        )

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "unsupported_strategy_profile")

    def test_happy_gold_tick_impulse_requires_a_warmed_window(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-17"
        request.indicators = {"impulse_30s_ready": 0, "impulse_30s_move_points": 150}

        response = evaluate_decision(request, settings(), "happy_gold_tick_impulse_shadow")

        self.assertEqual(response.reason, "happy_gold_tick_impulse_filter_block")
        self.assertFalse(response.metadata["candidate"])

    def test_happy_gold_tick_impulse_emits_isolated_buy_shadow_candidate(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-17"
        request.indicators = {"impulse_30s_ready": 1, "impulse_30s_move_points": 110}

        response = evaluate_decision(request, settings(), "happy_gold_tick_impulse_shadow")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_tick_impulse_shadow_signal")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertEqual(response.metadata["suggested_size"], .10)
        self.assertEqual(response.stop_loss_points, 240)
        self.assertEqual(response.take_profit_points, 1000)
        self.assertFalse(response.metadata["record_paper_trade"])
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_happy_gold_tick_impulse_uses_negative_move_for_sell(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-17"
        request.indicators = {"impulse_30s_ready": 1, "impulse_30s_move_points": -95.5}

        response = evaluate_decision(request, settings(), "happy_gold_tick_impulse_shadow")

        self.assertEqual(response.reason, "happy_gold_tick_impulse_shadow_signal")
        self.assertEqual(response.metadata["suggested_action"], "sell")
        self.assertGreater(float(response.stop_loss_price), float(response.entry_price))
        self.assertLess(float(response.take_profit_price), float(response.entry_price))

    def test_happy_gold_tick_impulse_normalizes_three_digit_broker_points(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-17"
        request.point_size = .001
        request.indicators = {"impulse_30s_ready": 1, "impulse_30s_move_points": 955}

        response = evaluate_decision(request, settings(), "happy_gold_tick_impulse_shadow")

        self.assertEqual(response.reason, "happy_gold_tick_impulse_shadow_signal")
        self.assertAlmostEqual(response.metadata["impulse_30s_move_pips"], 95.5)
        self.assertAlmostEqual(float(response.entry_price) - float(response.stop_loss_price), 2.4)
        self.assertEqual(response.stop_loss_points, 2400)

    def test_happy_gold_zigzag_tick_context_requires_aligned_900s_move(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-18"
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 799}

        response = evaluate_decision(request, settings(), "happy_gold_zigzag_tick_context_shadow")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_zigzag_tick_context_filter_block")
        self.assertFalse(response.metadata["candidate"])

    def test_happy_gold_zigzag_tick_context_emits_isolated_shadow_candidate(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-18"
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 800}

        response = evaluate_decision(request, settings(), "happy_gold_zigzag_tick_context_shadow")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_zigzag_tick_context_shadow_signal")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertEqual(response.metadata["suggested_size"], .10)
        self.assertEqual(response.metadata["impulse_900s_aligned_move_pips"], 800)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_happy_gold_structural_tick_emits_shadow_candidate_with_fixed_filters(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-19"
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 800}
        research_settings = settings(
            happy_gold_structural_context_900s_threshold_pips=800,
            happy_gold_structural_ema20_gap_pips=0,
            happy_gold_structural_max_pivot_extension_pips=1000,
        )

        response = evaluate_decision(request, research_settings, "happy_gold_zigzag_structural_tick_shadow")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_zigzag_structural_tick_shadow_signal")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertEqual(response.metadata["annual_replay_signals"], 27)
        self.assertEqual(response.stop_loss_points, 60)
        self.assertEqual(response.metadata["break_even_trigger_pips"], 40)
        self.assertEqual(response.metadata["trailing_distance_pips"], 20)
        self.assertEqual(response.metadata["max_holding_seconds"], 60)
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_happy_gold_structural_tick_blocks_friday(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 2000}

        response = evaluate_decision(request, settings(), "happy_gold_zigzag_structural_tick_shadow")

        self.assertEqual(response.reason, "happy_gold_zigzag_structural_tick_filter_block")
        weekday_check = next(check for check in response.risk_checks if check.name == "weekday_filter")
        self.assertFalse(weekday_check.passed)

    def test_happy_gold_normalized_range_emits_isolated_shadow_candidate(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-21"
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 800}
        research_settings = settings(
            happy_gold_structural_context_900s_threshold_pips=800,
            happy_gold_structural_ema20_gap_pips=0,
            happy_gold_structural_max_pivot_extension_pips=1000,
            happy_gold_normalized_range_max=100,
        )

        response = evaluate_decision(request, research_settings, "happy_gold_zigzag_normalized_range_shadow")

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_zigzag_normalized_range_shadow_signal")
        self.assertEqual(response.metadata["strategy_profile"], "happy_gold_zigzag_normalized_range_shadow")
        self.assertEqual(response.metadata["oos_2026_trades"], 75)
        self.assertTrue(response.metadata["tick_shadow_lifecycle"])
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_happy_gold_normalized_range_blocks_expanded_previous_candle(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 800}
        research_settings = settings(
            happy_gold_structural_context_900s_threshold_pips=800,
            happy_gold_structural_ema20_gap_pips=0,
            happy_gold_structural_max_pivot_extension_pips=1000,
            happy_gold_normalized_range_max=0,
        )

        response = evaluate_decision(request, research_settings, "happy_gold_zigzag_normalized_range_shadow")

        self.assertEqual(response.reason, "happy_gold_zigzag_normalized_range_shadow_filter_block")
        range_check = next(check for check in response.risk_checks if check.name == "normalized_prior_m5_range")
        self.assertFalse(range_check.passed)

    def test_happy_gold_normalized_range_blocks_spread_above_stress_limit(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.spread_points = 16
        request.indicators = {"impulse_900s_ready": 1, "impulse_900s_move_points": 800}
        research_settings = settings(
            happy_gold_structural_context_900s_threshold_pips=800,
            happy_gold_structural_ema20_gap_pips=0,
            happy_gold_structural_max_pivot_extension_pips=1000,
            happy_gold_normalized_range_max=100,
            happy_gold_normalized_range_max_spread_points=15,
        )

        response = evaluate_decision(request, research_settings, "happy_gold_zigzag_normalized_range_shadow")

        self.assertEqual(response.reason, "happy_gold_zigzag_normalized_range_shadow_filter_block")
        spread_check = next(check for check in response.risk_checks if check.name == "normalized_range_spread_limit")
        self.assertFalse(spread_check.passed)

    def test_happy_gold_multiwindow_tick_score_emits_isolated_shadow_candidate(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.bot_id = "bot-20"
        request.indicators = {
            "impulse_5s_ready": 1, "impulse_5s_move_points": 20,
            "impulse_10s_ready": 1, "impulse_10s_move_points": 40,
            "impulse_30s_ready": 1, "impulse_30s_move_points": 80,
            "impulse_60s_ready": 1, "impulse_60s_move_points": 100,
            "impulse_300s_ready": 1, "impulse_300s_move_points": 350,
            "impulse_900s_ready": 1, "impulse_900s_move_points": 400,
        }

        response = evaluate_decision(
            request, settings(happy_gold_multiwindow_shadow_enabled=True),
            "happy_gold_multiwindow_tick_score_shadow",
        )

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.reason, "happy_gold_multiwindow_tick_score_shadow_signal")
        self.assertEqual(response.metadata["suggested_action"], "buy")
        self.assertEqual(response.metadata["multiwindow_score"], 6)
        self.assertEqual(response.stop_loss_points, 60)
        self.assertTrue(response.metadata["tick_shadow_lifecycle"])
        self.assertTrue(response.metadata["experimental_shadow_lock"])

    def test_happy_gold_multiwindow_tick_score_requires_aligned_core_windows(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.indicators = {
            "impulse_5s_ready": 1, "impulse_5s_move_points": 20,
            "impulse_10s_ready": 1, "impulse_10s_move_points": 40,
            "impulse_30s_ready": 1, "impulse_30s_move_points": 80,
            "impulse_60s_ready": 1, "impulse_60s_move_points": 100,
            "impulse_300s_ready": 1, "impulse_300s_move_points": -350,
            "impulse_900s_ready": 1, "impulse_900s_move_points": 400,
        }

        response = evaluate_decision(request, settings(), "happy_gold_multiwindow_tick_score_shadow")

        self.assertEqual(response.reason, "happy_gold_multiwindow_tick_score_block")
        self.assertFalse(response.metadata["candidate"])

    def test_happy_gold_multiwindow_tick_score_is_locked_after_exhaustive_replay(self) -> None:
        request = happy_gold_breakout_request(datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc))
        request.indicators = {
            f"impulse_{seconds}s_{name}": value
            for seconds, move in ((5, 20), (10, 40), (30, 80), (60, 100), (300, 350), (900, 400))
            for name, value in (("ready", 1), ("move_points", move))
        }

        response = evaluate_decision(request, settings(), "happy_gold_multiwindow_tick_score_shadow")

        self.assertEqual(response.reason, "happy_gold_multiwindow_tick_score_block")
        replay_gate = next(check for check in response.risk_checks if check.name == "exhaustive_replay_gate")
        self.assertFalse(replay_gate.passed)


if __name__ == "__main__":
    unittest.main()
