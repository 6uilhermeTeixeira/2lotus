import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .results_store import build_results_snapshot
from .schemas import CandleSnapshot, DecisionRequest, DecisionResponse, HistoricalReplayResponse, MarketChartResponse, ResultsResponse, TickIngestRequest, TradeResult


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS trade_results (
  bot_id TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  opened_at_utc TEXT NOT NULL,
  closed_at_utc TEXT,
  PRIMARY KEY (bot_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_results_bot_closed ON trade_results(bot_id, closed_at_utc);
CREATE TABLE IF NOT EXISTS candles (
  bot_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  PRIMARY KEY (bot_id, symbol, timeframe, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(bot_id, symbol, timeframe, timestamp_utc DESC);
CREATE TABLE IF NOT EXISTS ticks (
  collector_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  time_msc INTEGER NOT NULL,
  sequence INTEGER NOT NULL,
  point_size REAL NOT NULL,
  bid REAL NOT NULL,
  ask REAL NOT NULL,
  last REAL NOT NULL,
  volume INTEGER NOT NULL,
  volume_real REAL NOT NULL,
  flags INTEGER NOT NULL,
  PRIMARY KEY (collector_id, symbol, time_msc, sequence)
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time ON ticks(symbol, time_msc DESC);
CREATE TABLE IF NOT EXISTS decisions (
  bot_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  requested_at_utc TEXT NOT NULL,
  candle_at_utc TEXT,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  latency_ms REAL,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  PRIMARY KEY (bot_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_bot_time ON decisions(bot_id, requested_at_utc DESC);
CREATE TABLE IF NOT EXISTS backtest_snapshots (
  bot_id TEXT NOT NULL,
  period_days INTEGER NOT NULL,
  period_start_utc TEXT NOT NULL,
  period_end_utc TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (bot_id, period_days)
);
"""


class SQLiteStore:
    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def readiness(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def upsert_trade(self, bot_id: str, trade: TradeResult) -> None:
        payload = trade.model_dump(mode="json")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO trade_results(bot_id,trade_id,payload_json,opened_at_utc,closed_at_utc)
                   VALUES(?,?,?,?,?) ON CONFLICT(bot_id,trade_id) DO UPDATE SET
                   payload_json=excluded.payload_json, opened_at_utc=excluded.opened_at_utc,
                   closed_at_utc=excluded.closed_at_utc""",
                (bot_id, trade.trade_id, json.dumps(payload), trade.opened_at_utc.isoformat(), trade.closed_at_utc.isoformat() if trade.closed_at_utc else None),
            )

    def load_results(self, bot_id: str) -> ResultsResponse:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM trade_results WHERE bot_id=? ORDER BY opened_at_utc", (bot_id,)
            ).fetchall()
        return build_results_snapshot([json.loads(row["payload_json"]) for row in rows], f"sqlite:{self.path}:{bot_id}")

    def upsert_candles(self, bot_id: str, symbol: str, timeframe: str, candles: list[CandleSnapshot]) -> None:
        values = [(bot_id, symbol, timeframe, candle.timestamp_utc.isoformat(), candle.open, candle.high, candle.low, candle.close, candle.volume) for candle in candles]
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO candles(bot_id,symbol,timeframe,timestamp_utc,open,high,low,close,volume)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,symbol,timeframe,timestamp_utc) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume""",
                values,
            )

    def load_chart(self, bot_id: str, symbol: str, timeframe: str, limit: int) -> MarketChartResponse:
        with self.connect() as connection:
            candle_rows = connection.execute(
                """SELECT timestamp_utc,open,high,low,close,volume FROM candles
                   WHERE bot_id=? AND symbol=? AND timeframe=? ORDER BY timestamp_utc DESC LIMIT ?""",
                (bot_id, symbol, timeframe, limit),
            ).fetchall()
            trade_rows = connection.execute(
                "SELECT payload_json FROM trade_results WHERE bot_id=? ORDER BY opened_at_utc DESC LIMIT 100", (bot_id,)
            ).fetchall()
        candles = [CandleSnapshot.model_validate(dict(row)) for row in reversed(candle_rows)]
        trades = [TradeResult.model_validate(json.loads(row["payload_json"])) for row in trade_rows]
        return MarketChartResponse(bot_id=bot_id, symbol=symbol, timeframe=timeframe, candles=candles, trades=trades)

    def upsert_ticks(self, payload: TickIngestRequest) -> None:
        values = [(payload.collector_id, payload.symbol, tick.time_msc, tick.sequence, payload.point_size, tick.bid, tick.ask, tick.last, tick.volume, tick.volume_real, tick.flags) for tick in payload.ticks]
        with self.connect() as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO ticks(collector_id,symbol,time_msc,sequence,point_size,bid,ask,last,volume,volume_real,flags)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )

    def record_decision(self, request: DecisionRequest, response: DecisionResponse, latency_ms: float) -> None:
        bot_id = request.bot_id or "default"
        candle_at = request.candles[-1].timestamp_utc.isoformat() if request.candles else None
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO decisions(bot_id,request_id,requested_at_utc,candle_at_utc,action,reason,latency_ms,request_json,response_json)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,request_id) DO UPDATE SET
                   action=excluded.action,reason=excluded.reason,latency_ms=excluded.latency_ms,response_json=excluded.response_json""",
                (bot_id, request.request_id, request.timestamp_utc.isoformat(), candle_at, response.action.value, response.reason, latency_ms, request.model_dump_json(), response.model_dump_json()),
            )

    def load_operations(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM (
                   SELECT d.*, ROW_NUMBER() OVER (
                     PARTITION BY bot_id ORDER BY requested_at_utc DESC, rowid DESC
                   ) AS latest_rank FROM decisions d
                ) ranked WHERE latest_rank=1"""
            ).fetchall()
        statuses = [{
            "bot_id": row["bot_id"], "last_request_at_utc": row["requested_at_utc"],
            "last_attempt_at_utc": row["requested_at_utc"], "last_success_at_utc": row["requested_at_utc"],
            "last_candle_at_utc": row["candle_at_utc"], "last_action": row["action"],
            "last_reason": row["reason"], "last_latency_ms": row["latency_ms"], "consecutive_errors": 0,
        } for row in rows]
        return statuses, []

    def save_backtest_snapshot(self, replay: HistoricalReplayResponse, period_days: int) -> None:
        payload = replay.model_dump(mode="json")
        payload["candles"] = []
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO backtest_snapshots(bot_id,period_days,period_start_utc,period_end_utc,payload_json,updated_at_utc)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id,period_days) DO UPDATE SET
                   period_start_utc=excluded.period_start_utc,period_end_utc=excluded.period_end_utc,
                   payload_json=excluded.payload_json,updated_at_utc=excluded.updated_at_utc""",
                (replay.bot_id, period_days, replay.period_start_utc.isoformat(), replay.period_end_utc.isoformat(), json.dumps(payload), datetime.now(timezone.utc).isoformat()),
            )

    def load_backtest_snapshot(self, bot_id: str, period_days: int) -> HistoricalReplayResponse | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM backtest_snapshots WHERE bot_id=? AND period_days=?", (bot_id, period_days)
            ).fetchone()
        return HistoricalReplayResponse.model_validate(json.loads(row["payload_json"])) if row else None

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("trade_results", "candles", "ticks", "decisions", "backtest_snapshots")}
