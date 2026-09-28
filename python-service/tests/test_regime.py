import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.regime import RegimeSnapshot, _adx, _atr_series, _classify_hourly, _hourly_candles, build_regime_timeline, classify_regime, regime_allows, regime_at
from app.schemas import CandleSnapshot


def regime_settings():
    return SimpleNamespace(
        backtest_regime_high_volatility_percentile=90,
        backtest_regime_trend_adx=25,
        backtest_regime_range_adx=20,
    )


def expand_hourly(closes: list[float]) -> list[CandleSnapshot]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    previous = closes[0]
    for hour_index, close in enumerate(closes):
        for part in range(12):
            ratio = (part + 1) / 12
            value = previous + (close - previous) * ratio
            candles.append(
                CandleSnapshot(
                    timestamp_utc=start + timedelta(hours=hour_index, minutes=part * 5),
                    open=value - 0.02,
                    high=value + 0.10,
                    low=value - 0.10,
                    close=value,
                    volume=100,
                )
            )
        previous = close
    return candles


class RegimeTests(unittest.TestCase):
    def test_directional_h1_market_is_classified_as_trend(self) -> None:
        candles = expand_hourly([100 + index * 0.2 for index in range(220)])

        regime = classify_regime(candles, regime_settings())

        self.assertEqual(regime.name, "trend")
        self.assertEqual(regime.direction, "buy")
        self.assertGreaterEqual(regime.adx, 25)

    def test_trend_strategy_requires_matching_h1_direction(self) -> None:
        regime = RegimeSnapshot("trend", "sell", "test", adx=30, atr_percentile=50)

        allowed_sell, _ = regime_allows("macd_trend_crossover", "sell", regime)
        allowed_buy, reason = regime_allows("macd_trend_crossover", "buy", regime)

        self.assertTrue(allowed_sell)
        self.assertFalse(allowed_buy)
        self.assertIn("trend_required", reason)

    def test_mean_reversion_is_only_authorized_in_range(self) -> None:
        allowed, _ = regime_allows("rsi_bollinger_reversion", "buy", RegimeSnapshot("range", "both", "low_adx"))
        blocked, _ = regime_allows("rsi_bollinger_reversion", "buy", RegimeSnapshot("trend", "buy", "high_adx"))

        self.assertTrue(allowed)
        self.assertFalse(blocked)

    def test_hourly_aggregation_discards_incomplete_hours(self) -> None:
        candles = expand_hourly([100, 101])
        candles.pop()
        hourly = _hourly_candles(candles)
        self.assertEqual(len(hourly), 1)
        self.assertEqual(hourly[0].volume, 1200)

    def test_indicator_warmup_and_unknown_policy_fail_closed(self) -> None:
        candle = CandleSnapshot(
            timestamp_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100, high=101, low=99, close=100, volume=1,
        )
        self.assertEqual(_atr_series([candle]), [])
        self.assertIsNone(_adx([candle]))
        self.assertEqual(_classify_hourly([candle], regime_settings()).name, "insufficient_history")
        self.assertEqual(regime_allows("unknown", "buy", RegimeSnapshot("trend", "buy", "test")), (False, "strategy_has_no_regime_policy"))

    def test_classifier_covers_high_volatility_range_and_transition(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        hourly = [
            CandleSnapshot(
                timestamp_utc=start + timedelta(hours=index),
                open=100 + index * 0.01,
                high=101 + index * 0.01,
                low=99 + index * 0.01,
                close=100 + index * 0.01,
                volume=1,
            )
            for index in range(205)
        ]
        with patch("app.regime._adx", return_value=30), patch("app.regime._atr_series", return_value=[1] * 99 + [2]):
            self.assertEqual(_classify_hourly(hourly, regime_settings()).name, "high_volatility")
        with patch("app.regime._adx", return_value=10), patch("app.regime._atr_series", return_value=[1] * 100):
            ranged = _classify_hourly(hourly, regime_settings())
            self.assertEqual((ranged.name, ranged.direction), ("range", "both"))
        with patch("app.regime._adx", return_value=22), patch("app.regime._atr_series", return_value=[1] * 100):
            self.assertEqual(_classify_hourly(hourly, regime_settings()).name, "transition")

    def test_timeline_is_causal_before_and_after_first_snapshot(self) -> None:
        candles = expand_hourly([100 + index * 0.01 for index in range(206)])
        timeline = build_regime_timeline(candles, regime_settings())
        before = regime_at(timeline, candles[0].timestamp_utc)
        after = regime_at(timeline, timeline[0][0])
        self.assertEqual(before.name, "insufficient_history")
        self.assertNotEqual(after.reason, "requires_200_complete_h1_candles")


if __name__ == "__main__":
    unittest.main()
