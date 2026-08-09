import unittest
from datetime import datetime, timezone

from app.growth import simulate_growth, simulate_tiered_growth
from app.schemas import HistoricalReplayResponse, ResultsSummary, RiskTier, TradeResult


class GrowthSimulationTests(unittest.TestCase):
    def test_compounds_realized_r_against_current_balance(self) -> None:
        replay = HistoricalReplayResponse(
            bot_id="bot-16",
            symbol="XAUUSD",
            timeframe="M5",
            risk_per_trade_pct=0.25,
            period_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end_utc=datetime(2026, 1, 3, tzinfo=timezone.utc),
            candles_evaluated=10,
            candidates_detected=2,
            summary=ResultsSummary(trades_count=2),
            trades=[
                TradeResult(trade_id="trade-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy", opened_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), closed_at_utc=datetime(2026, 1, 1, 1, tzinfo=timezone.utc), volume_lots=1, entry_price=1, rr_realized=2),
                TradeResult(trade_id="trade-2", symbol="XAUUSD", timeframe="M5", setup="test", side="sell", opened_at_utc=datetime(2026, 1, 2, tzinfo=timezone.utc), closed_at_utc=datetime(2026, 1, 2, 1, tzinfo=timezone.utc), volume_lots=1, entry_price=1, rr_realized=-1),
            ],
        )

        result = simulate_growth(replay, 10000, 1, label="Simulação")

        self.assertEqual(result.trades_simulated, 2)
        self.assertEqual(result.final_balance, 10098)
        self.assertEqual(result.net_pnl, 98)
        self.assertEqual(result.max_drawdown, 102)
        self.assertEqual([point.balance for point in result.points], [10000, 10200, 10098])

    def test_reduced_risk_stays_locked_after_balance_falls(self) -> None:
        replay = HistoricalReplayResponse(
            bot_id="bot-16", symbol="XAUUSD", timeframe="M5", period_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), period_end_utc=datetime(2026, 1, 4, tzinfo=timezone.utc), candles_evaluated=3, candidates_detected=3, summary=ResultsSummary(trades_count=3),
            trades=[
                TradeResult(trade_id="t-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy", opened_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), closed_at_utc=datetime(2026, 1, 1, 1, tzinfo=timezone.utc), volume_lots=1, entry_price=1, rr_realized=1),
                TradeResult(trade_id="t-2", symbol="XAUUSD", timeframe="M5", setup="test", side="buy", opened_at_utc=datetime(2026, 1, 2, tzinfo=timezone.utc), closed_at_utc=datetime(2026, 1, 2, 1, tzinfo=timezone.utc), volume_lots=1, entry_price=1, rr_realized=-2),
                TradeResult(trade_id="t-3", symbol="XAUUSD", timeframe="M5", setup="test", side="buy", opened_at_utc=datetime(2026, 1, 3, tzinfo=timezone.utc), closed_at_utc=datetime(2026, 1, 3, 1, tzinfo=timezone.utc), volume_lots=1, entry_price=1, rr_realized=1),
            ],
        )
        result = simulate_tiered_growth(replay, 100, [RiskTier(balance_threshold=0, risk_per_trade_pct=10), RiskTier(balance_threshold=110, risk_per_trade_pct=5)], label="Simulação")
        self.assertEqual(result.final_balance, 103.95)
        self.assertIsNotNone(result.milestones[1].reached_at_utc)


if __name__ == "__main__":
    unittest.main()
