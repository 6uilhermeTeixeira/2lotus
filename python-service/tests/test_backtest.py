import unittest
from datetime import datetime, timedelta, timezone

from app.backtest import _OpenTrade, _advance_trade
from app.schemas import CandleSnapshot
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
