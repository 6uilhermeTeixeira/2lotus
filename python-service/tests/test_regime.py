import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.regime import RegimeSnapshot, classify_regime, regime_allows
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


if __name__ == "__main__":
    unittest.main()
