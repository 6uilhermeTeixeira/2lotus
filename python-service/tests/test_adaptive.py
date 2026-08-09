import unittest
from datetime import date, datetime, timezone

from app.adaptive import build_adaptive_schedule, select_adaptive_component, summarize_outcomes
from app.schemas import TradeResult


def trade(trade_id: str, closed_at: datetime, outcome_r: float) -> TradeResult:
    return TradeResult(
        trade_id=trade_id,
        symbol="XAUUSD",
        timeframe="M5",
        setup="test",
        side="buy",
        opened_at_utc=closed_at,
        closed_at_utc=closed_at,
        volume_lots=.1,
        entry_price=100,
        rr_realized=outcome_r,
    )


class AdaptiveSelectorTests(unittest.TestCase):
    def test_health_requires_eight_trades_positive_expectancy_and_pf_1_10(self) -> None:
        insufficient = summarize_outcomes([1] * 7)
        eligible = summarize_outcomes([1] * 5 + [-1] * 3)

        self.assertFalse(insufficient["eligible"])
        self.assertTrue(eligible["eligible"])
        self.assertEqual(eligible["closed_trades"], 8)

    def test_selector_uses_highest_eligible_expectancy(self) -> None:
        selected = select_adaptive_component(
            {
                "bot-3": {"eligible": True, "expectancy_r": .10},
                "bot-6": {"eligible": False, "expectancy_r": .40},
                "bot-7": {"eligible": True, "expectancy_r": .25},
            }
        )

        self.assertEqual(selected, "bot-7")

    def test_daily_schedule_does_not_use_trade_closed_during_same_day(self) -> None:
        rows = [
            trade(f"prior-{index}", datetime(2026, 7, 30, index, tzinfo=timezone.utc), 1)
            for index in range(8)
        ]
        rows.append(trade("same-day-loss", datetime(2026, 7, 31, 10, tzinfo=timezone.utc), -20))

        schedule = build_adaptive_schedule({"bot-7": rows}, date(2026, 7, 31), date(2026, 8, 1))

        self.assertEqual(schedule[date(2026, 7, 31)]["active_component"], "bot-7")
        self.assertIsNone(schedule[date(2026, 8, 1)]["active_component"])


if __name__ == "__main__":
    unittest.main()
