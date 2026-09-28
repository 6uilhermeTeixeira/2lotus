import unittest
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from app.schemas import AccountSnapshot, BotCatalogItem, CandleIngestRequest, CandleSnapshot, DailyPnlPoint, GrowthSimulationRequest, HistoricalReplayResponse, MarketChartResponse, ResultsResponse, ResultsSummary, RiskTier, TickIngestRequest, TickSnapshot, TradeIngestRequest, TradeResult
from app.supabase_store import SupabaseRequestError


class MainContractTests(unittest.TestCase):
    def bot(self) -> BotCatalogItem:
        return BotCatalogItem(
            id="core-zigzag-v3",
            name="ZigZag",
            description="test",
            strategy_profile="happy_gold_zigzag_scalper",
            symbol="XAUUSD",
            timeframe="M5",
        )

    def candle(self, **values: float) -> CandleSnapshot:
        base = {"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1}
        base.update(values)
        return CandleSnapshot(timestamp_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), **base)

    def replay(self, risk: float = 0.25) -> HistoricalReplayResponse:
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return HistoricalReplayResponse(
            bot_id=self.bot().id,
            symbol="XAUUSD",
            timeframe="M5",
            risk_per_trade_pct=risk,
            period_start_utc=timestamp,
            period_end_utc=timestamp + timedelta(days=1),
            candles_evaluated=1,
            candidates_detected=0,
            summary=ResultsSummary(
                trades_count=2, wins=1, losses=1, win_rate_pct=50,
                gross_profit=100, gross_loss=-50, net_pnl=50,
                avg_win=100, avg_loss=-50, profit_factor=2, avg_rr=0.5,
                best_trade=100, worst_trade=-50,
            ),
            daily_pnl=[DailyPnlPoint(day=date(2026, 1, 1), net_pnl=50)],
        )

    def test_ingest_key_requires_configuration_and_constant_time_match(self) -> None:
        with patch.object(main_module.settings, "ingest_api_key", None):
            with self.assertRaisesRegex(HTTPException, "not configured"):
                main_module._require_ingest_key("anything")
        with patch.object(main_module.settings, "ingest_api_key", "secret"):
            with self.assertRaisesRegex(HTTPException, "invalid"):
                main_module._require_ingest_key("wrong")
            main_module._require_ingest_key("secret")

    def test_candle_validation_rejects_non_positive_and_inconsistent_ohlc(self) -> None:
        with self.assertRaisesRegex(HTTPException, "positive"):
            main_module._validate_ingested_candles([self.candle(low=0)])
        with self.assertRaisesRegex(HTTPException, "inconsistent"):
            main_module._validate_ingested_candles([self.candle(high=99.5)])
        main_module._validate_ingested_candles([self.candle()])

    def test_trade_and_candle_ingestion_persist_to_sqlite(self) -> None:
        trade = TradeResult(
            trade_id="trade-1", symbol="XAUUSD", timeframe="M5", setup="test", side="buy",
            opened_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), volume_lots=0.1,
            entry_price=100, exit_price=101, net_pnl=1,
        )
        payload_trade = TradeIngestRequest(bot_id=self.bot().id, trade=trade)
        payload_candles = CandleIngestRequest(bot_id=self.bot().id, symbol="XAUUSD", timeframe="M5", candles=[self.candle()])
        store = MagicMock()
        configured = SimpleNamespace(
            ingest_api_key="secret", results_store="SQLITE", ingest_rate_limit_per_minute=100,
            tick_ingest_rate_limit_per_minute=100,
        )
        request = MagicMock(client=SimpleNamespace(host="127.0.0.1"), headers={})
        with patch.object(main_module, "settings", configured), patch.object(main_module, "SQLiteStore", return_value=store), patch.object(main_module, "_find_bot", return_value=self.bot()):
            self.assertIsNone(main_module.ingest_trade(payload_trade, request, "secret"))
            self.assertIsNone(main_module.ingest_candles(payload_candles, request, "secret"))
        store.upsert_trade.assert_called_once_with(self.bot().id, trade)
        store.upsert_candles.assert_called_once()

    def test_candle_ingestion_rejects_bot_symbol_or_timeframe_mismatch(self) -> None:
        payload = CandleIngestRequest(bot_id=self.bot().id, symbol="EURUSD", timeframe="M5", candles=[self.candle()])
        request = MagicMock(client=SimpleNamespace(host="127.0.0.1"), headers={})
        configured = SimpleNamespace(ingest_api_key="secret", results_store="SQLITE", ingest_rate_limit_per_minute=100)
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_find_bot", return_value=self.bot()):
            with self.assertRaisesRegex(HTTPException, "does not match"):
                main_module.ingest_candles(payload, request, "secret")

    def test_ingestion_requires_persistent_store(self) -> None:
        request = MagicMock(client=SimpleNamespace(host="127.0.0.1"), headers={})
        configured = SimpleNamespace(ingest_api_key="secret", results_store="JSON", ingest_rate_limit_per_minute=100)
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_find_bot", return_value=self.bot()):
            with self.assertRaisesRegex(HTTPException, "persistent results store"):
                main_module.ingest_candles(
                    CandleIngestRequest(bot_id=self.bot().id, symbol="XAUUSD", timeframe="M5", candles=[self.candle()]),
                    request,
                    "secret",
                )

    def test_authenticated_api_rejects_missing_session(self) -> None:
        client = TestClient(app)
        with patch.object(main_module.settings, "dashboard_password", "configured"), patch.object(main_module.settings, "session_secret", "s" * 32), patch("app.main.has_valid_session", return_value=False):
            response = client.get("/api/results")
        self.assertEqual(response.status_code, 401)

    def test_readiness_reports_unconfigured_and_unreachable_supabase(self) -> None:
        unconfigured = SimpleNamespace(results_store="SUPABASE", supabase_results_configured=False)
        with patch.object(main_module, "settings", unconfigured):
            response = main_module.readiness()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"configuration":"failed"', response.body)

        configured = SimpleNamespace(results_store="SUPABASE", supabase_results_configured=True)
        store = MagicMock()
        store.readiness.side_effect = SupabaseRequestError("offline", code="network_error")
        with patch.object(main_module, "settings", configured), patch.object(main_module, "SupabaseStore", return_value=store):
            response = main_module.readiness()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"supabase":"network_error"', response.body)

    def test_readiness_reports_sqlite_failure_and_success(self) -> None:
        configured = SimpleNamespace(results_store="SQLITE")
        store = MagicMock()
        store.readiness.side_effect = sqlite3.OperationalError("locked")
        with patch.object(main_module, "settings", configured), patch.object(main_module, "SQLiteStore", return_value=store):
            failed = main_module.readiness()
        self.assertEqual(failed.status_code, 503)
        self.assertIn(b'"sqlite":"failed"', failed.body)

        store.readiness.side_effect = None
        with patch.object(main_module, "settings", configured), patch.object(main_module, "SQLiteStore", return_value=store):
            ready = main_module.readiness()
        self.assertEqual(ready.status_code, 200)
        self.assertIn(b'"sqlite":"passed"', ready.body)

    def test_bot_chart_selects_sqlite_json_and_supabase_backends(self) -> None:
        bot = self.bot()
        empty = MarketChartResponse(bot_id=bot.id, symbol=bot.symbol, timeframe=bot.timeframe)
        request = MagicMock()
        with patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot):
            sqlite_store = MagicMock()
            sqlite_store.load_chart.return_value = empty
            sqlite_settings = SimpleNamespace(results_store="SQLITE", chart_cache_limit=100)
            with patch.object(main_module, "settings", sqlite_settings), patch.object(main_module, "SQLiteStore", return_value=sqlite_store):
                self.assertEqual(main_module.read_bot_chart(bot.id, request), empty)
            json_settings = SimpleNamespace(results_store="JSON", chart_cache_limit=100)
            with patch.object(main_module, "settings", json_settings):
                self.assertEqual(main_module.read_bot_chart(bot.id, request).candles, [])
            supabase_store = MagicMock()
            supabase_store.load_chart.return_value = empty
            supabase_settings = SimpleNamespace(results_store="SUPABASE", chart_cache_limit=100)
            with patch.object(main_module, "settings", supabase_settings), patch.object(main_module, "SupabaseStore", return_value=supabase_store):
                self.assertEqual(main_module.read_bot_chart(bot.id, request), empty)

    def test_operations_prioritizes_errors_and_handles_missing_data(self) -> None:
        bots = [self.bot(), self.bot().model_copy(update={"id": "core-stochastic-v3", "name": "Stochastic"})]
        store = MagicMock()
        store.load_operations.return_value = (
            [{"bot_id": bots[0].id, "consecutive_errors": 2, "last_error": "offline"}],
            [{"bot_id": bots[0].id, "updated_at": "2026-01-01T00:00:00+00:00"}],
        )
        configured = SimpleNamespace(results_store="SQLITE")
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=bots), patch.object(main_module, "SQLiteStore", return_value=store):
            result = main_module.read_operations(MagicMock())
        self.assertEqual(result.bots[0].status, "degraded")
        self.assertEqual(result.bots[0].consecutive_errors, 2)
        self.assertEqual(result.bots[1].status, "no_data")

    def test_bot_lookup_and_storage_selectors(self) -> None:
        bot = self.bot()
        with patch.object(main_module, "_list_bots", return_value=[bot]):
            self.assertEqual(main_module._find_bot(bot.id), bot)
            with self.assertRaisesRegex(HTTPException, "bot not found"):
                main_module._find_bot("missing")
        with patch.object(main_module.settings, "results_store", "SUPABASE"):
            self.assertTrue(main_module._uses_supabase_results())
            self.assertFalse(main_module._uses_sqlite_results())
        with patch.object(main_module.settings, "results_store", "SQLITE"):
            self.assertTrue(main_module._uses_sqlite_results())

    def test_list_bots_filters_core_ids_for_local_and_supabase_sources(self) -> None:
        core = self.bot()
        foreign = core.model_copy(update={"id": "foreign-bot"})
        with patch.object(main_module.settings, "results_store", "JSON"), patch("app.main.load_bots_catalog", return_value=[core, foreign]):
            self.assertEqual(main_module._list_bots(), [core])
        store = MagicMock()
        store.list_bots.return_value = [core, foreign]
        with patch.object(main_module.settings, "results_store", "SUPABASE"), patch("app.main.SupabaseStore", return_value=store):
            self.assertEqual(main_module._list_bots(None), [core])

    def test_result_loader_selects_each_storage_backend(self) -> None:
        bot = self.bot()
        expected = ResultsResponse(source_file="test", summary=ResultsSummary())
        with patch.object(main_module.settings, "results_store", "SUPABASE"), patch("app.main.SupabaseStore") as store:
            store.return_value.load_results.return_value = expected
            self.assertEqual(main_module._load_bot_results(bot), expected)
        with patch.object(main_module.settings, "results_store", "SQLITE"), patch("app.main.SQLiteStore") as store:
            store.return_value.load_results.return_value = expected
            self.assertEqual(main_module._load_bot_results(bot), expected)
        with patch.object(main_module.settings, "results_store", "JSON"), patch("app.main.resolve_bot_results_file", return_value="file"), patch("app.main.load_results_snapshot", return_value=expected):
            self.assertEqual(main_module._load_bot_results(bot), expected)

    def test_legacy_replay_risk_is_scaled_without_mutating_cached_value(self) -> None:
        bot = self.bot()
        replay = self.replay(risk=0.5)
        with patch.object(main_module.settings, "backtest_risk_per_trade_pct", 0.25):
            normalized = main_module._normalize_replay_for_equity(bot, replay)
        self.assertIsNot(normalized, replay)
        self.assertEqual(normalized.risk_per_trade_pct, 0.25)
        self.assertEqual(normalized.summary.net_pnl, 25)
        self.assertEqual(normalized.daily_pnl[0].net_pnl, 25)
        self.assertEqual(replay.summary.net_pnl, 50)

        replay.risk_per_trade_pct = 0
        with patch.object(main_module.settings, "backtest_risk_per_trade_pct", 0.25):
            self.assertIsNone(main_module._normalize_replay_for_equity(bot, replay))

    def test_account_reports_waiting_and_observed_identity(self) -> None:
        request = MagicMock()
        configured = SimpleNamespace(live_canary_account_login=123, mt5_server="Fusion", mt5_initial_balance=0)
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "latest_live_account", None):
            waiting = main_module.read_account(request)
        self.assertFalse(waiting.available)

        account = AccountSnapshot(balance=10_000, equity=10_100, margin_free=9_000)
        observed = datetime.now(timezone.utc) - timedelta(seconds=45)
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "latest_live_account", (account, observed)), patch.object(main_module, "first_live_balance", 9_500):
            result = main_module.read_account(request)
        self.assertTrue(result.available)
        self.assertEqual(result.status, "delayed")
        self.assertEqual(result.initial_balance, 9_500)
        self.assertEqual(result.account.login, 123)

    def test_config_metrics_and_promotions_endpoints_return_runtime_views(self) -> None:
        request = MagicMock()
        with patch.object(main_module, "_require_api_session"), patch("app.main.metrics_snapshot", return_value={"harness_id": "test"}):
            self.assertEqual(main_module.read_metrics(request), {"harness_id": "test"})
        with patch.object(main_module, "_require_api_session"), patch("app.main.list_promotions", return_value=[{"id": "one"}]):
            promoted = main_module.read_promoted_strategies(request)
        self.assertEqual(promoted["count"], 1)
        with patch.object(main_module, "_require_api_session"):
            config = main_module.read_config(request)
        self.assertIn("enable_live_decisions", config)
        self.assertIn("execution_server_provider", config)

    def test_catalog_comparison_and_legacy_results_contracts(self) -> None:
        request = MagicMock()
        bot = self.bot()
        results = ResultsResponse(source_file="test", summary=ResultsSummary(net_pnl=12))
        with patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=[bot]):
            self.assertEqual(main_module.read_bots(request), [bot])
            with self.assertRaisesRegex(HTTPException, "retired"):
                main_module.read_bots(request, archived=True)
            with patch.object(main_module, "_load_bot_results", return_value=results):
                comparison = main_module.read_comparison(request)
                self.assertEqual(comparison["bots"][0]["summary"]["net_pnl"], 12)
                self.assertEqual(main_module.read_legacy_results(request), results)
        with patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=[]):
            with self.assertRaisesRegex(HTTPException, "no bots"):
                main_module.read_legacy_results(request)

    def test_equity_comparison_from_sqlite_accumulates_and_marks_missing(self) -> None:
        request = MagicMock()
        bots = [self.bot(), self.bot().model_copy(update={"id": "core-stochastic-v3"})]
        replay = self.replay()
        replay.daily_pnl = [
            DailyPnlPoint(day=date(2026, 1, 2), net_pnl=-5),
            DailyPnlPoint(day=date(2026, 1, 1), net_pnl=10),
        ]
        store = MagicMock()
        store.load_backtest_snapshot.side_effect = [replay, None]
        configured = SimpleNamespace(results_store="SQLITE")
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=bots), patch.object(main_module, "SQLiteStore", return_value=store):
            result = main_module.read_equity_comparison(request, "xauusd", 30)
        self.assertEqual([point.cumulative_pnl for point in result.series[0].points], [10, 5])
        self.assertEqual(result.missing_bot_ids, [bots[1].id])

    def test_equity_comparison_validates_period_and_symbol(self) -> None:
        request = MagicMock()
        with patch.object(main_module, "_require_api_session"):
            with self.assertRaisesRegex(HTTPException, "between 1 and 1827"):
                main_module.read_equity_comparison(request, days=0)
            with patch.object(main_module, "_list_bots", return_value=[]):
                with self.assertRaisesRegex(HTTPException, "no bots"):
                    main_module.read_equity_comparison(request)

    def test_backtest_market_chart_validates_input_and_maps_dataset_error(self) -> None:
        request = MagicMock()
        with patch.object(main_module, "_require_api_session"):
            with self.assertRaisesRegex(HTTPException, "resolution"):
                main_module.read_backtest_market_chart(request, resolution="2m")
            with self.assertRaisesRegex(HTTPException, "chart days"):
                main_module.read_backtest_market_chart(request, days=0)
            with patch("app.main.DatasetRelease.open", side_effect=main_module.DatasetError("missing")):
                with self.assertRaisesRegex(HTTPException, "missing"):
                    main_module.read_backtest_market_chart(request)

    def test_historical_replay_uses_sqlite_and_attaches_chart_candles(self) -> None:
        request = MagicMock()
        bot = self.bot()
        replay = self.replay()
        chart = MarketChartResponse(bot_id=bot.id, symbol=bot.symbol, timeframe=bot.timeframe, candles=[self.candle()])
        store = MagicMock()
        store.load_backtest_snapshot.return_value = replay
        store.load_chart.return_value = chart
        configured = SimpleNamespace(results_store="SQLITE", chart_cache_limit=100)
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot), patch.object(main_module, "SQLiteStore", return_value=store):
            result = main_module.read_historical_replay(bot.id, request, 30)
        self.assertEqual(result.candles, chart.candles)

    def test_historical_replay_reports_invalid_period_and_missing_cache(self) -> None:
        request = MagicMock()
        bot = self.bot()
        with patch.object(main_module, "_require_api_session"):
            with self.assertRaisesRegex(HTTPException, "between 1 and 1827"):
                main_module.read_historical_replay(bot.id, request, 0)
        configured = SimpleNamespace(results_store="SQLITE", chart_cache_limit=100)
        store = MagicMock()
        store.load_backtest_snapshot.return_value = None
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot), patch.object(main_module, "SQLiteStore", return_value=store):
            with self.assertRaisesRegex(HTTPException, "no local"):
                main_module.read_historical_replay(bot.id, request, 30)

    def test_growth_simulation_validates_bounds_and_storage(self) -> None:
        request = MagicMock()
        with patch.object(main_module, "_require_api_session"):
            with self.assertRaisesRegex(HTTPException, "initial balance"):
                main_module.read_growth_simulation(request, initial_balance=99)
            with self.assertRaisesRegex(HTTPException, "risk per trade"):
                main_module.read_growth_simulation(request, risk_per_trade_pct=11)
            with patch.object(main_module.settings, "results_store", "JSON"):
                with self.assertRaisesRegex(HTTPException, "SUPABASE"):
                    main_module.read_growth_simulation(request)

    def test_authenticated_page_handlers_render_every_dashboard_section(self) -> None:
        request = MagicMock()
        bot = self.bot()
        rendered = object()
        handlers = [
            (main_module.bots_page, "render_catalog_page"),
            (main_module.overview_page, "render_live_overview_page"),
            (main_module.account_page, "render_account_page"),
            (main_module.trades_page, "render_trades_page"),
            (main_module.security_page, "render_security_page"),
            (main_module.server_page, "render_server_page"),
            (main_module.comparison_page, "render_comparison_page"),
            (main_module.growth_simulator_page, "render_growth_simulator_page"),
            (main_module.operations_page, "render_operations_page"),
        ]
        with patch.object(main_module, "_require_page_session", return_value=None), patch.object(main_module, "_list_bots", return_value=[bot]):
            for handler, renderer_name in handlers:
                with self.subTest(handler=handler.__name__), patch.object(main_module, renderer_name, return_value=rendered):
                    self.assertIs(handler(request), rendered)
            with patch.object(main_module, "render_results_page", return_value=rendered), patch.object(main_module, "_find_bot", return_value=bot):
                self.assertIs(main_module.bot_results_page(bot.id, request), rendered)

        redirect = object()
        with patch.object(main_module, "_require_page_session", return_value=redirect):
            for handler, _ in handlers:
                with self.subTest(redirect=handler.__name__):
                    self.assertIs(handler(request), redirect)
            self.assertIs(main_module.bot_results_page(bot.id, request), redirect)

    def test_page_session_and_replay_cache_helpers_fail_closed(self) -> None:
        request = MagicMock()
        with patch.object(main_module.settings, "dashboard_password", None):
            self.assertIn("error=config", main_module._require_page_session(request).headers["location"])
        with patch.object(main_module.settings, "dashboard_password", "configured"), patch.object(main_module.settings, "session_secret", "s" * 32), patch.object(main_module, "has_valid_session", return_value=False):
            self.assertEqual(main_module._require_page_session(request).headers["location"], "/login")

        bot = self.bot()
        replay = self.replay()
        store = MagicMock()
        store.load_backtest_snapshot.return_value = replay
        store.load_candles.return_value = [self.candle()]
        with patch.object(main_module.settings, "backtest_risk_per_trade_pct", replay.risk_per_trade_pct), patch.object(main_module.settings, "chart_cache_limit", 10), patch.object(main_module, "replay_settings_fingerprint", return_value=replay.settings_fingerprint):
            cached = main_module._load_or_run_backtest_replay(store, bot, 30, include_candles=True)
        self.assertIs(cached, replay)
        self.assertEqual(cached.candles, store.load_candles.return_value)
        with patch.object(main_module, "_replay_cache_matches_settings", return_value=False):
            self.assertIsNone(main_module._load_or_run_backtest_replay(store, bot, 30))

    def test_root_login_and_legacy_page_redirect_contracts(self) -> None:
        request = MagicMock()
        with patch.object(main_module, "has_valid_session", side_effect=[False, True]):
            self.assertEqual(main_module.root(request).headers["location"], "/login")
            self.assertEqual(main_module.root(request).headers["location"], "/overview")
        with patch.object(main_module, "has_valid_session", return_value=True):
            self.assertEqual(main_module.login_page(request).headers["location"], "/overview")
        with patch.object(main_module, "_require_page_session", return_value=None), patch.object(main_module, "_list_bots", return_value=[self.bot()]):
            self.assertEqual(main_module.legacy_results(request).headers["location"], f"/bots/{self.bot().id}")
        with patch.object(main_module, "_require_page_session", return_value=None), patch.object(main_module, "_list_bots", return_value=[]):
            self.assertEqual(main_module.legacy_results(request).headers["location"], "/bots")

    def test_equity_comparison_uses_static_results_fallback_and_supabase(self) -> None:
        request = MagicMock()
        bots = [
            self.bot(),
            self.bot().model_copy(update={"id": "core-stochastic-v3"}),
            self.bot().model_copy(update={"id": "core-multitimeframe-v3"}),
        ]
        replay = self.replay()
        fallback = ResultsResponse(
            source_file="fallback",
            summary=ResultsSummary(net_pnl=3),
            daily_pnl=[DailyPnlPoint(day=date(2026, 1, 1), net_pnl=3)],
        )
        json_settings = SimpleNamespace(results_store="JSON", bots_catalog_path="catalog")
        with patch.object(main_module, "settings", json_settings), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=bots), patch.object(main_module, "load_static_backtest_snapshot", side_effect=[replay, None, None]), patch.object(main_module, "_load_bot_results", side_effect=[fallback, ResultsResponse(source_file="empty", summary=ResultsSummary())]):
            static_result = main_module.read_equity_comparison(request, days=30)
        self.assertEqual(len(static_result.series), 2)
        self.assertEqual(static_result.missing_bot_ids, [bots[2].id])

        store = MagicMock()
        store.load_backtest_snapshot.side_effect = [replay, None, None]
        supabase_settings = SimpleNamespace(results_store="SUPABASE", backtest_risk_per_trade_pct=0.25)
        with patch.object(main_module, "settings", supabase_settings), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_list_bots", return_value=bots), patch.object(main_module, "SupabaseStore", return_value=store):
            remote_result = main_module.read_equity_comparison(request, days=30)
        self.assertEqual(len(remote_result.series), 1)
        self.assertEqual(remote_result.missing_bot_ids, [bots[1].id, bots[2].id])

    def test_tick_ingestion_selects_each_persistent_backend(self) -> None:
        request = MagicMock(client=SimpleNamespace(host="127.0.0.1"), headers={})
        payload = TickIngestRequest(
            collector_id="collector-1",
            symbol="XAUUSD",
            point_size=0.01,
            ticks=[TickSnapshot(time_msc=1, sequence=0, bid=100, ask=100.1)],
        )
        store = MagicMock()
        for backend, store_name in (("SUPABASE", "SupabaseStore"), ("SQLITE", "SQLiteStore")):
            configured = SimpleNamespace(
                ingest_api_key="secret",
                results_store=backend,
                tick_ingest_rate_limit_per_minute=100,
            )
            with self.subTest(backend=backend), patch.object(main_module, "settings", configured), patch.object(main_module, store_name, return_value=store):
                self.assertIsNone(main_module.ingest_ticks(payload, request, "secret"))
        self.assertEqual(store.upsert_ticks.call_count, 2)
        store.advance_tick_shadow_trades.assert_called_once_with(payload)

    def test_historical_replay_static_and_supabase_contracts(self) -> None:
        request = MagicMock()
        bot = self.bot()
        replay = self.replay()
        static_settings = SimpleNamespace(results_store="JSON", bots_catalog_path="catalog")
        with patch.object(main_module, "settings", static_settings), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot), patch.object(main_module, "load_static_backtest_snapshot", return_value=replay):
            self.assertIs(main_module.read_historical_replay(bot.id, request, 30), replay)
        remote_settings = SimpleNamespace(results_store="SUPABASE")
        store = MagicMock()
        with patch.object(main_module, "settings", remote_settings), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot), patch.object(main_module, "SupabaseStore", return_value=store), patch.object(main_module, "_load_or_run_backtest_replay", return_value=None):
            with self.assertRaisesRegex(HTTPException, "no historical candles"):
                main_module.read_historical_replay(bot.id, request, 30)

    def test_tiered_growth_success_and_validation_contracts(self) -> None:
        request = MagicMock()
        bot = self.bot().model_copy(update={"id": "core-combined-v3"})
        replay = self.replay()
        configured = SimpleNamespace(
            results_store="SUPABASE",
            backtest_balance=10_000,
            backtest_risk_per_trade_pct=0.25,
        )
        store = MagicMock()
        store.load_backtest_snapshot.return_value = replay
        invalid = GrowthSimulationRequest(
            initial_balance=1000,
            risk_tiers=[RiskTier(balance_threshold=0, risk_per_trade_pct=0.25), RiskTier(balance_threshold=1000, risk_per_trade_pct=0.5)],
        )
        with patch.object(main_module, "settings", configured), patch.object(main_module, "_require_api_session"), patch.object(main_module, "_find_bot", return_value=bot), patch.object(main_module, "SupabaseStore", return_value=store), patch.object(main_module, "_replay_cache_matches_settings", return_value=True):
            with self.assertRaisesRegex(HTTPException, "risk must stay"):
                main_module.create_growth_simulation(invalid, request)
            valid = GrowthSimulationRequest(initial_balance=1000, risk_tiers=[RiskTier(balance_threshold=0, risk_per_trade_pct=0.25)])
            result = main_module.create_growth_simulation(valid, request)
        self.assertEqual(result.bot_id, bot.id)


if __name__ == "__main__":
    unittest.main()
