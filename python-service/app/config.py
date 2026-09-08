from functools import lru_cache
from os import getenv
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        service_root = Path(__file__).resolve().parent.parent
        self.lotus_data_root = Path(getenv("LOTUS_DATA_ROOT", "/home/codexops/lotus-data"))
        self.app_name = getenv("DAYTRADE_APP_NAME", "live-core")
        self.host = getenv("DAYTRADE_HOST", "127.0.0.1")
        self.port = int(getenv("DAYTRADE_PORT", "1111"))
        self.strategy_profile = getenv("DAYTRADE_STRATEGY_PROFILE", "portfolio_v3_adaptive_union_shadow")
        self.default_symbol = getenv("DAYTRADE_DEFAULT_SYMBOL", "XAUUSD")
        self.default_timeframe = getenv("DAYTRADE_DEFAULT_TIMEFRAME", "M5")
        self.max_spread_points = float(getenv("DAYTRADE_MAX_SPREAD_POINTS", "50.0"))
        self.risk_per_trade_pct = float(getenv("DAYTRADE_RISK_PER_TRADE_PCT", "2.0"))
        self.min_risk_reward = float(getenv("DAYTRADE_MIN_RISK_REWARD", "2.0"))
        self.enable_live_decisions = getenv("DAYTRADE_ENABLE_LIVE_DECISIONS", "false").lower() == "true"
        self.live_canary_armed = getenv("LIVE_CANARY_ARMED", "false").lower() == "true"
        self.live_canary_account_login = int(getenv("LIVE_CANARY_ACCOUNT_LOGIN", "0"))
        self.live_canary_bot_id = getenv("LIVE_CANARY_BOT_ID", "")
        self.live_canary_max_lot = float(getenv("LIVE_CANARY_MAX_LOT", "0.01"))
        self.live_canary_max_spread_points = float(getenv("LIVE_CANARY_MAX_SPREAD_POINTS", "15"))
        self.fimath_mode = getenv("DAYTRADE_FIMATH_MODE", "shadow").lower()
        self.fimath_flow_window_seconds = int(getenv("DAYTRADE_FIMATH_FLOW_WINDOW_SECONDS", "300"))
        self.fimath_flow_min_samples = int(getenv("DAYTRADE_FIMATH_FLOW_MIN_SAMPLES", "30"))
        self.fimath_flow_z_threshold = float(getenv("DAYTRADE_FIMATH_FLOW_Z_THRESHOLD", "1.0"))
        self.fimath_flow_policy = getenv("DAYTRADE_FIMATH_FLOW_POLICY", "veto").lower()
        if self.fimath_flow_policy not in {"required", "veto"}:
            raise ValueError("DAYTRADE_FIMATH_FLOW_POLICY must be 'required' or 'veto'")
        self.fimath_partial_pct = float(getenv("DAYTRADE_FIMATH_PARTIAL_PCT", "50"))
        self.fimath_min_closed_paper_trades = int(getenv("DAYTRADE_FIMATH_MIN_CLOSED_PAPER_TRADES", "30"))
        self.chart_cache_limit = int(getenv("DAYTRADE_CHART_CACHE_LIMIT", "5000"))
        self.backtest_balance = float(getenv("DAYTRADE_BACKTEST_BALANCE", "10000"))
        self.backtest_spread_points = float(getenv("DAYTRADE_BACKTEST_SPREAD_POINTS", "0"))
        self.backtest_point_size = float(getenv("DAYTRADE_BACKTEST_POINT_SIZE", "0.01"))
        self.backtest_value_per_point_per_lot = float(getenv("DAYTRADE_BACKTEST_VALUE_PER_POINT_PER_LOT", "1"))
        self.backtest_candle_limit = int(getenv("DAYTRADE_BACKTEST_CANDLE_LIMIT", "100000"))
        self.backtest_risk_per_trade_pct = float(getenv("DAYTRADE_BACKTEST_RISK_PER_TRADE_PCT", "1"))
        self.backtest_slippage_points = float(getenv("DAYTRADE_BACKTEST_SLIPPAGE_POINTS", "2"))
        self.backtest_commission_per_lot = float(getenv("DAYTRADE_BACKTEST_COMMISSION_PER_LOT", "4.50"))
        self.backtest_session_start_utc = getenv("DAYTRADE_BACKTEST_SESSION_START_UTC", "06:00")
        self.backtest_session_end_utc = getenv("DAYTRADE_BACKTEST_SESSION_END_UTC", "21:00")
        self.backtest_max_trades_per_day = int(getenv("DAYTRADE_BACKTEST_MAX_TRADES_PER_DAY", "4"))
        self.backtest_daily_loss_limit_r = float(getenv("DAYTRADE_BACKTEST_DAILY_LOSS_LIMIT_R", "2"))
        self.backtest_cooldown_bars_after_loss = int(getenv("DAYTRADE_BACKTEST_COOLDOWN_BARS_AFTER_LOSS", "3"))
        self.backtest_regime_enabled = getenv("DAYTRADE_BACKTEST_REGIME_ENABLED", "true").lower() == "true"
        self.backtest_regime_trend_adx = float(getenv("DAYTRADE_BACKTEST_REGIME_TREND_ADX", "25"))
        self.backtest_regime_range_adx = float(getenv("DAYTRADE_BACKTEST_REGIME_RANGE_ADX", "20"))
        self.backtest_regime_high_volatility_percentile = float(getenv("DAYTRADE_BACKTEST_REGIME_HIGH_VOLATILITY_PERCENTILE", "90"))
        self.backtest_validation_fold_days = int(getenv("DAYTRADE_BACKTEST_VALIDATION_FOLD_DAYS", "30"))
        self.happy_gold_break_even_trigger_points = float(getenv("DAYTRADE_HAPPY_GOLD_BREAK_EVEN_TRIGGER_POINTS", "40"))
        self.happy_gold_break_even_offset_points = float(getenv("DAYTRADE_HAPPY_GOLD_BREAK_EVEN_OFFSET_POINTS", "0"))
        self.happy_gold_trailing_activation_points = float(getenv("DAYTRADE_HAPPY_GOLD_TRAILING_ACTIVATION_POINTS", "120"))
        self.happy_gold_trailing_distance_points = float(getenv("DAYTRADE_HAPPY_GOLD_TRAILING_DISTANCE_POINTS", "60"))
        self.happy_gold_max_holding_bars = int(getenv("DAYTRADE_HAPPY_GOLD_MAX_HOLDING_BARS", "2"))
        self.happy_gold_combined_session_start_utc = getenv("DAYTRADE_HAPPY_GOLD_COMBINED_SESSION_START_UTC", "06:00")
        self.happy_gold_combined_session_end_utc = getenv("DAYTRADE_HAPPY_GOLD_COMBINED_SESSION_END_UTC", "19:00")
        self.happy_gold_impulse_30s_threshold_pips = float(getenv("DAYTRADE_HAPPY_GOLD_IMPULSE_30S_THRESHOLD_PIPS", "95.5"))
        self.happy_gold_zigzag_tick_context_900s_threshold_pips = float(
            getenv("DAYTRADE_HAPPY_GOLD_ZIGZAG_TICK_CONTEXT_900S_THRESHOLD_PIPS", "800")
        )
        self.happy_gold_structural_context_900s_threshold_pips = float(
            getenv("DAYTRADE_HAPPY_GOLD_STRUCTURAL_CONTEXT_900S_THRESHOLD_PIPS", "1000")
        )
        self.happy_gold_structural_ema20_gap_pips = float(
            getenv("DAYTRADE_HAPPY_GOLD_STRUCTURAL_EMA20_GAP_PIPS", "900")
        )
        self.happy_gold_structural_max_pivot_extension_pips = float(
            getenv("DAYTRADE_HAPPY_GOLD_STRUCTURAL_MAX_PIVOT_EXTENSION_PIPS", "50")
        )
        self.happy_gold_normalized_range_max = float(
            getenv("DAYTRADE_HAPPY_GOLD_NORMALIZED_RANGE_MAX", "1.876955192")
        )
        self.happy_gold_normalized_range_max_spread_points = float(
            getenv("DAYTRADE_HAPPY_GOLD_NORMALIZED_RANGE_MAX_SPREAD_POINTS", "15")
        )
        self.zigzag_scalper_v3_expansion_threshold = float(
            getenv("DAYTRADE_ZIGZAG_SCALPER_V3_EXPANSION_THRESHOLD", "6.0")
        )
        self.stochastic_sell_v2_threshold = float(getenv("DAYTRADE_STOCHASTIC_SELL_V2_THRESHOLD", "85"))
        self.stochastic_sell_v2_target_rr = float(getenv("DAYTRADE_STOCHASTIC_SELL_V2_TARGET_RR", "1.25"))
        self.stochastic_sell_v2_partial_pct = float(getenv("DAYTRADE_STOCHASTIC_SELL_V2_PARTIAL_PCT", "50"))
        self.stochastic_sell_v2_cutoff_hour = int(getenv("DAYTRADE_STOCHASTIC_SELL_V2_CUTOFF_HOUR", "15"))
        self.stochastic_sell_v3_cutoff_hour = int(getenv("DAYTRADE_STOCHASTIC_SELL_V3_CUTOFF_HOUR", "13"))
        self.multitimeframe_v2_strength_min = float(getenv("DAYTRADE_MULTITIMEFRAME_V2_STRENGTH_MIN", "0.5"))
        self.multitimeframe_v2_session_start_hour = int(getenv("DAYTRADE_MULTITIMEFRAME_V2_SESSION_START_HOUR", "13"))
        self.multitimeframe_v2_session_end_hour = int(getenv("DAYTRADE_MULTITIMEFRAME_V2_SESSION_END_HOUR", "16"))
        self.multitimeframe_v2_stop_atr = float(getenv("DAYTRADE_MULTITIMEFRAME_V2_STOP_ATR", "1.2"))
        self.multitimeframe_v2_target_rr = float(getenv("DAYTRADE_MULTITIMEFRAME_V2_TARGET_RR", "1.25"))
        self.multitimeframe_v2_max_holding_bars = int(getenv("DAYTRADE_MULTITIMEFRAME_V2_MAX_HOLDING_BARS", "6"))
        self.happy_gold_impulse_session_start_utc = getenv("DAYTRADE_HAPPY_GOLD_IMPULSE_SESSION_START_UTC", "00:00")
        self.happy_gold_impulse_session_end_utc = getenv("DAYTRADE_HAPPY_GOLD_IMPULSE_SESSION_END_UTC", "17:00")
        self.happy_gold_multiwindow_shadow_enabled = getenv("DAYTRADE_HAPPY_GOLD_MULTIWINDOW_SHADOW_ENABLED", "false").lower() == "true"
        self.results_store_path = Path(
            getenv("DAYTRADE_RESULTS_STORE_PATH", str(service_root / "data" / "trade-results.json"))
        )
        self.bots_catalog_path = Path(getenv("DAYTRADE_BOTS_CATALOG_PATH", str(service_root / "data" / "bots.json")))
        self.dashboard_password = getenv("DAYTRADE_DASHBOARD_PASSWORD")
        self.session_secret = getenv("DAYTRADE_SESSION_SECRET")
        self.session_max_age_seconds = int(getenv("DAYTRADE_SESSION_MAX_AGE_SECONDS", "43200"))
        self.results_store = getenv("DAYTRADE_RESULTS_STORE", "JSON").upper()
        self.sqlite_path = Path(getenv("DAYTRADE_SQLITE_PATH", str(service_root / "data" / "runtime.db")))
        self.strategy_registry_path = Path(
            getenv("LOTUS_STRATEGY_REGISTRY_PATH", str(service_root.parent / ".runtime" / "strategies"))
        )
        self.supabase_url = getenv("DAYTRADE_SUPABASE_URL")
        self.supabase_service_role_key = getenv("DAYTRADE_SUPABASE_SERVICE_ROLE_KEY")
        self.ingest_api_key = getenv("DAYTRADE_INGEST_API_KEY")
        self.supabase_timeout_seconds = float(getenv("DAYTRADE_SUPABASE_TIMEOUT_SECONDS", "2.0"))
        self.supabase_retry_attempts = int(getenv("DAYTRADE_SUPABASE_RETRY_ATTEMPTS", "2"))
        self.login_rate_limit_attempts = int(getenv("DAYTRADE_LOGIN_RATE_LIMIT_ATTEMPTS", "5"))
        self.ingest_rate_limit_per_minute = int(getenv("DAYTRADE_INGEST_RATE_LIMIT_PER_MINUTE", "180"))
        self.tick_ingest_rate_limit_per_minute = int(getenv("DAYTRADE_TICK_INGEST_RATE_LIMIT_PER_MINUTE", "120"))
        self._validate()

    def _validate(self) -> None:
        if self.results_store not in {"JSON", "SQLITE", "SUPABASE"}:
            raise ValueError("DAYTRADE_RESULTS_STORE must be JSON, SQLITE or SUPABASE")
        if self.fimath_mode not in {"shadow", "live"}:
            raise ValueError("DAYTRADE_FIMATH_MODE must be shadow or live")
        if not 0 < self.risk_per_trade_pct <= 10:
            raise ValueError("DAYTRADE_RISK_PER_TRADE_PCT must be between 0 and 10")
        if not 0 < self.backtest_risk_per_trade_pct <= 10:
            raise ValueError("DAYTRADE_BACKTEST_RISK_PER_TRADE_PCT must be between 0 and 10")
        if self.session_max_age_seconds <= 0:
            raise ValueError("DAYTRADE_SESSION_MAX_AGE_SECONDS must be positive")
        if self.supabase_timeout_seconds <= 0 or not 1 <= self.supabase_retry_attempts <= 5:
            raise ValueError("Supabase timeout/retry settings are invalid")
        if self.tick_ingest_rate_limit_per_minute <= 0:
            raise ValueError("DAYTRADE_TICK_INGEST_RATE_LIMIT_PER_MINUTE must be positive")
        if not 0 < self.live_canary_max_lot <= 0.01:
            raise ValueError("LIVE_CANARY_MAX_LOT must be between 0 and 0.01")
        if self.session_secret and len(self.session_secret) < 32:
            raise ValueError("DAYTRADE_SESSION_SECRET must contain at least 32 characters")

    @property
    def dashboard_auth_configured(self) -> bool:
        return bool(self.dashboard_password and self.session_secret)

    @property
    def supabase_results_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
