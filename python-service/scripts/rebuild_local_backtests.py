from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

from app.backtest import run_historical_replay
from app.config import get_settings
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build focused local replays from Dukascopy M5 candles.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--days", type=int, default=1827)
    parser.add_argument("--skip-candle-import", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 1827:
        parser.error("--days must be between 1 and 1827")

    settings = get_settings()
    store = SQLiteStore(settings)
    bots = load_bots_catalog(settings.bots_catalog_path)
    candles = load_dukascopy_m5(args.source)
    print(f"SOURCE candles={len(candles)} start={candles[0].timestamp_utc.isoformat()} end={candles[-1].timestamp_utc.isoformat()}", flush=True)

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
