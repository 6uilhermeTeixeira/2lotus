import logging
import sqlite3
from copy import copy
from hmac import compare_digest
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import SESSION_COOKIE_NAME, create_session_value, has_valid_session, password_matches, read_login_password
from .backtest import replay_settings_fingerprint
from .config import get_settings
from .growth import simulate_growth, simulate_tiered_growth
from .live_guard import apply_live_canary_guard
from .pages import render_catalog_page, render_comparison_page, render_growth_simulator_page, render_login_page, render_operations_page, render_results_page
from .results_store import load_bots_catalog, load_results_snapshot, resolve_bot_results_file
from .security import RateLimit, client_key, rate_limiter, require_same_origin
from .schemas import BotCatalogItem, CandleIngestRequest, CandleSnapshot, DecisionAction, DecisionRequest, DecisionResponse, EquityComparisonResponse, EquityPoint, EquitySeries, GrowthSimulationRequest, GrowthSimulationResponse, HealthResponse, HistoricalReplayResponse, MarketChartResponse, OperationsResponse, ResultsResponse, ResultsSummary, RuntimeStatusItem, TickIngestRequest, TradeIngestRequest
from .sqlite_store import SQLiteStore
from .strategy import HAPPY_GOLD_COMBINED_PROFILE, WEIGHTED_PORTFOLIO_PROFILES, evaluate_decision
from .supabase_store import SupabaseRequestError, SupabaseStore
from .telemetry import StepTimer, emit_event, metrics_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

