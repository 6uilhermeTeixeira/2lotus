import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from .schemas import BotCatalogItem, DailyPnlPoint, ResultsResponse, ResultsSummary, TradeResult


def _empty_response(source_file: Path) -> ResultsResponse:
    return ResultsResponse(
        source_file=str(source_file),
        latest_trade_at_utc=None,
        summary=ResultsSummary(),
        daily_pnl=[],
        recent_trades=[],
    )


def build_results_snapshot(raw_payload: list[object], source_name: str) -> ResultsResponse:
    if not isinstance(raw_payload, list):
        raise ValueError("trade results store must be a JSON array")

    trades = [TradeResult.model_validate(item) for item in raw_payload]
    closed_trades = [trade for trade in trades if trade.closed_at_utc is not None]

    wins = sum(1 for trade in closed_trades if trade.net_pnl > 0)
    losses = sum(1 for trade in closed_trades if trade.net_pnl < 0)
    breakevens = sum(1 for trade in closed_trades if trade.net_pnl == 0)

    gross_profit = sum(trade.net_pnl for trade in closed_trades if trade.net_pnl > 0)
    gross_loss = abs(sum(trade.net_pnl for trade in closed_trades if trade.net_pnl < 0))
    net_pnl = gross_profit - gross_loss

    avg_win = gross_profit / wins if wins else 0.0
    avg_loss = -(gross_loss / losses) if losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else 0.0)
    win_rate = (wins / len(closed_trades) * 100.0) if closed_trades else 0.0

    rr_values = [trade.rr_realized for trade in closed_trades if trade.rr_realized is not None]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    daily_totals: dict[date, float] = defaultdict(float)
    for trade in closed_trades:
        assert trade.closed_at_utc is not None
        daily_totals[trade.closed_at_utc.date()] += trade.net_pnl

    daily_points = [
        DailyPnlPoint(day=day_key, net_pnl=round(total, 2))
        for day_key, total in sorted(daily_totals.items())
    ]

    recent_trades = sorted(
        trades,
        key=lambda trade: trade.closed_at_utc or trade.opened_at_utc,
        reverse=True,
    )[:20]

    latest_trade_at = max(
        (trade.closed_at_utc or trade.opened_at_utc for trade in trades),
        default=None,
    )

    return ResultsResponse(
        source_file=source_name,
        latest_trade_at_utc=latest_trade_at,
        summary=ResultsSummary(
            trades_count=len(closed_trades),
            wins=wins,
            losses=losses,
            breakevens=breakevens,
            win_rate_pct=round(win_rate, 2),
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            net_pnl=round(net_pnl, 2),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            avg_rr=round(avg_rr, 2),
            best_trade=round(max((trade.net_pnl for trade in closed_trades), default=0.0), 2),
            worst_trade=round(min((trade.net_pnl for trade in closed_trades), default=0.0), 2),
            open_positions=sum(1 for trade in trades if trade.closed_at_utc is None),
        ),
        daily_pnl=daily_points,
        recent_trades=recent_trades,
    )


def load_results_snapshot(source_file: Path) -> ResultsResponse:
    if not source_file.exists():
        return _empty_response(source_file)
    raw_payload = json.loads(source_file.read_text(encoding="utf-8"))
    return build_results_snapshot(raw_payload, str(source_file))


def load_bots_catalog(catalog_file: Path) -> list[BotCatalogItem]:
    raw_payload = json.loads(catalog_file.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, list):
        raise ValueError("bots catalog must be a JSON array")
    return [BotCatalogItem.model_validate(item) for item in raw_payload]


def get_bot(catalog_file: Path, bot_id: str) -> BotCatalogItem | None:
    return next((bot for bot in load_bots_catalog(catalog_file) if bot.id == bot_id), None)


def resolve_bot_results_file(catalog_file: Path, bot: BotCatalogItem) -> Path:
    data_root = catalog_file.parent.resolve()
    source_file = (data_root / bot.results_file).resolve()
    if not source_file.is_relative_to(data_root):
        raise ValueError("bot results file must remain inside the data directory")
    return source_file
