import unittest
from datetime import datetime, timedelta, timezone

from app.indicators import atr, ema, ema_series, rsi, stochastic_series, z_score
from app.schemas import CandleSnapshot


class IndicatorTests(unittest.TestCase):
    def candles(self, prices: list[tuple[float, float, float, float]]) -> list[CandleSnapshot]:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            CandleSnapshot(
                timestamp_utc=start + timedelta(minutes=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1,
            )
            for index, (open_price, high, low, close) in enumerate(prices)
        ]

    def test_ema_and_series_follow_same_recursive_calculation(self) -> None:
        values = [10, 12, 11, 15]
        series = ema_series(values, 3)
        self.assertEqual(series, [10, 11, 11, 13])
        self.assertEqual(ema(values, 3), series[-1])
        self.assertEqual(ema_series([], 3), [])

    def test_atr_includes_gaps_from_previous_close(self) -> None:
        candles = self.candles([(10, 11, 9, 10), (15, 16, 14, 15), (14, 15, 13, 14)])
        self.assertAlmostEqual(atr(candles, 2), 4)

    def test_z_score_handles_flat_and_variable_samples(self) -> None:
        self.assertEqual(z_score(10, [5, 5, 5]), 0)
        self.assertGreater(z_score(4, [1, 2, 3]), 1)

    def test_rsi_handles_all_gains_and_mixed_changes(self) -> None:
        self.assertEqual(rsi([1, 2, 3, 4], 3), 100)
        self.assertAlmostEqual(rsi([1, 2, 1, 2], 3), 66.6666666667)

    def test_stochastic_handles_flat_window_and_smoothing(self) -> None:
        flat = self.candles([(10, 10, 10, 10)] * 4)
        smooth_k, signal_d = stochastic_series(flat, period=3, smooth=2)
        self.assertEqual(smooth_k, [50, 50])
        self.assertEqual(signal_d, [50, 50])

        moving = self.candles([(10, 12, 9, 11), (11, 13, 10, 12), (12, 14, 11, 13)])
        smooth_k, signal_d = stochastic_series(moving, period=2, smooth=2)
        self.assertEqual(len(smooth_k), 2)
        self.assertEqual(len(signal_d), 2)
        self.assertGreater(smooth_k[-1], 50)


if __name__ == "__main__":
    unittest.main()
