import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.schemas import CandleSnapshot, DecisionAction, DecisionRequest, DecisionResponse, HistoricalReplayResponse, TickIngestRequest, TradeResult
from app.sqlite_store import SQLiteStore


def store_at(path: Path) -> SQLiteStore:
    return SQLiteStore(SimpleNamespace(sqlite_path=path))


def candle(close: float = 2401) -> CandleSnapshot:
    return CandleSnapshot(timestamp_utc="2026-08-09T12:00:00Z", open=2400, high=2402, low=2399, close=close, volume=10)


def decision_request(request_id: str) -> DecisionRequest:
    return DecisionRequest(
        bot_id="core-combined-v3", request_id=request_id, timestamp_utc="2026-08-09T12:00:00Z",
        symbol="XAUUSD", timeframe="M5", bid=2400, ask=2400.1, spread_points=10,
        point_size=0.01, value_per_point_per_lot=1,
        account={"login": 123, "balance": 10000, "equity": 10000, "margin_free": 9000},
        candles=[candle()],
    )


def replay() -> HistoricalReplayResponse:
    return HistoricalReplayResponse(
        bot_id="core-combined-v3", symbol="XAUUSD", timeframe="M5",
        period_start_utc="2026-08-01T00:00:00Z", period_end_utc="2026-08-31T00:00:00Z",
        candles_evaluated=100, candidates_detected=2, summary={}, candles=[candle()],
    )


class SQLiteStoreTests(unittest.TestCase):
    def test_persists_and_upserts_market_and_runtime_data(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = store_at(path)
            trade = TradeResult(
                trade_id="trade-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy",
                opened_at_utc="2026-08-09T12:00:00Z", closed_at_utc="2026-08-09T12:05:00Z",
                volume_lots=0.01, entry_price=2400, exit_price=2401, net_pnl=1,
            )
            ticks = TickIngestRequest(
                collector_id="mt5-local", symbol="XAUUSD", point_size=0.01,
                ticks=[{"time_msc": 1_786_276_800_000, "sequence": 0, "bid": 2400, "ask": 2400.1}],
            )
            store.upsert_candles("core-combined-v3", "XAUUSD", "M5", [candle(), candle(2401.5)])
            store.upsert_trade("core-combined-v3", trade)
            store.upsert_trade("core-combined-v3", trade.model_copy(update={"net_pnl": 2}))
            store.upsert_ticks(ticks)
            store.upsert_ticks(ticks)
            store.record_decision(decision_request("decision-1"), DecisionResponse(action=DecisionAction.HOLD, reason="test"), 12.5)

            reopened = store_at(path)
            self.assertEqual(reopened.counts(), {"trade_results": 1, "candles": 1, "ticks": 1, "decisions": 1, "backtest_snapshots": 0})
            self.assertEqual(reopened.load_results("core-combined-v3").summary.net_pnl, 2)
            chart = reopened.load_chart("core-combined-v3", "XAUUSD", "M5", 100)
            self.assertEqual(chart.candles[0].close, 2401.5)
            self.assertEqual(len(chart.trades), 1)
            statuses, failures = reopened.load_operations()
            self.assertEqual(statuses[0]["last_reason"], "test")
            self.assertEqual(failures, [])
            reopened.readiness()

    def test_decision_upsert_updates_without_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            store = store_at(Path(directory) / "runtime.db")
            request = decision_request("same-request")
            store.record_decision(request, DecisionResponse(action=DecisionAction.HOLD, reason="first"), 10)
            store.record_decision(request, DecisionResponse(action=DecisionAction.BUY, reason="updated"), 20)
            statuses, _ = store.load_operations()
            self.assertEqual(store.counts()["decisions"], 1)

        self.assertEqual(statuses[0]["last_action"], "buy")
        self.assertEqual(statuses[0]["last_reason"], "updated")
        self.assertEqual(statuses[0]["last_latency_ms"], 20)

    def test_operations_returns_one_status_per_bot_on_timestamp_tie(self) -> None:
        with TemporaryDirectory() as directory:
            store = store_at(Path(directory) / "runtime.db")
            store.record_decision(decision_request("first"), DecisionResponse(action=DecisionAction.HOLD, reason="first"), 1)
            store.record_decision(decision_request("second"), DecisionResponse(action=DecisionAction.HOLD, reason="second"), 2)
            statuses, _ = store.load_operations()

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["last_reason"], "second")

    def test_backtest_snapshot_is_compact_upserted_and_loaded_by_period(self) -> None:
        with TemporaryDirectory() as directory:
            store = store_at(Path(directory) / "runtime.db")
            first = replay()
            store.save_backtest_snapshot(first, 30)
            store.save_backtest_snapshot(first.model_copy(update={"candidates_detected": 3}), 30)
            loaded = store.load_backtest_snapshot(first.bot_id, 30)
            missing = store.load_backtest_snapshot(first.bot_id, 90)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.candidates_detected, 3)
        self.assertEqual(loaded.candles, [])
        self.assertIsNone(missing)

    def test_empty_store_returns_empty_results_and_chart(self) -> None:
        with TemporaryDirectory() as directory:
            store = store_at(Path(directory) / "runtime.db")
            results = store.load_results("missing")
            chart = store.load_chart("missing", "XAUUSD", "M5", 10)

        self.assertEqual(results.summary.trades_count, 0)
        self.assertEqual(chart.candles, [])
        self.assertEqual(chart.trades, [])


if __name__ == "__main__":
    unittest.main()
