import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.results_store import (
    build_results_snapshot,
    get_bot,
    load_bots_catalog,
    load_results_snapshot,
    load_static_backtest_snapshot,
    resolve_bot_results_file,
    save_static_backtest_snapshot,
)
from app.schemas import HistoricalReplayResponse


def trade(trade_id: str, opened: str, *, closed: str | None, pnl: float, rr: float | None = None) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "setup": "zigzag",
        "side": "buy",
        "opened_at_utc": opened,
        "closed_at_utc": closed,
        "volume_lots": 0.01,
        "entry_price": 2400,
        "exit_price": 2401 if closed else None,
        "net_pnl": pnl,
        "rr_realized": rr,
    }


class ResultsStoreTests(unittest.TestCase):
    def test_build_results_snapshot_aggregates_closed_and_open_trades(self) -> None:
        snapshot = build_results_snapshot(
            [
                trade("win", "2026-09-09T10:00:00Z", closed="2026-09-09T11:00:00Z", pnl=100, rr=2),
                trade("loss", "2026-09-09T12:00:00Z", closed="2026-09-09T13:00:00Z", pnl=-40, rr=-1),
                trade("flat", "2026-09-10T10:00:00Z", closed="2026-09-10T11:00:00Z", pnl=0, rr=0),
                trade("open", "2026-09-10T12:00:00Z", closed=None, pnl=0),
            ],
            "memory",
        )

        self.assertEqual(snapshot.summary.trades_count, 3)
        self.assertEqual(snapshot.summary.open_positions, 1)
        self.assertEqual(snapshot.summary.wins, 1)
        self.assertEqual(snapshot.summary.losses, 1)
        self.assertEqual(snapshot.summary.breakevens, 1)
        self.assertEqual(snapshot.summary.net_pnl, 60)
        self.assertEqual(snapshot.summary.profit_factor, 2.5)
        self.assertEqual(snapshot.summary.win_rate_pct, 33.33)
        self.assertEqual(snapshot.summary.avg_rr, 0.33)
        self.assertEqual([point.net_pnl for point in snapshot.daily_pnl], [60, 0])
        self.assertEqual(snapshot.recent_trades[0].trade_id, "open")

    def test_missing_results_file_returns_empty_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "missing.json"
            snapshot = load_results_snapshot(source)

        self.assertEqual(snapshot.summary.trades_count, 0)
        self.assertEqual(snapshot.source_file, str(source))

    def test_results_store_and_catalog_reject_non_array_payloads(self) -> None:
        with self.assertRaises(ValueError):
            build_results_snapshot({"trade": "invalid"}, "memory")  # type: ignore[arg-type]
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "bots.json"
            catalog.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bots_catalog(catalog)

    def test_catalog_lookup_and_result_path_stay_inside_data_root(self) -> None:
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "bots.json"
            catalog.write_text(
                json.dumps(
                    [
                        {
                            "id": "core-zigzag-v3",
                            "name": "ZigZag",
                            "description": "test",
                            "strategy_profile": "zigzag",
                            "symbol": "XAUUSD",
                            "timeframe": "M5",
                            "results_file": "results/zigzag.json",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            bot = get_bot(catalog, "core-zigzag-v3")

            self.assertIsNotNone(bot)
            assert bot is not None
            self.assertEqual(resolve_bot_results_file(catalog, bot), Path(directory) / "results/zigzag.json")
            self.assertIsNone(get_bot(catalog, "missing"))
            with self.assertRaises(ValueError):
                resolve_bot_results_file(catalog, bot.model_copy(update={"results_file": "../outside.json"}))

    def test_static_backtest_is_compact_when_saved_and_round_trips(self) -> None:
        replay = HistoricalReplayResponse(
            bot_id="core-zigzag-v3",
            symbol="XAUUSD",
            timeframe="M5",
            period_start_utc="2026-09-01T00:00:00Z",
            period_end_utc="2026-09-02T00:00:00Z",
            candles_evaluated=10,
            candidates_detected=1,
            summary={},
            candles=[{"timestamp_utc": "2026-09-01T00:00:00Z", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1}],
        )
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "bots.json"
            target = save_static_backtest_snapshot(catalog, replay, 30)
            loaded = load_static_backtest_snapshot(catalog, replay.bot_id, 30)
            missing = load_static_backtest_snapshot(catalog, replay.bot_id, 31)

        self.assertTrue(target.name.endswith("-30.json"))
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.candles, [])
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