logger = logging.getLogger("2lotus.strategy")
settings = get_settings()
app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
BRAND_ICON_PATH = Path(__file__).resolve().parent / "static" / "brand" / "icon-1-black.png"
CORE_BOT_IDS = {"core-zigzag", "core-stochastic", "core-multitimeframe", "core-portfolio"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        require_same_origin(request)
    except HTTPException as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(BRAND_ICON_PATH, media_type="image/png")


def _login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _require_page_session(request: Request) -> RedirectResponse | None:
    if not settings.dashboard_auth_configured:
        return RedirectResponse(url="/login?error=config", status_code=303)
    return None if has_valid_session(request, settings) else _login_redirect()


def _require_api_session(request: Request) -> None:
    if not settings.dashboard_auth_configured:
        raise HTTPException(status_code=503, detail="dashboard authentication is not configured")
    if not has_valid_session(request, settings):
        raise HTTPException(status_code=401, detail="dashboard authentication required")


def _find_bot(bot_id: str, *, include_archived: bool = False) -> BotCatalogItem:
    bot = next((item for item in _list_bots(None if include_archived else True) if item.id == bot_id), None)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    return bot


def _uses_supabase_results() -> bool:
    return settings.results_store == "SUPABASE"


def _uses_sqlite_results() -> bool:
    return settings.results_store == "SQLITE"


def _list_bots(active: bool | None = True) -> list[BotCatalogItem]:
    if _uses_supabase_results():
        bots = SupabaseStore(settings).list_bots(active)
        return [bot for bot in bots if bot.id in CORE_BOT_IDS]
    bots = load_bots_catalog(settings.bots_catalog_path)
    bots = [bot for bot in bots if bot.id in CORE_BOT_IDS]
    return bots if active is None else [bot for bot in bots if bot.active is active]


def _load_bot_results(bot: BotCatalogItem) -> ResultsResponse:
    if _uses_supabase_results():
        return SupabaseStore(settings).load_results(bot.id)
    if _uses_sqlite_results():
        return SQLiteStore(settings).load_results(bot.id)
    return load_results_snapshot(resolve_bot_results_file(settings.bots_catalog_path, bot))


def _replay_cache_matches_settings(replay: HistoricalReplayResponse, bot: BotCatalogItem | None = None) -> bool:
    risk_matches = round(replay.risk_per_trade_pct, 8) == round(settings.backtest_risk_per_trade_pct, 8)
    return risk_matches and (bot is None or replay.settings_fingerprint == replay_settings_fingerprint(settings, bot))


def _risk_scale_for_legacy_replay(bot: BotCatalogItem, replay: HistoricalReplayResponse) -> float | None:
    if round(replay.risk_per_trade_pct, 8) == round(settings.backtest_risk_per_trade_pct, 8):
        return 1.0
    legacy_risk = replay.risk_per_trade_pct
    if legacy_risk <= 0 and bot.strategy_profile == HAPPY_GOLD_COMBINED_PROFILE:
        legacy_risk = 2.0
    if legacy_risk <= 0:
        return None
    return settings.backtest_risk_per_trade_pct / legacy_risk


def _normalize_replay_for_equity(bot: BotCatalogItem, replay: HistoricalReplayResponse) -> HistoricalReplayResponse | None:
    scale = _risk_scale_for_legacy_replay(bot, replay)
    if scale is None:
        return None
    if scale == 1.0:
        return replay
    normalized = replay.model_copy(deep=True)
    normalized.risk_per_trade_pct = settings.backtest_risk_per_trade_pct
    normalized.daily_pnl = [
        point.model_copy(update={"net_pnl": round(point.net_pnl * scale, 2)})
        for point in normalized.daily_pnl
    ]
    normalized.summary = ResultsSummary(
        trades_count=replay.summary.trades_count,
        wins=replay.summary.wins,
        losses=replay.summary.losses,
        breakevens=replay.summary.breakevens,
        open_positions=replay.summary.open_positions,
        win_rate_pct=replay.summary.win_rate_pct,
        gross_profit=round(replay.summary.gross_profit * scale, 2),
        gross_loss=round(replay.summary.gross_loss * scale, 2),
        net_pnl=round(replay.summary.net_pnl * scale, 2),
        avg_win=round(replay.summary.avg_win * scale, 2),
        avg_loss=round(replay.summary.avg_loss * scale, 2),
        profit_factor=replay.summary.profit_factor,
        avg_rr=replay.summary.avg_rr,
        best_trade=round(replay.summary.best_trade * scale, 2),
        worst_trade=round(replay.summary.worst_trade * scale, 2),
    )
    return normalized


def _load_or_run_backtest_replay(store: SupabaseStore, bot: BotCatalogItem, days: int, *, include_candles: bool = False) -> HistoricalReplayResponse | None:
    cached = store.load_backtest_snapshot(bot.id, days)
    if cached is not None and _replay_cache_matches_settings(cached, bot):
        if include_candles:
            cached.candles = store.load_candles(bot, settings.chart_cache_limit)
        return cached
    return None


def _require_ingest_key(provided_key: str | None) -> None:
    if not settings.ingest_api_key:
        raise HTTPException(status_code=503, detail="trade ingestion is not configured")
    if not provided_key or not compare_digest(provided_key, settings.ingest_api_key):
        raise HTTPException(status_code=401, detail="invalid trade ingestion key")


def _validate_ingested_candles(candles: list[CandleSnapshot]) -> None:
    for candle in candles:
        prices = (candle.open, candle.high, candle.low, candle.close)
        if min(prices) <= 0:
            raise HTTPException(status_code=422, detail="OHLC prices must be positive")
        if candle.low > min(candle.open, candle.close) or candle.high < max(candle.open, candle.close) or candle.low > candle.high:
            raise HTTPException(status_code=422, detail="OHLC values are inconsistent")


@app.get("/", include_in_schema=False)
def root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/bots" if has_valid_session(request, settings) else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def login_page(request: Request, error: str | None = None) -> HTMLResponse | RedirectResponse:
    if has_valid_session(request, settings):
        return RedirectResponse(url="/bots", status_code=303)
    if error == "config" or not settings.dashboard_auth_configured:
        return render_login_page("O acesso ainda nao foi configurado neste ambiente.", status_code=503)
    return render_login_page()


@app.post("/login", response_model=None, include_in_schema=False)
async def login(request: Request) -> RedirectResponse | HTMLResponse:
    if not settings.dashboard_auth_configured:
        return render_login_page("O acesso ainda nao foi configurado neste ambiente.", status_code=503)
    rate_key = client_key(request, "login")
    rate_limiter.check(rate_key, RateLimit(settings.login_rate_limit_attempts, 300))
    if not password_matches(await read_login_password(request), settings):
        return render_login_page("Senha invalida.", status_code=401)
    rate_limiter.reset(rate_key)
    response = RedirectResponse(url="/bots", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_value(settings),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/bots", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def bots_page(request: Request) -> HTMLResponse | RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    return render_catalog_page(_list_bots(True))


@app.get("/compare", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def comparison_page(request: Request) -> HTMLResponse | RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    return render_comparison_page(_list_bots())


@app.get("/growth-simulator", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def growth_simulator_page(request: Request) -> HTMLResponse | RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    return render_growth_simulator_page()


@app.get("/operations", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def operations_page(request: Request) -> HTMLResponse | RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    return render_operations_page()


@app.get("/bots/{bot_id}", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def bot_results_page(bot_id: str, request: Request) -> HTMLResponse | RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    return render_results_page(_find_bot(bot_id, include_archived=True))


@app.get("/results", include_in_schema=False)
def legacy_results(request: Request) -> RedirectResponse:
    if redirect := _require_page_session(request):
        return redirect
    bots = _list_bots()
    return RedirectResponse(url=f"/bots/{bots[0].id}" if bots else "/bots", status_code=303)


@app.get("/api/bots", response_model=list[BotCatalogItem])
def read_bots(request: Request, archived: bool = False) -> list[BotCatalogItem]:
    _require_api_session(request)
    if archived:
        raise HTTPException(status_code=410, detail="archived strategies were retired")
    return _list_bots(True)


@app.get("/api/compare")
def read_comparison(request: Request) -> dict[str, object]:
    _require_api_session(request)
    bots = _list_bots()
    snapshots = []
    for bot in bots:
        results = _load_bot_results(bot)
        snapshots.append(
            {
                "id": bot.id,
                "name": bot.name,
                "strategy_profile": bot.strategy_profile,
                "symbol": bot.symbol,
                "timeframe": bot.timeframe,
                "latest_trade_at_utc": results.latest_trade_at_utc,
                "summary": results.summary.model_dump(mode="json"),
            }
        )
    return {"updated_at_utc": datetime.now(timezone.utc), "bots": snapshots}


@app.get("/api/equity-comparison", response_model=EquityComparisonResponse)
def read_equity_comparison(request: Request, symbol: str = "XAUUSD", days: int = 365) -> EquityComparisonResponse:
    _require_api_session(request)
    normalized_symbol = symbol.upper()
    if days < 1 or days > 365:
        raise HTTPException(status_code=422, detail="comparison days must be between 1 and 365")
    bots = [bot for bot in _list_bots() if bot.symbol.upper() == normalized_symbol and bot.timeframe.upper() == "M5"]
    if not bots:
        raise HTTPException(status_code=404, detail="no bots configured for symbol")
    series: list[EquitySeries] = []
    missing: list[str] = []
    if not _uses_supabase_results():
        for bot in bots:
            results = _load_bot_results(bot)
            cumulative = 0.0
            points: list[EquityPoint] = []
            for point in sorted(results.daily_pnl, key=lambda item: item.day):
                cumulative += point.net_pnl
                points.append(EquityPoint(day=point.day, cumulative_pnl=round(cumulative, 2)))
            if not points:
                missing.append(bot.id)
                continue
            series.append(
                EquitySeries(
                    bot_id=bot.id,
                    name=bot.name,
                    strategy_profile=bot.strategy_profile,
                    summary=results.summary,
                    points=points,
                )
            )
        return EquityComparisonResponse(
            symbol=normalized_symbol,
            timeframe="M5",
            period_days=days,
            updated_at_utc=datetime.now(timezone.utc),
            series=series,
            missing_bot_ids=missing,
        )
    store = SupabaseStore(settings)
    for bot in bots:
        replay = store.load_backtest_snapshot(bot.id, days)
        if replay is not None:
            replay = _normalize_replay_for_equity(bot, replay)
        if replay is None:
            missing.append(bot.id)
            continue
        cumulative = 0.0
        points: list[EquityPoint] = []
        for point in sorted(replay.daily_pnl, key=lambda item: item.day):
            cumulative += point.net_pnl
            points.append(EquityPoint(day=point.day, cumulative_pnl=round(cumulative, 2)))
        series.append(EquitySeries(bot_id=bot.id, name=bot.name, strategy_profile=bot.strategy_profile, summary=replay.summary, points=points))
    return EquityComparisonResponse(
        symbol=normalized_symbol,
        timeframe="M5",
        period_days=days,
        updated_at_utc=datetime.now(timezone.utc),
        series=series,
        missing_bot_ids=missing,
    )


@app.get("/api/growth-simulation", response_model=GrowthSimulationResponse)
def read_growth_simulation(
    request: Request,
    initial_balance: float = 10000,
    risk_per_trade_pct: float = 0.25,
) -> GrowthSimulationResponse:
    _require_api_session(request)
    if not 100 <= initial_balance <= 100_000_000:
        raise HTTPException(status_code=422, detail="initial balance must be between 100 and 100000000")
    if not 0.01 <= risk_per_trade_pct <= 10:
        raise HTTPException(status_code=422, detail="risk per trade must be between 0.01 and 10")
    if not _uses_supabase_results():
        raise HTTPException(status_code=503, detail="growth simulation requires DAYTRADE_RESULTS_STORE=SUPABASE")
    bot = _find_bot("core-portfolio", include_archived=True)
    replay = SupabaseStore(settings).load_backtest_snapshot(bot.id, 365)
    if replay is None or not _replay_cache_matches_settings(replay, bot):
        raise HTTPException(status_code=503, detail="the annual 0.25 percent replay must be recalculated first")
    return GrowthSimulationResponse(
        bot_id=bot.id,
        bot_name=bot.name,
        period_start_utc=replay.period_start_utc,
        period_end_utc=replay.period_end_utc,
        reference=simulate_growth(replay, settings.backtest_balance, settings.backtest_risk_per_trade_pct, label="Referência"),
        simulation=simulate_growth(replay, initial_balance, risk_per_trade_pct, label="Simulação"),
    )


@app.post("/api/growth-simulation", response_model=GrowthSimulationResponse)
def create_growth_simulation(payload: GrowthSimulationRequest, request: Request) -> GrowthSimulationResponse:
    _require_api_session(request)
    if not _uses_supabase_results():
        raise HTTPException(status_code=503, detail="growth simulation requires DAYTRADE_RESULTS_STORE=SUPABASE")
    tiers = sorted(payload.risk_tiers, key=lambda tier: tier.balance_threshold)
    if tiers[0].balance_threshold > payload.initial_balance:
        raise HTTPException(status_code=422, detail="the first tier must start at or below the initial balance")
    if any(tiers[index].risk_per_trade_pct > tiers[index - 1].risk_per_trade_pct for index in range(1, len(tiers))):
        raise HTTPException(status_code=422, detail="risk must stay equal or decrease at higher balance tiers")
    bot = _find_bot("core-portfolio")
    replay = SupabaseStore(settings).load_backtest_snapshot(bot.id, 365)
    if replay is None or not _replay_cache_matches_settings(replay, bot):
        raise HTTPException(status_code=503, detail="the annual replay cache is unavailable or stale")
    return GrowthSimulationResponse(
        bot_id=bot.id,
        bot_name=bot.name,
        period_start_utc=replay.period_start_utc,
        period_end_utc=replay.period_end_utc,
        reference=simulate_growth(replay, settings.backtest_balance, settings.backtest_risk_per_trade_pct, label="Referência"),
        simulation=simulate_tiered_growth(replay, payload.initial_balance, tiers, label="Simulação"),
    )


@app.get("/api/operations", response_model=OperationsResponse)
def read_operations(request: Request) -> OperationsResponse:
    _require_api_session(request)
    bots = _list_bots()
    if _uses_supabase_results():
        statuses, snapshots = SupabaseStore(settings).load_operations()
    elif _uses_sqlite_results():
        statuses, snapshots = SQLiteStore(settings).load_operations()
    else:
        statuses, snapshots = [], []
    status_by_bot = {str(row.get("bot_id")): row for row in statuses}
    snapshot_by_bot = {str(row.get("bot_id")): row for row in snapshots}
    now = datetime.now(timezone.utc)
    items: list[RuntimeStatusItem] = []
    for bot in bots:
        state = status_by_bot.get(bot.id, {})
        snapshot = snapshot_by_bot.get(bot.id, {})
        last_request = datetime.fromisoformat(str(state["last_request_at_utc"])) if state.get("last_request_at_utc") else None
        last_success = datetime.fromisoformat(str(state["last_success_at_utc"])) if state.get("last_success_at_utc") else last_request
        last_candle = datetime.fromisoformat(str(state["last_candle_at_utc"])) if state.get("last_candle_at_utc") else None
        consecutive_errors = int(state.get("consecutive_errors") or 0)
        if consecutive_errors:
            health = "degraded"
        elif now.weekday() >= 5:
            health = "market_closed"
        elif last_success is None:
            health = "no_data"
        else:
            age_minutes = (now - last_success).total_seconds() / 60
            health = "healthy" if age_minutes <= 15 else "delayed" if age_minutes <= 30 else "stale"
        market_lag = max(0.0, (now - last_candle).total_seconds()) if last_candle else None
        items.append(RuntimeStatusItem(
            bot_id=bot.id, name=bot.name, status=health,
            last_request_at_utc=last_request,
            last_attempt_at_utc=state.get("last_attempt_at_utc"), last_success_at_utc=last_success,
            last_candle_at_utc=state.get("last_candle_at_utc"), last_action=state.get("last_action"), last_reason=state.get("last_reason"),
            last_latency_ms=state.get("last_latency_ms"), consecutive_errors=consecutive_errors, last_error=state.get("last_error"), last_error_code=state.get("last_error_code"), market_lag_seconds=market_lag,
            snapshot_updated_at_utc=snapshot.get("updated_at"), market_data_through_utc=snapshot.get("market_data_through_utc"),
        ))
    return OperationsResponse(updated_at_utc=now, bots=items)


@app.get("/api/bots/{bot_id}/results", response_model=ResultsResponse)
def read_bot_results(bot_id: str, request: Request) -> ResultsResponse:
    _require_api_session(request)
    return _load_bot_results(_find_bot(bot_id, include_archived=True))


@app.get("/api/bots/{bot_id}/chart", response_model=MarketChartResponse)
def read_bot_chart(bot_id: str, request: Request) -> MarketChartResponse:
    _require_api_session(request)
    bot = _find_bot(bot_id, include_archived=True)
    if _uses_sqlite_results():
        return SQLiteStore(settings).load_chart(bot.id, bot.symbol, bot.timeframe, settings.chart_cache_limit)
    if not _uses_supabase_results():
        return MarketChartResponse(bot_id=bot.id, symbol=bot.symbol, timeframe=bot.timeframe, candles=[], trades=[])
    return SupabaseStore(settings).load_chart(bot, settings.chart_cache_limit)


@app.get("/api/bots/{bot_id}/historical-replay", response_model=HistoricalReplayResponse)
def read_historical_replay(bot_id: str, request: Request, days: int = 120) -> HistoricalReplayResponse:
    _require_api_session(request)
    if days < 1 or days > 365:
        raise HTTPException(status_code=422, detail="replay days must be between 1 and 365")
    bot = _find_bot(bot_id, include_archived=True)
    if not _uses_supabase_results():
        raise HTTPException(status_code=503, detail="historical replay requires DAYTRADE_RESULTS_STORE=SUPABASE")
    store = SupabaseStore(settings)
    replay = _load_or_run_backtest_replay(store, bot, days, include_candles=True)
    if replay is None:
        raise HTTPException(status_code=404, detail="no historical candles available for replay")
    return replay


@app.get("/api/results", response_model=ResultsResponse, deprecated=True)
def read_legacy_results(request: Request) -> ResultsResponse:
    _require_api_session(request)
    bots = _list_bots()
    if not bots:
        raise HTTPException(status_code=404, detail="no bots configured")
    return _load_bot_results(bots[0])


@app.post("/api/trades", status_code=204)
def ingest_trade(payload: TradeIngestRequest, request: Request, x_2lotus_ingest_key: str | None = Header(default=None)) -> None:
    rate_limiter.check(client_key(request, "ingest"), RateLimit(settings.ingest_rate_limit_per_minute, 60))
    _require_ingest_key(x_2lotus_ingest_key)
    _find_bot(payload.bot_id)
    if _uses_supabase_results():
        SupabaseStore(settings).upsert_trade(payload.bot_id, payload.trade)
    elif _uses_sqlite_results():
        SQLiteStore(settings).upsert_trade(payload.bot_id, payload.trade)
    else:
        raise HTTPException(status_code=503, detail="trade ingestion requires a persistent results store")


@app.post("/api/candles", status_code=204)
def ingest_candles(payload: CandleIngestRequest, request: Request, x_2lotus_ingest_key: str | None = Header(default=None)) -> None:
    rate_limiter.check(client_key(request, "ingest"), RateLimit(settings.ingest_rate_limit_per_minute, 60))
    _require_ingest_key(x_2lotus_ingest_key)
    _validate_ingested_candles(payload.candles)
    bot = _find_bot(payload.bot_id)
    if bot.symbol != payload.symbol or bot.timeframe != payload.timeframe:
        raise HTTPException(status_code=422, detail="candle symbol or timeframe does not match bot configuration")
    if _uses_supabase_results():
        SupabaseStore(settings).upsert_candles(payload.bot_id, payload.symbol, payload.timeframe, payload.candles)
    elif _uses_sqlite_results():
        SQLiteStore(settings).upsert_candles(payload.bot_id, payload.symbol, payload.timeframe, payload.candles)
    else:
        raise HTTPException(status_code=503, detail="candle ingestion requires a persistent results store")


@app.post("/api/ticks", status_code=204)
def ingest_ticks(payload: TickIngestRequest, request: Request, x_2lotus_ingest_key: str | None = Header(default=None)) -> None:
    rate_limiter.check(client_key(request, "tick_ingest"), RateLimit(settings.tick_ingest_rate_limit_per_minute, 60))
    _require_ingest_key(x_2lotus_ingest_key)
    if _uses_supabase_results():
        store = SupabaseStore(settings)
        store.upsert_ticks(payload)
        store.advance_tick_shadow_trades(payload)
    elif _uses_sqlite_results():
        SQLiteStore(settings).upsert_ticks(payload)
    else:
        raise HTTPException(status_code=503, detail="tick ingestion requires a persistent results store")


@app.get("/health", response_model=HealthResponse)
def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok", service=settings.app_name, timestamp_utc=datetime.now(timezone.utc))


@app.get("/ready")
def readiness() -> JSONResponse:
    if _uses_supabase_results():
        if not settings.supabase_results_configured:
            return JSONResponse(status_code=503, content={"status": "not_ready", "checks": {"configuration": "failed", "supabase": "not_checked"}})
        try:
            SupabaseStore(settings).readiness()
        except SupabaseRequestError as error:
            return JSONResponse(status_code=503, content={"status": "not_ready", "checks": {"configuration": "passed", "supabase": error.code}})
    if _uses_sqlite_results():
        try:
            SQLiteStore(settings).readiness()
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=503, content={"status": "not_ready", "checks": {"configuration": "passed", "sqlite": "failed"}})
    checks = {"configuration": "passed", "supabase": "passed" if _uses_supabase_results() else "disabled"}
    if _uses_sqlite_results():
        checks["sqlite"] = "passed"
    return JSONResponse(content={"status": "ready", "checks": checks})


@app.get("/api/metrics")
def read_metrics(request: Request) -> dict[str, object]:
    _require_api_session(request)
    return metrics_snapshot()


@app.get("/config")
def read_config(request: Request) -> dict[str, str | int | float | bool]:
    _require_api_session(request)
    return {
        "app_name": settings.app_name,
        "host": settings.host,
        "port": settings.port,
        "strategy_profile": settings.strategy_profile,
        "default_symbol": settings.default_symbol,
        "default_timeframe": settings.default_timeframe,
        "max_spread_points": settings.max_spread_points,
        "risk_per_trade_pct": settings.risk_per_trade_pct,
        "min_risk_reward": settings.min_risk_reward,
        "fimath_mode": settings.fimath_mode,
        "fimath_flow_window_seconds": settings.fimath_flow_window_seconds,
        "fimath_flow_min_samples": settings.fimath_flow_min_samples,
        "fimath_flow_z_threshold": settings.fimath_flow_z_threshold,
        "fimath_flow_policy": settings.fimath_flow_policy,
        "fimath_partial_pct": settings.fimath_partial_pct,
        "fimath_min_closed_paper_trades": settings.fimath_min_closed_paper_trades,
        "chart_cache_limit": settings.chart_cache_limit,
        "backtest_balance": settings.backtest_balance,
        "backtest_spread_points": settings.backtest_spread_points,
        "backtest_candle_limit": settings.backtest_candle_limit,
        "backtest_risk_per_trade_pct": settings.backtest_risk_per_trade_pct,
        "backtest_slippage_points": settings.backtest_slippage_points,
        "backtest_commission_per_lot": settings.backtest_commission_per_lot,
        "backtest_session_start_utc": settings.backtest_session_start_utc,
        "backtest_session_end_utc": settings.backtest_session_end_utc,
        "backtest_regime_enabled": settings.backtest_regime_enabled,
        "happy_gold_break_even_trigger_points": settings.happy_gold_break_even_trigger_points,
        "happy_gold_break_even_offset_points": settings.happy_gold_break_even_offset_points,
        "happy_gold_trailing_activation_points": settings.happy_gold_trailing_activation_points,
        "happy_gold_trailing_distance_points": settings.happy_gold_trailing_distance_points,
        "happy_gold_max_holding_bars": settings.happy_gold_max_holding_bars,
        "happy_gold_combined_session_start_utc": settings.happy_gold_combined_session_start_utc,
        "happy_gold_combined_session_end_utc": settings.happy_gold_combined_session_end_utc,
        "enable_live_decisions": settings.enable_live_decisions,
        "dashboard_auth_configured": settings.dashboard_auth_configured,
        "results_store": settings.results_store,
        "supabase_results_configured": settings.supabase_results_configured,
    }


@app.post("/decision", response_model=DecisionResponse)
def decision(payload: DecisionRequest, request: Request, x_2lotus_ingest_key: str | None = Header(default=None)) -> DecisionResponse:
    run_id = payload.request_id
    started_at = monotonic()
    run_timer = StepTimer(run_id, "decision_run")
    rate_limiter.check(client_key(request, "ingest"), RateLimit(settings.ingest_rate_limit_per_minute, 60))
    _require_ingest_key(x_2lotus_ingest_key)
    _validate_ingested_candles(payload.candles)
    store: SupabaseStore | None = None
    strategy_profile = settings.strategy_profile
    try:
        if payload.bot_id:
            bot = _find_bot(payload.bot_id)
            strategy_profile = bot.strategy_profile
            if bot.symbol != payload.symbol or bot.timeframe != payload.timeframe:
                raise HTTPException(status_code=422, detail="decision symbol or timeframe does not match bot configuration")
        if payload.bot_id and _uses_supabase_results():
            store = SupabaseStore(settings)
            cached_response = store.load_decision_response(payload.bot_id, payload.request_id)
            if cached_response is not None:
                emit_event(run_id=run_id, step="idempotency_lookup", status="replayed", bot_id=payload.bot_id)
                try:
                    store.record_runtime_status(payload.bot_id, request_at=payload.timestamp_utc, candle_at=payload.candles[-1].timestamp_utc if payload.candles else None, action=cached_response.action.value, reason=cached_response.reason, latency_ms=(monotonic() - started_at) * 1000)
                except SupabaseRequestError as telemetry_error:
                    emit_event(run_id=run_id, step="persist_runtime_status", status="failed", error_code=telemetry_error.code, bot_id=payload.bot_id)
                run_timer.finish(status="passed", bot_id=payload.bot_id, outcome="idempotent_replay")
                return cached_response
            store.upsert_candles(payload.bot_id, payload.symbol, payload.timeframe, payload.candles)
            store.advance_paper_trades(payload.bot_id, payload.candles)
            if strategy_profile in {"multitimeframe_trend_pullback", "happy_gold_zigzag_scalper", "happy_gold_zigzag_regime_confirmed", HAPPY_GOLD_COMBINED_PROFILE, *WEIGHTED_PORTFOLIO_PROFILES}:
                payload.candles = store.load_candles(
                    bot,
                    4000 if strategy_profile in {"happy_gold_zigzag_regime_confirmed", HAPPY_GOLD_COMBINED_PROFILE} or strategy_profile in WEIGHTED_PORTFOLIO_PROFILES else 1000,
                )
        strategy_settings = settings
        if payload.bot_id:
            strategy_settings = copy(settings)
            strategy_settings.default_symbol = payload.symbol
            strategy_settings.default_timeframe = payload.timeframe
            if (strategy_profile in WEIGHTED_PORTFOLIO_PROFILES or strategy_profile == HAPPY_GOLD_COMBINED_PROFILE) and store:
                strategy_settings.portfolio_risk_state = store.load_portfolio_risk_state(payload.bot_id, payload.timestamp_utc)
        response = evaluate_decision(payload, strategy_settings, strategy_profile)
        if payload.bot_id == settings.live_canary_bot_id:
            response = apply_live_canary_guard(
                payload,
                response,
                settings,
                persistent_risk_state_available=store is not None,
            )
        if payload.bot_id and store and response.metadata.get("candidate"):
            signal_key = store.record_strategy_candidate(payload, response)
            if signal_key and response.metadata.get("tick_shadow_lifecycle"):
                store.record_tick_shadow_trade(signal_key, payload, response)
            if signal_key and response.metadata.get("record_paper_trade", True):
                store.record_paper_trade(signal_key, payload, response, "technical")
                if response.metadata.get("flow_confirmed"):
                    store.record_paper_trade(signal_key, payload, response, "flow_confirmed")
        if payload.bot_id and store and response.action.value in {"buy", "sell"}:
            validation_variant = "flow_confirmed" if strategy_profile == "fimath_ema20_flow" and settings.fimath_flow_policy == "required" else "technical"
            closed_count = store.closed_paper_trade_count(payload.bot_id, validation_variant)
            response.metadata["validation_variant"] = validation_variant
            response.metadata["closed_paper_trades"] = closed_count
            if closed_count < settings.fimath_min_closed_paper_trades:
                response.action = DecisionAction.HOLD
                response.reason = "shadow_minimum_not_reached"
                response.size = 0.0
        if payload.bot_id and store:
            store.save_decision_response(payload.bot_id, payload.request_id, response)
            try:
                store.record_runtime_status(payload.bot_id, request_at=payload.timestamp_utc, candle_at=payload.candles[-1].timestamp_utc if payload.candles else None, action=response.action.value, reason=response.reason, latency_ms=(monotonic() - started_at) * 1000)
            except SupabaseRequestError as telemetry_error:
                emit_event(run_id=run_id, step="persist_runtime_status", status="failed", error_code=telemetry_error.code, bot_id=payload.bot_id)
        if _uses_sqlite_results():
            SQLiteStore(settings).record_decision(payload, response, (monotonic() - started_at) * 1000)
        run_timer.finish(status="passed", bot_id=payload.bot_id, outcome=response.reason)
        return response
    except SupabaseRequestError as error:
        run_timer.finish(status="failed", bot_id=payload.bot_id, error_code=error.code)
        if payload.bot_id and store:
            try:
                store.record_runtime_failure(payload.bot_id, error_code=error.code, error_message=str(error))
            except SupabaseRequestError:
                pass
        raise HTTPException(status_code=503, detail=f"decision dependency unavailable: {error.code}") from error
