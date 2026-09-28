import unittest
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.schemas import BotCatalogItem, CandleSnapshot, DecisionResponse, HistoricalReplayResponse, ResultsSummary, TickIngestRequest, TradeResult
from app.supabase_store import SupabaseRequestError, SupabaseStore
from tests.test_strategy import bullish_request, macd_trend_request, mean_reversion_request, stochastic_pullback_request


class RecordingStore(SupabaseStore):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, str | None]] = []
        self.open_rows: list[object] = []

    def _request(self, path: str, *, method: str = "GET", payload: object = None, prefer: str | None = None) -> object:
        self.calls.append((path, method, payload, prefer))
        if path.startswith("bots?"):
            return []
        if path.startswith("paper_trades?bot_id=") and method == "GET":
            return self.open_rows
        if path.startswith("paper_trades?") and method == "GET":
            return self.open_rows
        return None


class PaperResultsStore(RecordingStore):
    def _request(self, path: str, *, method: str = "GET", payload: object = None, prefer: str | None = None) -> object:
        if path.startswith("paper_trades?"):
            return [
                {
                    "signal_key": "bot-2:M5:signal",
                    "side": "buy",
                    "entry_price": 100,
                    "initial_stop_price": 99,
                    "target_price": 102,
                    "opened_at_utc": "2026-07-31T10:00:00+00:00",
                    "closed_at_utc": "2026-07-31T10:30:00+00:00",
                    "outcome_r": 1.5,
                }
            ]
        if path.startswith("strategy_signals?"):
            return [
                {
                    "signal_key": "bot-2:M5:signal",
                    "response": {
                        "risk_amount": 100,
                        "metadata": {"setup_type": "momentum_atr_breakout", "suggested_size": 0.5},
                    },
                }
            ]
        return []


class TickShadowResultsStore(RecordingStore):
    def _request(self, path: str, *, method: str = "GET", payload: object = None, prefer: str | None = None) -> object:
        if path.startswith("tick_shadow_trades?"):
            return [{
                "signal_key": "bot-19:signal", "bot_id": "bot-19", "symbol": "XAUUSD",
                "side": "buy", "opened_at_utc": "2026-08-09T10:00:00+00:00",
                "closed_at_utc": "2026-08-09T10:00:20+00:00",
                "entry_price": 100.0, "initial_stop_price": 99.4,
                "target_price": 110.0, "exit_price": 100.3,
                "pip_size": .01, "value_per_pip": .1, "volume_lots": .1,
                "net_pnl": 3.0, "exit_reason": "stop",
            }]
        return []


