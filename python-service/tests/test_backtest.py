import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.backtest import (
    _OpenTrade,
    _advance_trade,
    _force_close,
    _instrument_point_size,
    _risk_diagnostics,
    _timeframe_delta,
    _validation_folds,
    replay_settings_fingerprint,
    run_historical_replay,
)
from app.schemas import BotCatalogItem, CandleSnapshot, DecisionAction, DecisionResponse, TradeResult
from tests.test_strategy import settings


def replay_settings():
    configured = settings()
    configured.backtest_balance = 10_000
    configured.backtest_spread_points = 10
    configured.backtest_point_size = 0.01
    configured.backtest_value_per_point_per_lot = 1
    configured.backtest_risk_per_trade_pct = 0.25
    configured.backtest_slippage_points = 0
    configured.backtest_commission_per_lot = 0
    configured.backtest_session_start_utc = "06:00"
    configured.backtest_session_end_utc = "21:00"
    configured.backtest_max_trades_per_day = 4
    configured.backtest_daily_loss_limit_r = 2
    configured.backtest_cooldown_bars_after_loss = 3
    configured.backtest_regime_enabled = False
    configured.backtest_validation_fold_days = 5
    return configured


class HistoricalReplayTests(unittest.TestCase):
    def bot(self, symbol: str = "XAUUSD") -> BotCatalogItem:
        return BotCatalogItem(
            id="test-bot",
            name="Test",
            description="Test bot",
            strategy_profile="test_profile",
            symbol=symbol,
            timeframe="M5",
        )

    def trade_result(self, trade_id: str, opened_at: datetime, rr: float, fees: float = 0) -> TradeResult:
        return TradeResult(
            trade_id=trade_id,
            symbol="XAUUSD",
            timeframe="M5",
            setup="test",
            side="buy",
            opened_at_utc=opened_at,
            closed_at_utc=opened_at + timedelta(minutes=5),
            volume_lots=1,
            entry_price=100,
            exit_price=100 + rr,
            risk_amount=1,
            net_pnl=rr,
            gross_pnl=rr + fees,
            fees=fees,
            rr_realized=rr,
        )

    def test_fingerprint_is_stable_and_changes_with_replay_costs(self) -> None:
        configured = replay_settings()
        first = replay_settings_fingerprint(configured, self.bot())
        self.assertEqual(first, replay_settings_fingerprint(configured, self.bot()))
        configured.backtest_slippage_points += 1
        self.assertNotEqual(first, replay_settings_fingerprint(configured, self.bot()))

    def test_timeframe_and_instrument_point_sizes(self) -> None:
        self.assertEqual(_timeframe_delta("m5"), timedelta(minutes=5))
        self.assertEqual(_timeframe_delta("H2"), timedelta(hours=2))
        with self.assertRaisesRegex(ValueError, "unsupported replay timeframe"):
            _timeframe_delta("D1")
        self.assertEqual(_instrument_point_size(self.bot("EURUSD"), replay_settings()), 0.00001)
        self.assertEqual(_instrument_point_size(self.bot(), replay_settings()), 0.01)

    def test_risk_diagnostics_calculates_expectancy_drawdown_streak_and_costs(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        trades = [
            self.trade_result("win", start, 2, 1.25),
            self.trade_result("loss-1", start + timedelta(days=1), -1, 2),
            self.trade_result("loss-2", start + timedelta(days=2), -0.5, 0.75),
            self.trade_result("win-2", start + timedelta(days=3), 1, 1),
        ]
        expectancy, drawdown, streak, costs = _risk_diagnostics(list(reversed(trades)))
        self.assertEqual(expectancy, 0.375)
        self.assertEqual(drawdown, 1.5)
        self.assertEqual(streak, 2)
        self.assertEqual(costs, 5)
        self.assertEqual(_risk_diagnostics([]), (0.0, 0.0, 0, 0))

    def test_validation_folds_include_empty_and_positive_periods(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        trades = [self.trade_result("winner", start + timedelta(days=1), 2)]
        folds = _validation_folds(trades, start, start + timedelta(days=6), 2)
        self.assertEqual(len(folds), 3)
        self.assertEqual(folds[0].trades_count, 1)
        self.assertTrue(folds[0].positive)
        self.assertEqual(folds[1].trades_count, 0)
        self.assertFalse(folds[1].positive)

    def test_force_close_applies_sell_spread_slippage_commission_and_bar_end(self) -> None:
        configured = replay_settings()
        configured.backtest_slippage_points = 2
        configured.backtest_commission_per_lot = 4
        opened_at = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="forced-sell",
            setup="test",
            symbol="XAUUSD",
            timeframe="M5",
            side="sell",
            opened_at_utc=opened_at,
            volume_lots=0.5,
            entry_price=100,
            initial_stop_price=101,
            stop_price=101,
            partial_price=99,
            target_price=98,
            risk_amount=50,
        )
        candle = CandleSnapshot(timestamp_utc=opened_at, open=99, high=99, low=99, close=99, volume=1)
        result = _force_close(trade, candle, configured, "data_end")
        self.assertEqual(result.closed_at_utc, opened_at + timedelta(minutes=5))
        self.assertAlmostEqual(result.exit_price, 99.12)
        self.assertEqual(result.fees, 2)
        self.assertIn("saída=data_end", result.note)

    def test_sell_partial_then_target_closes_remaining_position(self) -> None:
        configured = replay_settings()
        configured.backtest_spread_points = 0
        opened_at = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="partial-sell",
            setup="test",
            symbol="XAUUSD",
            timeframe="M5",
            side="sell",
            opened_at_utc=opened_at,
            volume_lots=1,
            entry_price=100,
            initial_stop_price=101,
            stop_price=101,
            partial_price=99,
            target_price=98,
            risk_amount=100,
        )
        partial = CandleSnapshot(timestamp_utc=opened_at, open=100, high=100.2, low=98.9, close=99, volume=1)
        self.assertIsNone(_advance_trade(trade, partial, configured))
        self.assertTrue(trade.partial_hit)
        self.assertEqual(trade.remaining_fraction, 0.5)
        self.assertEqual(trade.stop_price, trade.entry_price)
        target = CandleSnapshot(timestamp_utc=opened_at + timedelta(minutes=5), open=99, high=99.2, low=97.9, close=98, volume=1)
        result = _advance_trade(trade, target, configured)
        self.assertIsNotNone(result)
        self.assertEqual(result.gross_pnl, 150)

    def test_historical_replay_rejects_empty_dataset(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires candles"):
            run_historical_replay(self.bot(), [], replay_settings())

    def test_historical_replay_without_candidates_is_causal_and_reports_insufficient_history(self) -> None:
        configured = replay_settings()
        start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        candles = [
            CandleSnapshot(
                timestamp_utc=start + timedelta(minutes=5 * index),
                open=100 + index / 100,
                high=101 + index / 100,
                low=99 + index / 100,
                close=100.5 + index / 100,
                volume=100,
            )
            for index in range(60)
        ]
        with patch(
            "app.backtest.evaluate_decision",
            return_value=SimpleNamespace(metadata={"candidate": False}),
        ) as evaluate:
            replay = run_historical_replay(self.bot(), list(reversed(candles)), configured, days=30)

        self.assertEqual(evaluate.call_count, 1)
        request = evaluate.call_args.args[0]
        self.assertEqual(len(request.candles), 60)
        self.assertEqual(request.candles[0].timestamp_utc, start)
        self.assertEqual(replay.candidates_detected, 0)
        self.assertEqual(replay.candidates_eligible, 0)
        self.assertEqual(replay.trades, [])
        self.assertEqual(replay.validation_status, "insufficient_history")
        self.assertEqual(replay.candles_evaluated, 60)
        self.assertTrue(any("não cobre" in assumption for assumption in replay.assumptions))

    def test_historical_replay_opens_candidate_blocks_overlap_and_closes_at_data_end(self) -> None:
        configured = replay_settings()
        start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        candles = [
            CandleSnapshot(
                timestamp_utc=start + timedelta(minutes=5 * index),
                open=100,
                high=100.2,
                low=99.8,
                close=100,
                volume=100,
            )
            for index in range(65)
        ]
        candidate = DecisionResponse(
            action=DecisionAction.HOLD,
            reason="shadow_candidate",
            entry_price=100,
            stop_loss_price=90,
            partial_take_profit_price=105,
            final_take_profit_price=110,
            partial_close_pct=50,
            risk_amount=25,
            metadata={
                "candidate": True,
                "suggested_action": "buy",
                "suggested_size": 0.1,
                "setup_type": "covered_candidate",
                "max_holding_bars": 20,
                "break_even_trigger_points": 100,
                "break_even_offset_points": 1,
                "trailing_activation_points": 200,
                "trailing_distance_points": 50,
            },
        )
        with patch("app.backtest.evaluate_decision", return_value=candidate):
            replay = run_historical_replay(self.bot(), candles, configured, days=1)

        self.assertEqual(replay.candidates_detected, 6)
        self.assertEqual(replay.candidates_eligible, 1)
        self.assertEqual(replay.blocked_reasons["position_already_open"], 5)
        self.assertEqual(replay.summary.trades_count, 1)
        self.assertIn("saída=data_end", replay.trades[0].note)

    def test_stop_precedes_targets_when_same_candle_touches_both(self) -> None:
        opened_at = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="replay-1",
            setup="test",
            symbol="XAUUSD",
            timeframe="M5",
            side="buy",
            opened_at_utc=opened_at,
            volume_lots=1,
            entry_price=100,
            initial_stop_price=99,
            stop_price=100,
            partial_price=101,
            target_price=102,
            risk_amount=100,
            remaining_fraction=0.5,
            realized_gross_pnl=50,
            partial_hit=True,
        )
        candle = CandleSnapshot(
            timestamp_utc=opened_at + timedelta(minutes=5),
            open=100,
            high=103,
            low=99,
            close=102,
            volume=100,
        )

        result = _advance_trade(trade, candle, replay_settings())

        self.assertIsNotNone(result)
        self.assertEqual(result.rr_realized, 0.5)
        self.assertEqual(result.exit_price, 100)
        self.assertEqual(result.net_pnl, 50)

    def test_happy_gold_break_even_and_trailing_raise_stop_for_next_candle(self) -> None:
        opened_at = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="happy-gold-1",
            setup="happy_gold_zigzag_breakout",
            symbol="XAUUSD",
            timeframe="M5",
            side="buy",
            opened_at_utc=opened_at,
            volume_lots=1,
            entry_price=100,
            initial_stop_price=97.6,
            stop_price=97.6,
            partial_price=110,
            target_price=110,
            risk_amount=240,
            point_size=.01,
            partial_close_pct=100,
            max_holding_bars=2,
            break_even_trigger_points=40,
            break_even_offset_points=0,
            trailing_activation_points=120,
            trailing_distance_points=60,
        )
        favorable = CandleSnapshot(
            timestamp_utc=opened_at + timedelta(minutes=5),
            open=100,
            high=101.3,
            low=99.9,
            close=101.1,
            volume=100,
        )

        self.assertIsNone(_advance_trade(trade, favorable, replay_settings()))
        self.assertAlmostEqual(trade.stop_price, 100.7)

        retracement = CandleSnapshot(
            timestamp_utc=opened_at + timedelta(minutes=10),
            open=101,
            high=101.1,
            low=100.6,
            close=100.8,
            volume=100,
        )
        result = _advance_trade(trade, retracement, replay_settings())

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.exit_price, 100.7)
        self.assertEqual(result.net_pnl, 70)

    def test_sell_stop_is_checked_against_historical_ask(self) -> None:
        configured = replay_settings()
        configured.backtest_spread_points = 10
        opened_at = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="sell-1",
            setup="test",
            symbol="XAUUSD",
            timeframe="M5",
            side="sell",
            opened_at_utc=opened_at,
            volume_lots=1,
            entry_price=100,
            initial_stop_price=101,
            stop_price=101,
            partial_price=99,
            target_price=98,
            risk_amount=100,
        )
        candle = CandleSnapshot(
            timestamp_utc=opened_at + timedelta(minutes=5),
            open=100,
            high=100.95,
            low=99.5,
            close=100,
            volume=100,
        )

        result = _advance_trade(trade, candle, configured)

        self.assertIsNotNone(result)
        self.assertEqual(result.exit_price, 101)
        self.assertLess(result.net_pnl, 0)

    def test_single_target_strategy_closes_full_position_at_half_r(self) -> None:
        opened_at = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
        trade = _OpenTrade(
            trade_id="single-target",
            setup="multitimeframe",
            symbol="XAUUSD",
            timeframe="M5",
            side="buy",
            opened_at_utc=opened_at,
            volume_lots=1,
            entry_price=100,
            initial_stop_price=99,
            stop_price=99,
            partial_price=100.5,
            target_price=100.5,
            risk_amount=100,
            partial_close_pct=100,
        )
        candle = CandleSnapshot(
            timestamp_utc=opened_at + timedelta(minutes=5),
            open=100,
            high=100.6,
            low=99.8,
            close=100.5,
            volume=100,
        )

        result = _advance_trade(trade, candle, replay_settings())

        self.assertIsNotNone(result)
        self.assertEqual(result.rr_realized, .5)


if __name__ == "__main__":
    unittest.main()
