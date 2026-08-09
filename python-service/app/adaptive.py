from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .schemas import TradeResult


ADAPTIVE_COMPONENTS = ("bot-3", "bot-6", "bot-7")
ADAPTIVE_WINDOW_DAYS = 120
ADAPTIVE_MIN_CLOSED_TRADES = 8
ADAPTIVE_MIN_PROFIT_FACTOR = 1.10


def summarize_outcomes(outcomes: Iterable[float]) -> dict[str, float | int | bool]:
    values = [float(value) for value in outcomes]
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    expectancy = sum(values) / len(values) if values else 0.0
    eligible = (
        len(values) >= ADAPTIVE_MIN_CLOSED_TRADES
        and expectancy > 0
        and profit_factor >= ADAPTIVE_MIN_PROFIT_FACTOR
    )
    return {
        "closed_trades": len(values),
        "expectancy_r": round(expectancy, 6),
        "profit_factor": round(profit_factor, 6),
        "eligible": eligible,
    }


def select_adaptive_component(health: dict[str, dict[str, object]]) -> str | None:
    eligible = [
        (float(snapshot.get("expectancy_r", 0)), component)
        for component, snapshot in health.items()
        if component in ADAPTIVE_COMPONENTS and bool(snapshot.get("eligible"))
    ]
    return max(eligible, default=(0.0, ""))[1] or None


def utc_day_start(timestamp: datetime) -> datetime:
    day = timestamp.astimezone(timezone.utc).date()
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def build_adaptive_schedule(
    component_trades: dict[str, list[TradeResult]],
    start: date,
    end: date,
) -> dict[date, dict[str, object]]:
    """Build a causal daily selector using only trades closed before each day."""
    grouped: dict[str, list[TradeResult]] = {
        component: sorted(trades, key=lambda trade: trade.closed_at_utc or trade.opened_at_utc)
        for component, trades in component_trades.items()
    }
    schedule: dict[date, dict[str, object]] = {}
    cursor = start
    while cursor <= end:
        day_start = datetime.combine(cursor, time.min, tzinfo=timezone.utc)
        window_start = day_start - timedelta(days=ADAPTIVE_WINDOW_DAYS)
        health: dict[str, dict[str, object]] = {}
        for component in ADAPTIVE_COMPONENTS:
            outcomes = [
                float(trade.rr_realized or 0)
                for trade in grouped.get(component, [])
                if trade.closed_at_utc is not None and window_start <= trade.closed_at_utc < day_start
            ]
            health[component] = summarize_outcomes(outcomes)
        schedule[cursor] = {
            "active_component": select_adaptive_component(health),
            "health": health,
            "window_start_utc": window_start.isoformat(),
            "as_of_utc": day_start.isoformat(),
        }
        cursor += timedelta(days=1)
    return schedule
