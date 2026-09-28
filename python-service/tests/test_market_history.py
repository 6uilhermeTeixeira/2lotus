import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from app.local_dataset import DatasetError, DatasetRelease
from app.market_history import load_chart_candles


def release(root: Path, **manifest_updates: object) -> DatasetRelease:
    manifest: dict[str, object] = {
        "instrument": "XAUUSD",
        "coverage_end": "2026-09-10",
        "aggregations": {},
    }
    manifest.update(manifest_updates)
    return DatasetRelease(root=root.resolve(), version="test", manifest=manifest)


class MarketHistoryTests(unittest.TestCase):
    def test_rejects_invalid_resolution_symbol_and_coverage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(DatasetError):
                load_chart_candles(release(root), "XAUUSD", "15m", 1)
            with self.assertRaises(DatasetError):
                load_chart_candles(release(root), "EURUSD", "5m", 1)
            with self.assertRaises(DatasetError):
                load_chart_candles(release(root, coverage_end=None), "XAUUSD", "5m", 1)

    def test_returns_empty_when_no_eligible_aggregation_exists(self) -> None:
        with TemporaryDirectory() as directory:
            candles = load_chart_candles(release(Path(directory)), "XAUUSD", "5m", 7)

        self.assertEqual(candles, [])

    def test_rejects_missing_or_outside_aggregation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_rows = [{"day": "2026-09-10", "path": "missing.parquet"}]
            with self.assertRaises(DatasetError):
                load_chart_candles(release(root, aggregations={"5m": manifest_rows}), "XAUUSD", "5m", 1)

    def test_reads_orders_and_limits_chart_candles_from_parquet(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "candles.parquet"
            connection = duckdb.connect(":memory:")
            try:
                connection.execute(
                    "COPY (SELECT * FROM (VALUES (1788998400000, 10, 12, 9, 11, 2, 3), (1788998460000, 11, 13, 10, 12, 4, 5)) candles(t,o,h,l,c,bid_volume,ask_volume)) TO ? (FORMAT PARQUET)",
                    [str(target)],
                )
            finally:
                connection.close()
            rows = [{"day": "2026-09-10", "path": "candles.parquet"}]

            candles = load_chart_candles(release(root, aggregations={"1m": rows}), "XAUUSD", "1m", 1, limit=1)

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].t, 1788998460000)
        self.assertEqual(candles[0].volume, 9)


if __name__ == "__main__":
    unittest.main()
