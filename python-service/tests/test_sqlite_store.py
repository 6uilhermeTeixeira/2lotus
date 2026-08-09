from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.schemas import CandleSnapshot, DecisionAction, DecisionRequest, DecisionResponse, TickIngestRequest, TradeResult
from app.sqlite_store import SQLiteStore


def store_at(path: Path) -> SQLiteStore:
    return SQLiteStore(SimpleNamespace(sqlite_path=path))


def test_sqlite_persists_market_and_runtime_data_between_connections():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.db"
        store = store_at(path)
        candle = CandleSnapshot(timestamp_utc="2026-08-09T12:00:00Z", open=2400, high=2402, low=2399, close=2401, volume=10)
        trade = TradeResult(
            trade_id="trade-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy",
            opened_at_utc="2026-08-09T12:00:00Z", closed_at_utc="2026-08-09T12:05:00Z",
            volume_lots=.01, entry_price=2400, exit_price=2401, net_pnl=1,
        )
        ticks = TickIngestRequest(
            collector_id="mt5-local", symbol="XAUUSD", point_size=.01,
            ticks=[{"time_msc": 1_786_276_800_000, "sequence": 0, "bid": 2400, "ask": 2400.1}],
        )
        decision_request = DecisionRequest(
            bot_id="core-portfolio", request_id="decision-1", timestamp_utc="2026-08-09T12:00:00Z",
            symbol="XAUUSD", timeframe="M5", bid=2400, ask=2400.1, spread_points=10,
            point_size=.01, value_per_point_per_lot=1,
            account={"login": 123, "balance": 10000, "equity": 10000, "margin_free": 9000},
            candles=[candle],
        )
        store.upsert_candles("core-portfolio", "XAUUSD", "M5", [candle, candle])
        store.upsert_trade("core-portfolio", trade)
        store.upsert_trade("core-portfolio", trade)
        store.upsert_ticks(ticks)
        store.upsert_ticks(ticks)
        store.record_decision(decision_request, DecisionResponse(action=DecisionAction.HOLD, reason="test"), 12.5)

        reopened = store_at(path)
        assert reopened.counts() == {"trade_results": 1, "candles": 1, "ticks": 1, "decisions": 1}
        assert reopened.load_results("core-portfolio").summary.net_pnl == 1
        chart = reopened.load_chart("core-portfolio", "XAUUSD", "M5", 100)
        assert len(chart.candles) == 1
        assert len(chart.trades) == 1
        statuses, _ = reopened.load_operations()
        assert statuses[0]["last_reason"] == "test"
