from decimal import Decimal, ROUND_DOWN

from .config import Settings
from .schemas import DecisionAction, DecisionRequest, DecisionResponse, RiskCheck


def apply_live_canary_guard(
    request: DecisionRequest,
    response: DecisionResponse,
    settings: Settings,
    *,
    persistent_risk_state_available: bool,
) -> DecisionResponse:
    """Promote one combined shadow candidate only after every live gate passes."""
    suggested_action = str(response.metadata.get("suggested_action", ""))
    suggested_size = float(response.metadata.get("suggested_size", 0.0))
    capped_size = min(suggested_size, settings.live_canary_max_lot)
    executable_size = float(Decimal(str(capped_size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    checks = {
        "canary_armed": settings.live_canary_armed,
        "live_decisions_enabled": settings.enable_live_decisions,
        "bot_allowlisted": request.bot_id == settings.live_canary_bot_id,
        "account_allowlisted": bool(
            settings.live_canary_account_login
            and request.account.login == settings.live_canary_account_login
        ),
        "persistent_risk_state": persistent_risk_state_available,
        "candidate_present": bool(response.metadata.get("candidate")),
        "valid_side": suggested_action in {DecisionAction.BUY.value, DecisionAction.SELL.value},
        "spread_limit": request.spread_points <= settings.live_canary_max_spread_points,
        "position_limit": not any(
            position.symbol.upper() == request.symbol.upper() for position in request.positions
        ),
        "protective_orders": bool(response.stop_loss_price and response.take_profit_price),
        "positive_size": executable_size > 0,
    }
    response.risk_checks.extend(
        RiskCheck(name=f"live_{name}", passed=passed, detail=str(passed).lower())
        for name, passed in checks.items()
    )
    response.metadata["live_canary_eligible"] = all(checks.values())
    if not all(checks.values()):
        response.action = DecisionAction.HOLD
        response.size = 0.0
        return response

    response.action = DecisionAction(suggested_action)
    response.size = executable_size
    response.reason = "live_canary_guard_passed"
    response.metadata["execution_mode"] = "live_canary"
    return response
