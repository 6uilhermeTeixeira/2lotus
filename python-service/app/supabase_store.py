import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .adaptive import ADAPTIVE_COMPONENTS, ADAPTIVE_WINDOW_DAYS, summarize_outcomes, utc_day_start
from .config import Settings
from .results_store import build_results_snapshot
from .schemas import BotCatalogItem, CandleSnapshot, DecisionRequest, DecisionResponse, HistoricalReplayResponse, MarketChartResponse, ResultsResponse, TickIngestRequest, TradeResult
from .telemetry import StepTimer, emit_event


class SupabaseRequestError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class SupabaseStore:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_results_configured:
            raise RuntimeError("Supabase results store is not configured")
        self.base_url = (settings.supabase_url or "").rstrip("/")
        self.key = settings.supabase_service_role_key or ""
        self.settings = settings

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: object | None = None,
        prefer: str | None = None,
        idempotent: bool = False,
        run_id: str = "supabase-background",
    ) -> object:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, default=str).encode("utf-8")
        if prefer:
            headers["Prefer"] = prefer
        safe_to_retry = method == "GET" or idempotent or bool(prefer and "resolution=" in prefer)
        attempts = getattr(self.settings, "supabase_retry_attempts", 1) if safe_to_retry else 1
        resource = path.split("?", 1)[0]
        for attempt in range(1, attempts + 1):
            request = Request(f"{self.base_url}/rest/v1/{path}", data=body, headers=headers, method=method)
            timer = StepTimer(run_id, f"supabase_{method.lower()}_{resource}", integration="supabase")
            try:
                with urlopen(request, timeout=getattr(self.settings, "supabase_timeout_seconds", 10.0)) as response:
                    response_body = response.read().decode("utf-8")
                timer.finish(attempt=attempt)
                return json.loads(response_body) if response_body else None
            except HTTPError as error:
                retryable = error.code == 429 or error.code >= 500
                error_code = "rate_limit" if error.code == 429 else "auth_error" if error.code in {401, 403} else "http_error"
                timer.finish(status="failed", attempt=attempt, error_code=error_code)
                failure = SupabaseRequestError(
                    f"Supabase request failed with status {error.code}",
                    code=error_code,
                    retryable=retryable,
                    status_code=error.code,
                )
            except (URLError, TimeoutError) as error:
                error_code = "timeout" if isinstance(getattr(error, "reason", None), TimeoutError) or isinstance(error, TimeoutError) else "network_error"
                timer.finish(status="failed", attempt=attempt, error_code=error_code)
                failure = SupabaseRequestError("Supabase request could not be completed", code=error_code, retryable=True)
            if not failure.retryable or attempt >= attempts:
                raise failure
            emit_event(run_id=run_id, step=f"supabase_{resource}", status="retry", integration="supabase", error_code=failure.code, attempt=attempt)
            time.sleep(0.05 * (2 ** (attempt - 1)))
        raise SupabaseRequestError("Supabase request failed", code="unknown")

    def list_bots(self, active: bool | None = True) -> list[BotCatalogItem]:
        active_filter = "" if active is None else f"active=eq.{str(active).lower()}&"
        rows = self._request(f"bots?{active_filter}select=id,name,description,strategy_profile,strategy_description,symbol,timeframe,active&order=id")
        return [BotCatalogItem.model_validate(row) for row in rows if isinstance(row, dict)]

    def load_results(self, bot_id: str) -> ResultsResponse:
        query = urlencode({"bot_id": f"eq.{bot_id}", "select": "*", "order": "closed_at_utc.desc.nullslast"})
        rows = self._request(f"trade_results?{query}")
        real_trades = rows if isinstance(rows, list) else []
        return build_results_snapshot(
            real_trades + self._load_paper_results(bot_id) + self._load_tick_shadow_results(bot_id),
            f"supabase:trades:{bot_id}",
        )

    def save_backtest_snapshot(self, replay: HistoricalReplayResponse, period_days: int) -> None:
        compact_payload = replay.model_dump(mode="json")
        compact_payload["candles"] = []
        self._request(
            "backtest_snapshots?on_conflict=bot_id,period_days",
            method="POST",
            payload={
                "bot_id": replay.bot_id,
                "period_days": period_days,
                "period_start_utc": replay.period_start_utc.isoformat(),
                "period_end_utc": replay.period_end_utc.isoformat(),
                "strategy_version": replay.strategy_version,
                "settings_fingerprint": replay.settings_fingerprint,
                "market_data_through_utc": replay.period_end_utc.isoformat(),
                "payload": compact_payload,
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def load_backtest_snapshot(self, bot_id: str, period_days: int) -> HistoricalReplayResponse | None:
        query = urlencode({"bot_id": f"eq.{bot_id}", "period_days": f"eq.{period_days}", "select": "payload", "limit": "1"})
        rows = self._request(f"backtest_snapshots?{query}")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        payload = rows[0].get("payload")
        return HistoricalReplayResponse.model_validate(payload) if isinstance(payload, dict) else None

    def _load_paper_results(self, bot_id: str) -> list[dict[str, object]]:
        paper_query = urlencode(
            {
                "bot_id": f"eq.{bot_id}",
                "variant": "eq.technical",
                "select": "*",
                "order": "opened_at_utc.desc",
                "limit": "1000",
            }
        )
        signal_query = urlencode(
            {
                "bot_id": f"eq.{bot_id}",
                "select": "signal_key,response",
                "limit": "1000",
            }
        )
        paper_rows = self._request(f"paper_trades?{paper_query}")
        signal_rows = self._request(f"strategy_signals?{signal_query}")
        responses = {
            str(row["signal_key"]): row.get("response", {})
            for row in signal_rows if isinstance(row, dict) and row.get("signal_key")
        } if isinstance(signal_rows, list) else {}
        results: list[dict[str, object]] = []
        for row in paper_rows if isinstance(paper_rows, list) else []:
            if not isinstance(row, dict):
                continue
            signal_key = str(row.get("signal_key", ""))
            response = responses.get(signal_key, {})
            response = response if isinstance(response, dict) else {}
            metadata = response.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            risk_amount = float(response.get("risk_amount") or 0)
            outcome = float(row["outcome_r"]) if row.get("outcome_r") is not None else None
            entry = float(row["entry_price"])
            if outcome is None:
                exit_price = None
            elif outcome < 0:
                exit_price = float(row["initial_stop_price"])
            elif outcome >= 1:
                exit_price = float(row["target_price"])
            else:
                exit_price = entry
            setup_type = str(metadata.get("setup_type", "strategy"))
            results.append(
                {
                    "trade_id": f"paper:{signal_key}",
                    "symbol": str(response.get("symbol") or "XAUUSD"),
                    "timeframe": str(response.get("timeframe") or "M5"),
                    "setup": f"{setup_type}_paper_trade",
                    "side": row["side"],
                    "opened_at_utc": row["opened_at_utc"],
                    "closed_at_utc": row.get("closed_at_utc"),
                    "volume_lots": float(metadata.get("suggested_size") or 0),
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "stop_loss_price": float(row["initial_stop_price"]),
                    "take_profit_price": float(row["target_price"]),
                    "risk_amount": risk_amount,
                    "gross_pnl": (outcome or 0) * risk_amount,
                    "fees": 0,
                    "net_pnl": (outcome or 0) * risk_amount,
                    "rr_realized": outcome,
                    "note": "Paper trade técnico; não foi enviado ao mercado.",
                }
            )
        return results

    def _load_tick_shadow_results(self, bot_id: str) -> list[dict[str, object]]:
        query = urlencode({
            "bot_id": f"eq.{bot_id}", "select": "*",
            "order": "opened_at_utc.desc", "limit": "1000",
        })
        rows = self._request(f"tick_shadow_trades?{query}")
        results: list[dict[str, object]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            initial_risk = abs(float(row["entry_price"]) - float(row["initial_stop_price"]))
            initial_risk_pips = initial_risk / float(row["pip_size"])
            risk_amount = initial_risk_pips * float(row["value_per_pip"])
            net_pnl = float(row.get("net_pnl") or 0)
            results.append({
                "trade_id": f"tick-shadow:{row['signal_key']}",
                "symbol": row["symbol"], "timeframe": "TICK",
                "setup": "happy_gold_zigzag_structural_tick_shadow",
                "side": row["side"], "opened_at_utc": row["opened_at_utc"],
                "closed_at_utc": row.get("closed_at_utc"),
                "volume_lots": float(row.get("volume_lots") or .10),
                "entry_price": float(row["entry_price"]),
                "exit_price": float(row["exit_price"]) if row.get("exit_price") is not None else None,
                "stop_loss_price": float(row["initial_stop_price"]),
                "take_profit_price": float(row["target_price"]) if row.get("target_price") is not None else None,
                "risk_amount": risk_amount,
                "gross_pnl": net_pnl, "fees": 0, "net_pnl": net_pnl,
                "rr_realized": net_pnl / risk_amount if risk_amount and row.get("closed_at_utc") else None,
                "note": f"Shadow tick; saída={row.get('exit_reason') or 'open'}. Nenhuma ordem enviada ao mercado.",
            })
        return results

    def upsert_trade(self, bot_id: str, trade: TradeResult) -> None:
        payload = trade.model_dump(mode="json")
        payload["bot_id"] = bot_id
        payload["raw_payload"] = trade.model_dump(mode="json")
        self._request(
            "trade_results?on_conflict=bot_id,trade_id",
            method="POST",
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def upsert_candles(self, bot_id: str, symbol: str, timeframe: str, candles: list[CandleSnapshot]) -> None:
        if not candles:
            return
        payload = [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp_utc": candle.timestamp_utc.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]
        for start in range(0, len(payload), 1000):
            batch = payload[start:start + 1000]
            self._request(
                "market_history?on_conflict=symbol,timeframe,timestamp_utc",
                method="POST",
                payload=batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )

    def upsert_ticks(self, batch: TickIngestRequest) -> None:
        payload = [
            {
                "collector_id": batch.collector_id,
                "symbol": batch.symbol,
                "time_msc": tick.time_msc,
                "sequence": tick.sequence,
                "bid": tick.bid,
                "ask": tick.ask,
                "last": tick.last,
                "volume": tick.volume,
                "volume_real": tick.volume_real,
                "flags": tick.flags,
                "point_size": batch.point_size,
            }
            for tick in batch.ticks
        ]
        for start in range(0, len(payload), 500):
            self._request(
                "market_ticks?on_conflict=collector_id,symbol,time_msc,sequence",
                method="POST",
                payload=payload[start:start + 500],
                prefer="resolution=ignore-duplicates,return=minimal",
            )

    def record_tick_shadow_trade(
        self, signal_key: str, payload: DecisionRequest, response: DecisionResponse
    ) -> bool:
        if not payload.bot_id or response.entry_price is None or response.stop_loss_price is None:
            return False
        query = urlencode({
            "bot_id": f"eq.{payload.bot_id}", "status": "eq.open",
            "select": "signal_key", "limit": "1",
        })
        rows = self._request(f"tick_shadow_trades?{query}")
        if isinstance(rows, list) and rows:
            return False
        side = str(response.metadata.get("suggested_action", "buy"))
        pip_size = 0.01
        value_per_pip = payload.value_per_point_per_lot * (pip_size / payload.point_size) * float(
            response.metadata.get("suggested_size", 0.10)
        )
        self._request(
            "tick_shadow_trades?on_conflict=signal_key",
            method="POST",
            payload={
                "signal_key": signal_key, "bot_id": payload.bot_id,
                "symbol": payload.symbol, "side": side, "status": "open",
                "opened_at_utc": payload.timestamp_utc.isoformat(),
                "opened_time_msc": int(payload.timestamp_utc.timestamp() * 1000),
                "entry_price": response.entry_price,
                "volume_lots": float(response.metadata.get("suggested_size", .10)),
                "initial_stop_price": response.stop_loss_price,
                "stop_price": response.stop_loss_price,
                "target_price": response.final_take_profit_price,
                "best_price": response.entry_price,
                "pip_size": pip_size, "value_per_pip": value_per_pip,
                "break_even_trigger_pips": response.metadata.get("break_even_trigger_pips", 40),
                "break_even_offset_pips": response.metadata.get("break_even_offset_pips", 1),
                "trailing_activation_pips": response.metadata.get("trailing_activation_pips", 40),
                "trailing_distance_pips": response.metadata.get("trailing_distance_pips", 20),
                "max_holding_seconds": response.metadata.get("max_holding_seconds", 60),
            },
            prefer="resolution=ignore-duplicates,return=minimal",
        )
        recent_query = urlencode({
            "symbol": f"eq.{payload.symbol}",
            "time_msc": f"gte.{int(payload.timestamp_utc.timestamp() * 1000)}",
            "select": "time_msc,bid,ask,last,volume,volume_real,flags",
            "order": "time_msc.asc", "limit": "2000",
        })
        recent_rows = self._request(f"market_ticks?{recent_query}")
        if isinstance(recent_rows, list) and recent_rows:
            replay_ticks = []
            seen: set[tuple[int, float, float]] = set()
            for row in recent_rows:
                if not isinstance(row, dict):
                    continue
                key = (int(row["time_msc"]), float(row["bid"]), float(row["ask"]))
                if key in seen:
                    continue
                seen.add(key)
                replay_ticks.append({
                    "time_msc": key[0], "sequence": len(replay_ticks),
                    "bid": key[1], "ask": key[2],
                    "last": float(row.get("last") or 0),
                    "volume": int(row.get("volume") or 0),
                    "volume_real": float(row.get("volume_real") or 0),
                    "flags": int(row.get("flags") or 0),
                })
            if replay_ticks:
                self.advance_tick_shadow_trades(TickIngestRequest(
                    collector_id="shadow-backfill", symbol=payload.symbol,
                    point_size=payload.point_size, ticks=replay_ticks,
                ))
        return True

    @staticmethod
    def _advance_tick_shadow_row(
        row: dict[str, object], batch: TickIngestRequest
    ) -> dict[str, object]:
        side = str(row["side"])
        direction = 1 if side == "buy" else -1
        entry = float(row["entry_price"])
        pip_size = float(row["pip_size"])
        stop = float(row["stop_price"])
        best = float(row["best_price"])
        target = float(row["target_price"]) if row.get("target_price") is not None else None
        opened_msc = int(row["opened_time_msc"])
        max_holding_msc = int(row["max_holding_seconds"]) * 1000
        exit_price: float | None = None
        exit_msc: int | None = None
        exit_reason: str | None = None
        for tick in batch.ticks:
            if tick.time_msc < opened_msc:
                continue
            executable = tick.bid if side == "buy" else tick.ask
            best = max(best, executable) if side == "buy" else min(best, executable)
            favorable_pips = direction * (best - entry) / pip_size
            if favorable_pips >= float(row["trailing_activation_pips"]):
                trailing_stop = best - direction * float(row["trailing_distance_pips"]) * pip_size
                stop = max(stop, trailing_stop) if side == "buy" else min(stop, trailing_stop)
            if favorable_pips >= float(row["break_even_trigger_pips"]):
                break_even = entry + direction * float(row["break_even_offset_pips"]) * pip_size
                stop = max(stop, break_even) if side == "buy" else min(stop, break_even)
            stopped = executable <= stop if side == "buy" else executable >= stop
            targeted = target is not None and (executable >= target if side == "buy" else executable <= target)
            expired = tick.time_msc - opened_msc >= max_holding_msc
            if stopped or targeted or expired:
                exit_price, exit_msc = executable, tick.time_msc
                exit_reason = "stop" if stopped else "target" if targeted else "time"
                break
        patch: dict[str, object] = {
            "stop_price": stop, "best_price": best,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if exit_price is not None and exit_msc is not None and exit_reason is not None:
            pnl_pips = direction * (exit_price - entry) / pip_size
            patch.update({
                "status": "closed", "closed_time_msc": exit_msc,
                "closed_at_utc": datetime.fromtimestamp(exit_msc / 1000, timezone.utc).isoformat(),
                "exit_price": exit_price, "exit_reason": exit_reason,
                "pnl_pips": pnl_pips,
                "net_pnl": pnl_pips * float(row["value_per_pip"]),
            })
        return patch

    def advance_tick_shadow_trades(self, batch: TickIngestRequest) -> None:
        query = urlencode({
            "symbol": f"eq.{batch.symbol}", "status": "eq.open", "select": "*",
        })
        rows = self._request(f"tick_shadow_trades?{query}")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            patch = self._advance_tick_shadow_row(row, batch)
            filters = urlencode({"signal_key": f"eq.{row['signal_key']}", "status": "eq.open"})
            self._request(
                f"tick_shadow_trades?{filters}", method="PATCH", payload=patch,
                prefer="return=minimal",
            )

    def advance_paper_trades(self, bot_id: str, candles: list[CandleSnapshot]) -> None:
        if not candles:
            return
        candle = candles[-1]
        rows = self._request(f"paper_trades?bot_id=eq.{bot_id}&status=eq.open&select=*")
        for trade in rows if isinstance(rows, list) else []:
            opened_at = datetime.fromisoformat(str(trade["opened_at_utc"]))
            if candle.timestamp_utc <= opened_at:
                continue
            buy = trade["side"] == "buy"
            stop = float(trade["stop_price"])
            entry = float(trade["entry_price"])
            partial = float(trade["partial_price"])
            target = float(trade["target_price"])
            initial_stop = float(trade["initial_stop_price"])
            risk = abs(entry - initial_stop)
            partial_fraction = float(trade.get("partial_close_pct", 50)) / 100
            direction = 1 if buy else -1
            partial_r = direction * (partial - entry) / risk if risk else 0
            target_r = direction * (target - entry) / risk if risk else 0
            patch: dict[str, object] = {}
            # Conservative intrabar rule: a stop has precedence over all targets.
            stopped = candle.low <= stop if buy else candle.high >= stop
            if stopped:
                outcome_r = partial_fraction * partial_r if trade["partial_hit"] else -1
                patch = {"status": "closed", "closed_at_utc": candle.timestamp_utc.isoformat(), "outcome_r": outcome_r}
            elif not trade["partial_hit"] and (candle.high >= partial if buy else candle.low <= partial):
                if partial_fraction >= 1:
                    patch = {"partial_hit": True, "status": "closed", "closed_at_utc": candle.timestamp_utc.isoformat(), "outcome_r": partial_r}
                else:
                    patch = {"partial_hit": True, "stop_price": entry}
            elif trade["partial_hit"] and (candle.high >= target if buy else candle.low <= target):
                outcome_r = partial_fraction * partial_r + (1 - partial_fraction) * target_r
                patch = {"status": "closed", "closed_at_utc": candle.timestamp_utc.isoformat(), "outcome_r": outcome_r}
            elif trade.get("expires_at_utc") and candle.timestamp_utc + timedelta(minutes=5) >= datetime.fromisoformat(str(trade["expires_at_utc"])):
                outcome_r = direction * (candle.close - entry) / risk if risk else 0
                patch = {"status": "closed", "closed_at_utc": candle.timestamp_utc.isoformat(), "outcome_r": outcome_r}
            elif risk and trade.get("point_size") and (
                trade.get("break_even_trigger_points") is not None or trade.get("trailing_activation_points") is not None
            ):
                point_size = float(trade["point_size"])
                favorable_price = candle.high if buy else candle.low
                favorable_points = direction * (favorable_price - entry) / point_size
                next_stop = stop
                break_even_trigger = trade.get("break_even_trigger_points")
                break_even_offset = float(trade.get("break_even_offset_points") or 0)
                trailing_activation = trade.get("trailing_activation_points")
                trailing_distance = trade.get("trailing_distance_points")
                if break_even_trigger is not None and favorable_points >= float(break_even_trigger):
                    break_even_stop = entry + direction * break_even_offset * point_size
                    next_stop = max(next_stop, break_even_stop) if buy else min(next_stop, break_even_stop)
                if trailing_activation is not None and trailing_distance is not None and favorable_points >= float(trailing_activation):
                    trailing_stop = favorable_price - direction * float(trailing_distance) * point_size
                    next_stop = max(next_stop, trailing_stop) if buy else min(next_stop, trailing_stop)
                if next_stop != stop:
                    patch = {"stop_price": next_stop}
            if patch:
                filters = urlencode({"signal_key": f"eq.{trade['signal_key']}", "variant": f"eq.{trade['variant']}"})
                self._request(f"paper_trades?{filters}", method="PATCH", payload=patch, prefer="return=minimal")

    def record_strategy_candidate(self, payload: DecisionRequest, response: DecisionResponse) -> str | None:
        if not payload.bot_id or not payload.candles or response.entry_price is None or response.stop_loss_price is None:
            return None
        setup = str(response.metadata.get("setup_type", "fimath"))
        suggested_action = str(response.metadata.get("suggested_action", ""))
        candle_timestamp = payload.candles[-1].timestamp_utc
        signal_timestamp = response.metadata.get("signal_timestamp_utc", candle_timestamp.isoformat())
        signal_key = f"{payload.bot_id}:{payload.timeframe}:{signal_timestamp}:{suggested_action}:{setup}"
        candidate = {
            "signal_key": signal_key,
            "bot_id": payload.bot_id,
            "candle_timestamp_utc": signal_timestamp,
            "last_evaluated_at_utc": payload.timestamp_utc.isoformat(),
            "suggested_action": suggested_action,
            "setup_type": setup,
            "flow_status": str(response.metadata.get("flow_status", "flow_unavailable")),
            "flow_confirmed": bool(response.metadata.get("flow_confirmed", False)),
            "flow_contradicted": bool(response.metadata.get("flow_contradicted", False)),
            "book_z": response.metadata.get("book_z"),
            "tick_z": response.metadata.get("tick_z"),
            "flow_score": response.metadata.get("flow_score"),
            "spread_points": payload.spread_points,
            "payload": payload.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
        }
        self._request(
            "strategy_signals?on_conflict=signal_key",
            method="POST",
            payload=candidate,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return signal_key

    def record_paper_trade(self, signal_key: str, payload: DecisionRequest, response: DecisionResponse, variant: str) -> bool:
        if not payload.bot_id or response.entry_price is None or response.stop_loss_price is None:
            return False
        open_query = urlencode(
            {
                "bot_id": f"eq.{payload.bot_id}",
                "variant": f"eq.{variant}",
                "status": "eq.open",
                "select": "signal_key",
                "limit": "1",
            }
        )
        open_rows = self._request(f"paper_trades?{open_query}")
        if isinstance(open_rows, list) and open_rows:
            return False
        self._request("paper_trades?on_conflict=signal_key,variant", method="POST", payload={
            "signal_key": signal_key, "variant": variant, "bot_id": payload.bot_id, "side": str(response.metadata.get("suggested_action", "buy")),
            "entry_price": response.entry_price, "initial_stop_price": response.stop_loss_price, "stop_price": response.stop_loss_price,
            "partial_price": response.partial_take_profit_price, "target_price": response.final_take_profit_price,
            "partial_close_pct": response.partial_close_pct,
            "point_size": payload.point_size,
            "break_even_trigger_points": response.metadata.get("break_even_trigger_points"),
            "break_even_offset_points": response.metadata.get("break_even_offset_points"),
            "trailing_activation_points": response.metadata.get("trailing_activation_points"),
            "trailing_distance_points": response.metadata.get("trailing_distance_points"),
            "expires_at_utc": (
                payload.timestamp_utc + timedelta(minutes=5 * int(response.metadata["max_holding_bars"]))
            ).isoformat() if "max_holding_bars" in response.metadata else None,
            "opened_at_utc": payload.timestamp_utc.isoformat(),
        }, prefer="resolution=ignore-duplicates,return=minimal")
        return True

    def closed_paper_trade_count(self, bot_id: str, variant: str) -> int:
        query = urlencode({"bot_id": f"eq.{bot_id}", "variant": f"eq.{variant}", "status": "eq.closed", "select": "signal_key"})
        rows = self._request(f"paper_trades?{query}")
        return len(rows) if isinstance(rows, list) else 0

    def load_adaptive_component_health(self, timestamp_utc: datetime) -> dict[str, dict[str, object]]:
        """Return the frozen UTC-day health snapshot used by Bot 9."""
        day_start = utc_day_start(timestamp_utc)
        window_start = day_start - timedelta(days=ADAPTIVE_WINDOW_DAYS)
        health: dict[str, dict[str, object]] = {}
        for component in ADAPTIVE_COMPONENTS:
            query = urlencode(
                [
                    ("bot_id", f"eq.{component}"),
                    ("variant", "eq.technical"),
                    ("status", "eq.closed"),
                    ("closed_at_utc", f"gte.{window_start.isoformat()}"),
                    ("closed_at_utc", f"lt.{day_start.isoformat()}"),
                    ("select", "outcome_r"),
                ]
            )
            rows = self._request(f"paper_trades?{query}")
            outcomes = [
                float(row["outcome_r"])
                for row in rows if isinstance(row, dict) and row.get("outcome_r") is not None
            ] if isinstance(rows, list) else []
            health[component] = summarize_outcomes(outcomes)
        return health

    def load_portfolio_risk_state(self, bot_id: str, timestamp_utc: datetime) -> dict[str, object]:
        """Load the Bot 10/11 risk state frozen to the current UTC day."""
        day_start = utc_day_start(timestamp_utc)
        next_day = day_start + timedelta(days=1)
        query = urlencode(
            [
                ("bot_id", f"eq.{bot_id}"),
                ("variant", "eq.technical"),
                ("opened_at_utc", f"gte.{day_start.isoformat()}"),
                ("opened_at_utc", f"lt.{next_day.isoformat()}"),
                ("select", "status,outcome_r,closed_at_utc"),
            ]
        )
        rows = self._request(f"paper_trades?{query}")
        trades = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        closed = [row for row in trades if row.get("status") == "closed" and row.get("outcome_r") is not None]
        losses = [
            datetime.fromisoformat(str(row["closed_at_utc"]))
            for row in closed
            if float(row["outcome_r"]) < 0 and row.get("closed_at_utc")
        ]
        cooldown_until = max(losses) + timedelta(minutes=15) if losses else None
        return {
            "trades_started": len(trades),
            "daily_outcome_r": round(sum(float(row["outcome_r"]) for row in closed), 6),
            "cooldown_until_utc": cooldown_until.isoformat() if cooldown_until else None,
            "as_of_utc": timestamp_utc.isoformat(),
        }

    def load_chart(self, bot: BotCatalogItem, limit: int = 5000, source_bot_id: str | None = None) -> MarketChartResponse:
        candles = self.load_candles(bot, limit)
        trades_query = urlencode(
            {
                "bot_id": f"eq.{bot.id}",
                "symbol": f"eq.{bot.symbol}",
                "select": "*",
                "order": "opened_at_utc.desc",
                "limit": "100",
            }
        )
        trade_rows = self._request(f"trade_results?{trades_query}")
        combined_rows = (
            (trade_rows if isinstance(trade_rows, list) else [])
            + self._load_paper_results(bot.id)
            + self._load_tick_shadow_results(bot.id)
        )
        trades = [TradeResult.model_validate(row) for row in combined_rows if isinstance(row, dict)]
        return MarketChartResponse(bot_id=bot.id, symbol=bot.symbol, timeframe=bot.timeframe, candles=candles, trades=trades)

    def load_candles(self, bot: BotCatalogItem, limit: int = 5000, source_bot_id: str | None = None) -> list[CandleSnapshot]:
        candle_rows: list[object] = []
        for offset in range(0, limit, 1000):
            query = urlencode({"symbol": f"eq.{bot.symbol}", "timeframe": f"eq.{bot.timeframe}", "select": "timestamp_utc,open,high,low,close,volume", "order": "timestamp_utc.desc", "limit": "1000", "offset": str(offset)})
            page = self._request(f"market_history?{query}")
            if not isinstance(page, list) or not page:
                break
            candle_rows.extend(page)
            if len(page) < 1000:
                break
        return [CandleSnapshot.model_validate(row) for row in reversed(candle_rows)]

    def record_runtime_status(self, bot_id: str, *, request_at: datetime, candle_at: datetime | None, action: str, reason: str, latency_ms: float) -> None:
        self._request(
            "bot_runtime_status?on_conflict=bot_id",
            method="POST",
            payload={"bot_id": bot_id, "last_request_at_utc": request_at.isoformat(), "last_attempt_at_utc": datetime.now(timezone.utc).isoformat(), "last_success_at_utc": datetime.now(timezone.utc).isoformat(), "last_candle_at_utc": candle_at.isoformat() if candle_at else None, "last_action": action, "last_reason": reason, "last_latency_ms": round(latency_ms, 2), "consecutive_errors": 0, "last_error": None, "last_error_code": None},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def record_runtime_failure(self, bot_id: str, *, error_code: str, error_message: str) -> None:
        rows = self._request(f"bot_runtime_status?bot_id=eq.{bot_id}&select=consecutive_errors&limit=1")
        previous = int(rows[0].get("consecutive_errors") or 0) if isinstance(rows, list) and rows else 0
        self._request(
            "bot_runtime_status?on_conflict=bot_id",
            method="POST",
            payload={
                "bot_id": bot_id,
                "last_attempt_at_utc": datetime.now(timezone.utc).isoformat(),
                "consecutive_errors": previous + 1,
                "last_error": error_message[:500],
                "last_error_code": error_code,
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def load_decision_response(self, bot_id: str, request_id: str) -> DecisionResponse | None:
        query = urlencode({"bot_id": f"eq.{bot_id}", "request_id": f"eq.{request_id}", "select": "response", "limit": "1"})
        rows = self._request(f"decision_requests?{query}")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        response = rows[0].get("response")
        return DecisionResponse.model_validate(response) if isinstance(response, dict) else None

    def save_decision_response(self, bot_id: str, request_id: str, response: DecisionResponse) -> None:
        self._request(
            "decision_requests?on_conflict=bot_id,request_id",
            method="POST",
            payload={"bot_id": bot_id, "request_id": request_id, "response": response.model_dump(mode="json")},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def readiness(self) -> None:
        self._request("bots?select=id&limit=1")

    def load_operations(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        statuses = self._request("bot_runtime_status?select=*&order=bot_id")
        snapshots = self._request("backtest_snapshots?period_days=eq.365&select=bot_id,updated_at,market_data_through_utc&order=bot_id")
        return (
            [row for row in statuses if isinstance(row, dict)] if isinstance(statuses, list) else [],
            [row for row in snapshots if isinstance(row, dict)] if isinstance(snapshots, list) else [],
        )
