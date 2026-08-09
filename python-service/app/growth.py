from datetime import date

from .schemas import GrowthMilestone, GrowthPoint, GrowthSimulationSeries, HistoricalReplayResponse, RiskTier


def simulate_growth(
    replay: HistoricalReplayResponse,
    initial_balance: float,
    risk_per_trade_pct: float,
    *,
    label: str,
) -> GrowthSimulationSeries:
    return simulate_tiered_growth(
        replay,
        initial_balance,
        [RiskTier(balance_threshold=0, risk_per_trade_pct=risk_per_trade_pct)],
        label=label,
    )


def simulate_tiered_growth(
    replay: HistoricalReplayResponse,
    initial_balance: float,
    risk_tiers: list[RiskTier],
    *,
    label: str,
) -> GrowthSimulationSeries:
    """Reprice realized R with sticky risk reductions after each threshold."""
    tiers = sorted(risk_tiers, key=lambda tier: tier.balance_threshold)
    if not tiers or tiers[0].balance_threshold > initial_balance:
        raise ValueError("the first risk tier must start at or below the initial balance")
    balance = initial_balance
    minimum_balance = initial_balance
    peak = initial_balance
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily_balance: dict[date, float] = {}
    trades_simulated = 0
    active_tier_index = max(index for index, tier in enumerate(tiers) if tier.balance_threshold <= initial_balance)
    reached_at: dict[int, object] = {index: replay.period_start_utc for index in range(active_tier_index + 1)}

    ordered_trades = sorted(
        (trade for trade in replay.trades if trade.closed_at_utc is not None and trade.rr_realized is not None),
        key=lambda trade: trade.closed_at_utc,
    )
    for trade in ordered_trades:
        if balance <= 0:
            break
        risk_amount = balance * tiers[active_tier_index].risk_per_trade_pct / 100
        balance += risk_amount * float(trade.rr_realized)
        while active_tier_index + 1 < len(tiers) and balance >= tiers[active_tier_index + 1].balance_threshold:
            active_tier_index += 1
            reached_at[active_tier_index] = trade.closed_at_utc
        minimum_balance = min(minimum_balance, balance)
        trades_simulated += 1
        peak = max(peak, balance)
        drawdown = peak - balance
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak * 100)
        daily_balance[trade.closed_at_utc.date()] = balance

    points = [GrowthPoint(day=replay.period_start_utc.date(), balance=round(initial_balance, 2))]
    points.extend(GrowthPoint(day=day, balance=round(value, 2)) for day, value in sorted(daily_balance.items()))
    net_pnl = balance - initial_balance
    return GrowthSimulationSeries(
        label=label,
        initial_balance=round(initial_balance, 2),
        risk_per_trade_pct=tiers[0].risk_per_trade_pct,
        final_balance=round(balance, 2),
        net_pnl=round(net_pnl, 2),
        return_pct=round(net_pnl / initial_balance * 100, 2),
        max_drawdown=round(max_drawdown, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        trades_simulated=trades_simulated,
        minimum_balance=round(minimum_balance, 2),
        milestones=[GrowthMilestone(balance_threshold=tier.balance_threshold, risk_per_trade_pct=tier.risk_per_trade_pct, reached_at_utc=reached_at.get(index)) for index, tier in enumerate(tiers)],
        points=points,
    )