class SupabaseStoreTests(unittest.TestCase):
    @staticmethod
    def tick_shadow_row(side: str = "buy") -> dict[str, object]:
        return {
            "signal_key": "bot-19:signal", "side": side,
            "entry_price": 100.0, "stop_price": 99.4 if side == "buy" else 100.6,
            "best_price": 100.0, "target_price": 110.0 if side == "buy" else 90.0,
            "pip_size": 0.01, "value_per_pip": 0.1,
            "opened_time_msc": 1_700_000_000_000,
            "break_even_trigger_pips": 40, "break_even_offset_pips": 1,
            "trailing_activation_pips": 40, "trailing_distance_pips": 20,
            "max_holding_seconds": 60,
        }

    def test_tick_batches_are_idempotent_and_chunked(self) -> None:
        store = RecordingStore()
        batch = TickIngestRequest(
            collector_id="forward-01",
            symbol="XAUUSD",
            point_size=0.01,
            ticks=[
                {"time_msc": 1_700_000_000_000 + index, "sequence": index, "bid": 2000, "ask": 2000.1}
                for index in range(501)
            ],
        )

        store.upsert_ticks(batch)

        self.assertEqual(len(store.calls), 2)
        self.assertTrue(all("on_conflict=collector_id,symbol,time_msc,sequence" in call[0] for call in store.calls))
        self.assertEqual(len(store.calls[0][2]), 500)
        self.assertEqual(store.calls[0][3], "resolution=ignore-duplicates,return=minimal")

    def test_store_requires_configuration_and_normalizes_base_url(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            SupabaseStore(SimpleNamespace(supabase_results_configured=False))
        configured = SimpleNamespace(
            supabase_results_configured=True,
            supabase_url="https://project.supabase.co/",
            supabase_service_role_key="key",
        )
        store = SupabaseStore(configured)
        self.assertEqual(store.base_url, "https://project.supabase.co")
        self.assertEqual(store.key, "key")

    def test_request_maps_auth_and_rate_limit_errors(self) -> None:
        configured = SimpleNamespace(
            supabase_results_configured=True,
            supabase_url="https://project.supabase.co",
            supabase_service_role_key="key",
            supabase_retry_attempts=1,
            supabase_timeout_seconds=0.1,
        )
        store = SupabaseStore(configured)
        for status, code, retryable in ((401, "auth_error", False), (429, "rate_limit", True), (500, "http_error", True)):
            error = HTTPError("https://project", status, "failed", {}, BytesIO())
            with self.subTest(status=status), patch("app.supabase_store.urlopen", side_effect=error):
                with self.assertRaises(SupabaseRequestError) as context:
                    store._request("bots?select=id")
            self.assertEqual(context.exception.code, code)
            self.assertEqual(context.exception.retryable, retryable)
            self.assertEqual(context.exception.status_code, status)

    def test_backtest_snapshot_loads_valid_payload_and_rejects_missing_rows(self) -> None:
        store = RecordingStore()
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        replay = HistoricalReplayResponse(
            bot_id="bot-1", symbol="XAUUSD", timeframe="M5",
            period_start_utc=timestamp, period_end_utc=timestamp,
            candles_evaluated=0, candidates_detected=0, summary=ResultsSummary(),
        )
        store._request = MagicMock(return_value=[{"payload": replay.model_dump(mode="json")}])
        self.assertEqual(store.load_backtest_snapshot("bot-1", 30).bot_id, "bot-1")
        for rows in (None, [], ["invalid"], [{"payload": "invalid"}]):
            store._request.return_value = rows
            with self.subTest(rows=rows):
                self.assertIsNone(store.load_backtest_snapshot("bot-1", 30))

    def test_trade_and_candle_upserts_are_serialized_and_chunked(self) -> None:
        store = RecordingStore()
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trade = TradeResult(
            trade_id="trade-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy",
            opened_at_utc=timestamp, volume_lots=0.1, entry_price=100,
        )
        store.upsert_trade("bot-1", trade)
        self.assertEqual(store.calls[-1][2]["bot_id"], "bot-1")
        self.assertEqual(store.calls[-1][2]["raw_payload"]["trade_id"], "trade-1")

        store.calls.clear()
        store.upsert_candles("bot-1", "XAUUSD", "M5", [])
        self.assertEqual(store.calls, [])
        candles = [
            CandleSnapshot(timestamp_utc=timestamp, open=100, high=101, low=99, close=100, volume=1)
            for _ in range(1001)
        ]
        store.upsert_candles("bot-1", "XAUUSD", "M5", candles)
        self.assertEqual([len(call[2]) for call in store.calls], [1000, 1])

    def test_candle_loading_paginates_and_returns_chronological_order(self) -> None:
        store = RecordingStore()
        bot = BotCatalogItem(id="bot-1", name="Bot", description="test", strategy_profile="test", symbol="XAUUSD", timeframe="M5")
        newest = {"timestamp_utc": "2026-01-01T00:05:00Z", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1}
        oldest = {"timestamp_utc": "2026-01-01T00:00:00Z", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        store._request = MagicMock(return_value=[newest, oldest])
        candles = store.load_candles(bot, 1000)
        self.assertEqual([candle.close for candle in candles], [1, 2])

    def test_runtime_decision_and_operations_helpers_preserve_contracts(self) -> None:
        store = RecordingStore()
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.record_runtime_status("bot-1", request_at=timestamp, candle_at=None, action="hold", reason="safe", latency_ms=1.236)
        self.assertEqual(store.calls[-1][2]["last_latency_ms"], 1.24)
        self.assertIsNone(store.calls[-1][2]["last_candle_at_utc"])

        store._request = MagicMock(side_effect=[[{"consecutive_errors": 2}], None])
        store.record_runtime_failure("bot-1", error_code="network", error_message="x" * 600)
        failure_payload = store._request.call_args_list[-1].kwargs["payload"]
        self.assertEqual(failure_payload["consecutive_errors"], 3)
        self.assertEqual(len(failure_payload["last_error"]), 500)

        response = DecisionResponse(action="hold", reason="safe")
        store._request = MagicMock(return_value=None)
        store.save_decision_response("bot-1", "request-1", response)
        self.assertEqual(store._request.call_args.kwargs["payload"]["response"]["reason"], "safe")

        store._request = MagicMock(side_effect=[[{"response": response.model_dump(mode="json")}], [{"bot_id": "bot-1"}, "bad"], [{"bot_id": "bot-1"}, None]])
        self.assertEqual(store.load_decision_response("bot-1", "request-1").reason, "safe")
        statuses, snapshots = store.load_operations()
        self.assertEqual(statuses, [{"bot_id": "bot-1"}])
        self.assertEqual(snapshots, [{"bot_id": "bot-1"}])

    def test_tick_shadow_buy_applies_trailing_then_closes_on_executable_bid(self) -> None:
        batch = TickIngestRequest(
            collector_id="forward-01", symbol="XAUUSD", point_size=0.01,
            ticks=[
                {"time_msc": 1_700_000_001_000, "sequence": 1, "bid": 100.5, "ask": 100.6},
                {"time_msc": 1_700_000_002_000, "sequence": 2, "bid": 100.29, "ask": 100.39},
            ],
        )

        patch = SupabaseStore._advance_tick_shadow_row(self.tick_shadow_row(), batch)

        self.assertEqual(patch["status"], "closed")
        self.assertEqual(patch["exit_reason"], "stop")
        self.assertEqual(patch["exit_price"], 100.29)
        self.assertAlmostEqual(float(patch["pnl_pips"]), 29)

    def test_tick_shadow_sell_uses_ask_and_closes_at_timeout(self) -> None:
        batch = TickIngestRequest(
            collector_id="forward-01", symbol="XAUUSD", point_size=0.01,
            ticks=[
                {"time_msc": 1_700_000_060_000, "sequence": 1, "bid": 99.7, "ask": 99.8},
            ],
        )

        patch = SupabaseStore._advance_tick_shadow_row(self.tick_shadow_row("sell"), batch)

        self.assertEqual(patch["status"], "closed")
        self.assertEqual(patch["exit_reason"], "time")
        self.assertEqual(patch["exit_price"], 99.8)
        self.assertAlmostEqual(float(patch["pnl_pips"]), 20)

    def test_tick_shadow_trade_validates_input_existing_position_and_backfill(self) -> None:
        store = RecordingStore()
        request = bullish_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(action="hold", reason="shadow")
        self.assertFalse(store.record_tick_shadow_trade("signal", request, response))

        response = DecisionResponse(
            action="hold", reason="shadow", entry_price=100, stop_loss_price=99,
            final_take_profit_price=102,
            metadata={"suggested_action": "buy", "suggested_size": 0.1},
        )
        store._request = MagicMock(return_value=[{"signal_key": "already-open"}])
        self.assertFalse(store.record_tick_shadow_trade("signal", request, response))

        recent = [
            {"time_msc": 1_700_000_000_000, "bid": 100, "ask": 100.1},
            {"time_msc": 1_700_000_000_000, "bid": 100, "ask": 100.1},
            "invalid",
        ]
        store._request = MagicMock(side_effect=[[], None, recent])
        with patch.object(store, "advance_tick_shadow_trades") as advance:
            self.assertTrue(store.record_tick_shadow_trade("signal", request, response))
        inserted = store._request.call_args_list[1].kwargs["payload"]
        self.assertEqual(inserted["status"], "open")
        self.assertEqual(inserted["value_per_pip"], 0.1)
        replay = advance.call_args.args[0]
        self.assertEqual(len(replay.ticks), 1)
        self.assertEqual(replay.collector_id, "shadow-backfill")

    def test_advance_tick_shadow_trades_patches_each_valid_open_row(self) -> None:
        store = RecordingStore()
        batch = TickIngestRequest(
            collector_id="forward-01", symbol="XAUUSD", point_size=0.01,
            ticks=[{"time_msc": 1_700_000_001_000, "sequence": 1, "bid": 100.1, "ask": 100.2}],
        )
        row = self.tick_shadow_row()
        store._request = MagicMock(side_effect=[[row, "invalid"], None])
        store.advance_tick_shadow_trades(batch)
        self.assertEqual(store._request.call_count, 2)
        self.assertEqual(store._request.call_args_list[-1].kwargs["method"], "PATCH")

    def test_paper_trade_advances_partial_target_expiry_and_trailing(self) -> None:
        base = {
            "signal_key": "signal", "variant": "technical", "side": "buy",
            "entry_price": 100, "initial_stop_price": 99, "stop_price": 99,
            "partial_price": 101, "target_price": 102, "partial_hit": False,
            "partial_close_pct": 50, "opened_at_utc": "2026-01-01T10:00:00+00:00",
        }
        candle = CandleSnapshot(timestamp_utc="2026-01-01T10:05:00Z", open=100, high=101.2, low=99.5, close=101, volume=1)

        store = RecordingStore()
        store.open_rows = [base]
        store.advance_paper_trades("bot-1", [candle])
        self.assertEqual(store.calls[-1][2], {"partial_hit": True, "stop_price": 100.0})

        store = RecordingStore()
        store.open_rows = [{**base, "partial_hit": True, "stop_price": 100}]
        target = candle.model_copy(update={"high": 102.1, "low": 100.1})
        store.advance_paper_trades("bot-1", [target])
        self.assertEqual(store.calls[-1][2]["status"], "closed")
        self.assertEqual(store.calls[-1][2]["outcome_r"], 1.5)

        store = RecordingStore()
        store.open_rows = [{**base, "expires_at_utc": "2026-01-01T10:10:00+00:00"}]
        quiet = candle.model_copy(update={"high": 100.5, "low": 99.5, "close": 100.25})
        store.advance_paper_trades("bot-1", [quiet])
        self.assertEqual(store.calls[-1][2]["outcome_r"], 0.25)

        store = RecordingStore()
        store.open_rows = [{
            **base, "partial_price": 110, "target_price": 120,
            "point_size": 0.01, "break_even_trigger_points": 40,
            "break_even_offset_points": 0, "trailing_activation_points": 100,
            "trailing_distance_points": 50,
        }]
        favorable = candle.model_copy(update={"high": 101.5, "low": 99.5})
        store.advance_paper_trades("bot-1", [favorable])
        self.assertEqual(store.calls[-1][2]["stop_price"], 101.0)

    def test_tick_shadow_results_are_exposed_without_looking_like_real_trades(self) -> None:
        results = TickShadowResultsStore()._load_tick_shadow_results("bot-19")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trade_id"], "tick-shadow:bot-19:signal")
        self.assertEqual(results[0]["timeframe"], "TICK")
        self.assertAlmostEqual(float(results[0]["rr_realized"]), .5)
        self.assertIn("Nenhuma ordem", str(results[0]["note"]))

    def test_catalog_only_loads_active_bots(self) -> None:
        store = RecordingStore()

        self.assertEqual(store.list_bots(), [])
        self.assertIn("active=eq.true", store.calls[0][0])
        self.assertIn("active", store.calls[0][0])

    def test_catalog_can_load_archived_bots(self) -> None:
        store = RecordingStore()

        self.assertEqual(store.list_bots(False), [])
        self.assertIn("active=eq.false", store.calls[0][0])

    def test_backtest_snapshot_is_compact_and_upserted_by_period(self) -> None:
        store = RecordingStore()
        timestamp = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        replay = HistoricalReplayResponse(
            bot_id="bot-eur-1",
            symbol="EURUSD",
            timeframe="M5",
            period_start_utc=timestamp,
            period_end_utc=timestamp,
            candles_evaluated=1,
            candidates_detected=0,
            summary=ResultsSummary(),
            candles=[CandleSnapshot(timestamp_utc=timestamp, open=1.1, high=1.2, low=1.0, close=1.1, volume=1)],
        )

        store.save_backtest_snapshot(replay, 365)

        path, method, payload, prefer = store.calls[-1]
        self.assertIn("on_conflict=bot_id,period_days", path)
        self.assertEqual(method, "POST")
        self.assertEqual(payload["period_days"], 365)
        self.assertEqual(payload["payload"]["candles"], [])
        self.assertEqual(prefer, "resolution=merge-duplicates,return=minimal")

    def test_replay_can_reuse_canonical_market_history(self) -> None:
        store = RecordingStore()
        bot = type("Bot", (), {"id": "bot-4", "symbol": "XAUUSD", "timeframe": "M5"})()

        candles = store.load_candles(bot, 1000, source_bot_id="bot-1")

        self.assertEqual(candles, [])
        self.assertIn("market_history?", store.calls[0][0])
        self.assertNotIn("bot_id=", store.calls[0][0])

    def test_bot_3_uses_its_own_open_position_scope(self) -> None:
        store = RecordingStore()
        request = mean_reversion_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(
            action="hold",
            reason="rsi_bollinger_shadow_signal",
            entry_price=100.3,
            stop_loss_price=97,
            partial_take_profit_price=101,
            final_take_profit_price=106.9,
            metadata={"suggested_action": "buy"},
        )

        inserted = store.record_paper_trade("bot-3-signal", request, response, "technical")

        self.assertTrue(inserted)
        self.assertIn("bot_id=eq.bot-3", store.calls[0][0])
        self.assertEqual(store.calls[-1][2]["bot_id"], "bot-3")

    def test_bot_4_uses_its_own_open_position_scope(self) -> None:
        store = RecordingStore()
        request = macd_trend_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(
            action="hold",
            reason="macd_trend_shadow_signal",
            entry_price=102.1,
            stop_loss_price=101.5,
            partial_take_profit_price=102.7,
            final_take_profit_price=103.3,
            metadata={"suggested_action": "buy"},
        )

        inserted = store.record_paper_trade("bot-4-signal", request, response, "technical")

        self.assertTrue(inserted)
        self.assertIn("bot_id=eq.bot-4", store.calls[0][0])
        self.assertEqual(store.calls[-1][2]["bot_id"], "bot-4")

    def test_adaptive_health_uses_only_prior_closed_technical_trades(self) -> None:
        store = RecordingStore()
        store.open_rows = [{"outcome_r": 1.0} for _ in range(8)]

        health = store.load_adaptive_component_health(datetime(2026, 8, 3, 10, tzinfo=timezone.utc))

        self.assertTrue(health["bot-3"]["eligible"])
        self.assertEqual(len(store.calls), 3)
        for path, method, _, _ in store.calls:
            self.assertEqual(method, "GET")
            self.assertIn("variant=eq.technical", path)
            self.assertIn("status=eq.closed", path)
            self.assertIn("closed_at_utc=gte.", path)
            self.assertIn("closed_at_utc=lt.", path)
            self.assertIn("T00%3A00%3A00%2B00%3A00", path)

    def test_weighted_portfolio_risk_state_is_scoped_to_bot_and_utc_day(self) -> None:
        store = RecordingStore()
        store.open_rows = [
            {"status": "closed", "outcome_r": -1.0, "closed_at_utc": "2026-08-03T10:00:00+00:00"},
            {"status": "open", "outcome_r": None, "closed_at_utc": None},
        ]

        state = store.load_portfolio_risk_state("bot-10", datetime(2026, 8, 3, 10, 5, tzinfo=timezone.utc))

        self.assertEqual(state["trades_started"], 2)
        self.assertEqual(state["daily_outcome_r"], -1)
        self.assertEqual(state["cooldown_until_utc"], "2026-08-03T10:15:00+00:00")
        path = store.calls[0][0]
        self.assertIn("bot_id=eq.bot-10", path)
        self.assertIn("opened_at_utc=gte.", path)
        self.assertIn("opened_at_utc=lt.", path)

    def test_bot_5_uses_its_own_open_position_scope(self) -> None:
        store = RecordingStore()
        request = stochastic_pullback_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(
            action="hold",
            reason="stochastic_pullback_shadow_signal",
            entry_price=101.95,
            stop_loss_price=100.9,
            partial_take_profit_price=103,
            final_take_profit_price=104.05,
            metadata={"suggested_action": "buy"},
        )

        inserted = store.record_paper_trade("bot-5-signal", request, response, "technical")

        self.assertTrue(inserted)
        self.assertIn("bot_id=eq.bot-5", store.calls[0][0])
        self.assertEqual(store.calls[-1][2]["bot_id"], "bot-5")

    def test_paper_results_are_exposed_as_simulated_trades(self) -> None:
        result = PaperResultsStore()._load_paper_results("bot-2")[0]

        self.assertEqual(result["setup"], "momentum_atr_breakout_paper_trade")
        self.assertEqual(result["net_pnl"], 150)
        self.assertEqual(result["rr_realized"], 1.5)
        self.assertIn("não foi enviado", result["note"])

    def test_hold_candidate_key_uses_suggested_side(self) -> None:
        store = RecordingStore()
        request = bullish_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(
            action="hold",
            reason="flow_neutral",
            entry_price=102.1,
            stop_loss_price=100,
            partial_take_profit_price=104.2,
            final_take_profit_price=106.3,
            metadata={
                "candidate": True,
                "suggested_action": "buy",
                "setup_type": "pullback",
                "flow_status": "flow_neutral",
            },
        )

        signal_key = store.record_strategy_candidate(request, response)

        self.assertIsNotNone(signal_key)
        self.assertIn(":buy:pullback", signal_key)
        payload = store.calls[-1][2]
        self.assertEqual(payload["suggested_action"], "buy")

    def test_parallel_variants_have_composite_conflict_target(self) -> None:
        store = RecordingStore()
        request = bullish_request(datetime(2026, 7, 31, 10, 20, tzinfo=timezone.utc))
        response = DecisionResponse(
            action="hold",
            reason="fimath_shadow_signal",
            entry_price=102.1,
            stop_loss_price=100,
            partial_take_profit_price=104.2,
            final_take_profit_price=106.3,
            metadata={"suggested_action": "buy"},
        )

        inserted = store.record_paper_trade("signal-1", request, response, "technical")

        self.assertTrue(inserted)
        self.assertIn("on_conflict=signal_key,variant", store.calls[-1][0])
        self.assertEqual(store.calls[-1][2]["variant"], "technical")

    def test_trade_does_not_advance_with_candle_before_entry(self) -> None:
        store = RecordingStore()
        store.open_rows = [
            {
                "signal_key": "signal-1",
                "variant": "technical",
                "side": "buy",
                "entry_price": 100,
                "stop_price": 99,
                "partial_price": 101,
                "target_price": 102,
                "partial_hit": False,
                "opened_at_utc": "2026-07-31T10:25:05+00:00",
            }
        ]
        candle = CandleSnapshot(
            timestamp_utc="2026-07-31T10:25:00Z",
            open=100,
            high=102,
            low=98,
            close=100,
            volume=100,
        )

        store.advance_paper_trades("bot-1", [candle])

        patch_calls = [call for call in store.calls if call[1] == "PATCH"]
        self.assertEqual(patch_calls, [])

    def test_full_single_target_paper_trade_closes_at_half_r(self) -> None:
        store = RecordingStore()
        store.open_rows = [{
            "signal_key": "signal-7",
            "variant": "technical",
            "side": "buy",
            "entry_price": 100,
            "initial_stop_price": 99,
            "stop_price": 99,
            "partial_price": 100.5,
            "target_price": 100.5,
            "partial_close_pct": 100,
            "partial_hit": False,
            "opened_at_utc": "2026-07-31T15:00:00+00:00",
            "expires_at_utc": "2026-07-31T16:00:00+00:00",
        }]
        candle = CandleSnapshot(
            timestamp_utc="2026-07-31T15:05:00Z",
            open=100,
            high=100.6,
            low=99.8,
            close=100.5,
            volume=100,
        )

        store.advance_paper_trades("bot-7", [candle])

        patch = next(call[2] for call in store.calls if call[1] == "PATCH")
        self.assertEqual(patch["status"], "closed")
        self.assertEqual(patch["outcome_r"], .5)


if __name__ == "__main__":
    unittest.main()
