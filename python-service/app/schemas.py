from datetime import date, datetime
from enum import Enum
from math import isfinite

from pydantic import BaseModel, Field, model_validator


class DecisionAction(str, Enum):
    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


class PositionSnapshot(BaseModel):
    symbol: str
    side: str
    volume: float
    entry_price: float
    profit: float = 0.0


class AccountSnapshot(BaseModel):
    login: int | None = Field(default=None, gt=0)
    balance: float
    equity: float
    margin_free: float
    broker: str = Field(default="", max_length=120)
    server: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", min_length=1, max_length=12)
    floating_profit: float | None = None
    realized_profit_today: float | None = None
    profit_day: date | None = None
    leverage: int | None = None
    trade_mode: str = Field(default="unknown", pattern="^(demo|contest|real|unknown)$")


class CandleSnapshot(BaseModel):
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @model_validator(mode="after")
    def validate_ohlc(self) -> "CandleSnapshot":
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        return self


class TickSnapshot(BaseModel):
    time_msc: int = Field(..., gt=0)
    sequence: int = Field(..., ge=0)
    bid: float = Field(..., gt=0)
    ask: float = Field(..., gt=0)
    last: float = Field(default=0.0, ge=0)
    volume: int = Field(default=0, ge=0)
    volume_real: float = Field(default=0.0, ge=0)
    flags: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_quote(self) -> "TickSnapshot":
        if self.ask < self.bid:
            raise ValueError("tick ask cannot be below bid")
        return self


