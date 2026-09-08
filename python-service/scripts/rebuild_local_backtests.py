from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from app.backtest import run_historical_replay
from app.config import get_settings
from app.local_dataset import DatasetError, DatasetRelease
from app.results_store import load_bots_catalog, save_static_backtest_snapshot
from app.schemas import CandleSnapshot
from app.sqlite_store import SQLiteStore


def load_dukascopy_m5(path: Path) -> list[CandleSnapshot]:
    candles: list[CandleSnapshot] = []
    seen: set[int] = set()
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            timestamp_ms = int(row["timestamp"])
            if timestamp_ms in seen:
                continue
            seen.add(timestamp_ms)
            candles.append(CandleSnapshot(
                timestamp_utc=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=float(row["volume"]),
            ))
    candles.sort(key=lambda candle: candle.timestamp_utc)
    if not candles:
        raise RuntimeError("source CSV has no candles")
    return candles


def load_release_m5(release: DatasetRelease, symbol: str, days: int) -> list[CandleSnapshot]:
    if str(release.manifest.get("instrument", "")).upper() != symbol.upper():
        raise DatasetError(f"dataset ativo não corresponde a {symbol}")
    coverage_end = release.manifest.get("coverage_end")
    if not isinstance(coverage_end, str):
        raise DatasetError("dataset não informa o fim da cobertura")
    cutoff = date.fromisoformat(coverage_end) - timedelta(days=days - 1)
    aggregations = release.manifest.get("aggregations", {})
    entries = aggregations.get("5m", []) if isinstance(aggregations, dict) else []
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        partition_day = entry.get("day")
        if not isinstance(partition_day, str) or date.fromisoformat(partition_day) < cutoff:
            continue
        path = (release.root / entry["path"]).resolve()
        if not path.is_relative_to(release.root) or not path.is_file():
            raise DatasetError(f"agregação M5 ausente ou fora da raiz: {entry['path']}")
        paths.append(str(path))
    if not paths:
        raise DatasetError("nenhuma agregação M5 disponível no período")
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            """SELECT t,o,h,l,c,coalesce(bid_volume,0)+coalesce(ask_volume,0)
               FROM read_parquet(?) ORDER BY t""",
            [paths],
        ).fetchall()
    finally:
        connection.close()
    return [
        CandleSnapshot(
            timestamp_utc=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            open=open_price, high=high_price, low=low_price, close=close_price, volume=volume,
        )
        for timestamp_ms, open_price, high_price, low_price, close_price, volume in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build focused local replays from the current 1lotus Parquet release.")
    parser.add_argument("--source", type=Path, help="Legacy CSV source; CURRENT Parquet is used when omitted.")
    parser.add_argument("--dataset-version", default="CURRENT")
    parser.add_argument("--days", type=int, default=1827)
    parser.add_argument("--skip-candle-import", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 1827:
        parser.error("--days must be between 1 and 1827")

    settings = get_settings()
    store = SQLiteStore(settings)
    bots = load_bots_catalog(settings.bots_catalog_path)
    release = None if args.source else DatasetRelease.open(settings.lotus_data_root, args.dataset_version)
    candles = load_dukascopy_m5(args.source) if args.source else load_release_m5(release, "XAUUSD", args.days)
    source = str(args.source) if args.source else f"parquet:{release.version}"
    print(
        f"SOURCE source={source} spread_points={settings.backtest_spread_points:g} "
        f"candles={len(candles)} start={candles[0].timestamp_utc.isoformat()} end={candles[-1].timestamp_utc.isoformat()}",
        flush=True,
    )

    for bot in bots:
        if not args.skip_candle_import:
            for start in range(0, len(candles), 5000):
                store.upsert_candles(bot.id, bot.symbol, bot.timeframe, candles[start:start + 5000])
        replay = run_historical_replay(bot, candles, settings, days=args.days)
        store.save_backtest_snapshot(replay, args.days)
        save_static_backtest_snapshot(settings.bots_catalog_path, replay, args.days)
        summary = replay.summary
        print(
            f"BOT id={bot.id} trades={summary.trades_count} win_rate={summary.win_rate_pct:.2f} "
            f"net={summary.net_pnl:.2f} profit_factor={summary.profit_factor:.2f} "
            f"drawdown_r={replay.max_drawdown_r:.2f} validation={replay.validation_status}",
            flush=True,
        )


if __name__ == "__main__":
    main()