class TickIngestRequest(BaseModel):
    collector_id: str = Field(..., min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    symbol: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    point_size: float = Field(..., gt=0)
    ticks: list[TickSnapshot] = Field(..., min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_order(self) -> "TickIngestRequest":
        keys = [(tick.time_msc, tick.sequence) for tick in self.ticks]
        if keys != sorted(keys):
            raise ValueError("ticks must be ordered by time_msc and sequence")
        if len(keys) != len(set(keys)):
            raise ValueError("tick keys must be unique inside a batch")
        return self


class OrderBookLevel(BaseModel):
    side: str = Field(..., pattern="^(bid|ask)$")
    price: float
    volume: float


class FlowSnapshot(BaseModel):
    book: list[OrderBookLevel] = Field(default_factory=list, max_length=500)
    up_ticks: int = 0
    down_ticks: int = 0
    neutral_ticks: int = 0
    up_volume: float = 0.0
    down_volume: float = 0.0


class LiveSecuritySettingsUpdate(BaseModel):
    enable_live_decisions: bool
    live_canary_armed: bool
    password: str | None = Field(default=None, max_length=512)


class DecisionRequest(BaseModel):
    bot_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")
    request_id: str = Field(..., min_length=3)
    timestamp_utc: datetime
    symbol: str
    timeframe: str
    bid: float
    ask: float
    spread_points: float
    point_size: float
    value_per_point_per_lot: float
    candles: list[CandleSnapshot] = Field(default_factory=list, max_length=5000)
    positions: list[PositionSnapshot] = Field(default_factory=list, max_length=100)
    account: AccountSnapshot
    indicators: dict[str, float] = Field(default_factory=dict)
    flow: FlowSnapshot | None = None

    @model_validator(mode="after")
    def validate_market_snapshot(self) -> "DecisionRequest":
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("bid and ask are inconsistent")
        if self.spread_points < 0 or self.point_size <= 0 or self.value_per_point_per_lot <= 0:
            raise ValueError("market sizing fields must be positive")
        if any(current.timestamp_utc < previous.timestamp_utc for previous, current in zip(self.candles, self.candles[1:])):
            raise ValueError("candles must be ordered by timestamp")
        if any(not isfinite(value) for value in self.indicators.values()):
            raise ValueError("indicator values must be finite")
        return self


class RiskCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class DecisionResponse(BaseModel):
    action: DecisionAction
    reason: str
    size: float = 0.0
    risk_amount: float = 0.0
    entry_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    stop_loss_points: float | None = None
    take_profit_points: float | None = None
    partial_take_profit_price: float | None = None
    final_take_profit_price: float | None = None
    partial_close_pct: float = 0.0
    move_stop_to_breakeven: bool = False
    risk_checks: list[RiskCheck] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    parallel_candidates: list["DecisionResponse"] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp_utc: datetime


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TradeResult(BaseModel):
    trade_id: str = Field(..., min_length=3)
    symbol: str
    timeframe: str
    setup: str
    side: TradeSide
    opened_at_utc: datetime
    closed_at_utc: datetime | None = None
    volume_lots: float
    entry_price: float
    exit_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    risk_amount: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    rr_realized: float | None = None
    note: str = ""


class MarketChartResponse(BaseModel):
    bot_id: str
    symbol: str
    timeframe: str
    candles: list[CandleSnapshot] = Field(default_factory=list)
    trades: list[TradeResult] = Field(default_factory=list)


class ResultsSummary(BaseModel):
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    open_positions: int = 0
    win_rate_pct: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0


class DailyPnlPoint(BaseModel):
    day: date
    net_pnl: float


class ResultsResponse(BaseModel):
    source_file: str
    latest_trade_at_utc: datetime | None = None
    summary: ResultsSummary
    daily_pnl: list[DailyPnlPoint] = Field(default_factory=list)
    recent_trades: list[TradeResult] = Field(default_factory=list)


class BotCatalogItem(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9-]+$")
    name: str
    description: str
    strategy_profile: str
    strategy_description: str = ""
    symbol: str
    timeframe: str
    results_file: str = ""
    active: bool = True


class TradeIngestRequest(BaseModel):
    bot_id: str = Field(..., pattern=r"^[a-z0-9-]+$")
    trade: TradeResult


class CandleIngestRequest(BaseModel):
    bot_id: str = Field(..., pattern=r"^[a-z0-9-]+$")
    symbol: str
    timeframe: str
    candles: list[CandleSnapshot] = Field(default_factory=list, max_length=1000)


class ReplayValidationFold(BaseModel):
    period_start_utc: datetime
    period_end_utc: datetime
    trades_count: int
    expectancy_r: float
    profit_factor: float
    net_pnl: float
    positive: bool


class HistoricalReplayResponse(BaseModel):
    bot_id: str
    symbol: str
    timeframe: str
    risk_per_trade_pct: float = 0.0
    strategy_version: str = "legacy"
    settings_fingerprint: str = "legacy"
    period_start_utc: datetime
    period_end_utc: datetime
    candles_evaluated: int
    candidates_detected: int
    candidates_eligible: int = 0
    candidates_blocked: int = 0
    regime_counts: dict[str, int] = Field(default_factory=dict)
    blocked_reasons: dict[str, int] = Field(default_factory=dict)
    expectancy_r: float = 0.0
    max_drawdown_r: float = 0.0
    max_consecutive_losses: int = 0
    forced_session_closes: int = 0
    total_costs: float = 0.0
    validation_status: str = "research_only"
    validation_reason: str = ""
    validation_folds: list[ReplayValidationFold] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    summary: ResultsSummary
    daily_pnl: list[DailyPnlPoint] = Field(default_factory=list)
    candles: list[CandleSnapshot] = Field(default_factory=list)
    trades: list[TradeResult] = Field(default_factory=list)


class EquityPoint(BaseModel):
    day: date
    cumulative_pnl: float


class EquitySeries(BaseModel):
    bot_id: str
    name: str
    strategy_profile: str
    summary: ResultsSummary
    points: list[EquityPoint] = Field(default_factory=list)


class EquityComparisonResponse(BaseModel):
    symbol: str
    timeframe: str
    period_days: int
    updated_at_utc: datetime
    series: list[EquitySeries] = Field(default_factory=list)
    missing_bot_ids: list[str] = Field(default_factory=list)


class MarketCandlePoint(BaseModel):
    t: int
    o: float
    h: float
    l: float  # noqa: E741 - compact OHLC wire format used by the chart
    c: float
    volume: float


class BacktestMarketChartResponse(BaseModel):
    symbol: str
    resolution: str
    period_days: int
    dataset_version: str
    candles: list[MarketCandlePoint] = Field(default_factory=list)
    series: list[EquitySeries] = Field(default_factory=list)
    missing_bot_ids: list[str] = Field(default_factory=list)


class GrowthPoint(BaseModel):
    day: date
    balance: float


class GrowthMilestone(BaseModel):
    balance_threshold: float
    risk_per_trade_pct: float
    reached_at_utc: datetime | None = None


class GrowthSimulationSeries(BaseModel):
    label: str
    initial_balance: float
    risk_per_trade_pct: float
    final_balance: float
    net_pnl: float
    return_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    trades_simulated: int
    minimum_balance: float = 0.0
    milestones: list[GrowthMilestone] = Field(default_factory=list)
    points: list[GrowthPoint] = Field(default_factory=list)


class GrowthSimulationResponse(BaseModel):
    bot_id: str
    bot_name: str
    period_start_utc: datetime
    period_end_utc: datetime
    reference: GrowthSimulationSeries
    simulation: GrowthSimulationSeries


class RiskTier(BaseModel):
    balance_threshold: float = Field(..., ge=0, le=100_000_000)
    risk_per_trade_pct: float = Field(..., ge=0.01, le=10)


class GrowthSimulationRequest(BaseModel):
    initial_balance: float = Field(10000, ge=1, le=100_000_000)
    risk_tiers: list[RiskTier] = Field(default_factory=lambda: [RiskTier(balance_threshold=0, risk_per_trade_pct=0.25)], min_length=1, max_length=12)


class RuntimeStatusItem(BaseModel):
    bot_id: str
    name: str
    last_request_at_utc: datetime | None = None
    last_attempt_at_utc: datetime | None = None
    last_success_at_utc: datetime | None = None
    last_candle_at_utc: datetime | None = None
    last_action: str | None = None
    last_reason: str | None = None
    last_latency_ms: float | None = None
    consecutive_errors: int = 0
    last_error: str | None = None
    last_error_code: str | None = None
    market_lag_seconds: float | None = None
    snapshot_updated_at_utc: datetime | None = None
    market_data_through_utc: datetime | None = None
    status: str = "no_data"


class OperationsResponse(BaseModel):
    updated_at_utc: datetime
    bots: list[RuntimeStatusItem] = Field(default_factory=list)


class AccountDashboardResponse(BaseModel):
    available: bool
    status: str = Field(pattern="^(online|delayed|offline|waiting)$")
    initial_balance: float
    expected_account_id: str = ""
    expected_server: str = ""
    identity_matches: bool | None = None
    account: AccountSnapshot | None = None
    observed_at_utc: datetime | None = None
    age_seconds: float | None = None
