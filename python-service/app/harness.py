from copy import copy
from datetime import datetime, timezone

from .config import Settings
from .live_guard import apply_live_canary_guard
from .schemas import AccountSnapshot, DecisionAction, DecisionRequest, DecisionResponse


def run_live_decisions_fail_closed_test(settings: Settings) -> dict[str, object]:
    """Exercise the production live guard without changing runtime state or writing data."""
    isolated = copy(settings)
    isolated.enable_live_decisions = False
    isolated.live_canary_armed = True
    isolated.live_canary_account_login = 999001
    isolated.live_canary_bot_id = "core-combined-v3"
    isolated.live_canary_max_lot = 0.01
    isolated.live_canary_max_spread_points = 15

    request = DecisionRequest(
        bot_id=isolated.live_canary_bot_id,
        request_id="harness-live-decisions-fail-closed",
        timestamp_utc=datetime.now(timezone.utc),
        symbol="XAUUSD",
        timeframe="M5",
        bid=2400,
        ask=2400.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        account=AccountSnapshot(
            login=isolated.live_canary_account_login,
            balance=10_000,
            equity=10_000,
            margin_free=10_000,
        ),
    )
    candidate = DecisionResponse(
        action=DecisionAction.HOLD,
        reason="harness_synthetic_candidate",
        stop_loss_price=2399,
        take_profit_price=2402,
        metadata={"candidate": True, "suggested_action": "buy", "suggested_size": 0.01},
    )
    result = apply_live_canary_guard(
        request,
        candidate,
        isolated,
        persistent_risk_state_available=True,
    )
    failed_checks = [check.name for check in result.risk_checks if not check.passed]
    passed = (
        result.action == DecisionAction.HOLD
        and result.size == 0
        and result.metadata.get("live_canary_eligible") is False
        and failed_checks == ["live_live_decisions_enabled"]
    )
    return {
        "id": "live-decisions-fail-closed",
        "name": "Fail-closed de decisões reais",
        "passed": passed,
        "safe": True,
        "description": "Confirma que DAYTRADE_ENABLE_LIVE_DECISIONS=false converte um candidato executável em hold.",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "observed": {
            "action": result.action.value,
            "size": result.size,
            "live_canary_eligible": result.metadata.get("live_canary_eligible"),
            "failed_checks": failed_checks,
        },
    }


def run_canary_disarmed_fail_closed_test(settings: Settings) -> dict[str, object]:
    """Verify the independent canary switch with isolated production-guard inputs."""
    isolated = copy(settings)
    isolated.enable_live_decisions = True
    isolated.live_canary_armed = False
    isolated.live_canary_account_login = 999001
    isolated.live_canary_bot_id = "core-combined-v3"
    isolated.live_canary_max_lot = 0.01
    isolated.live_canary_max_spread_points = 15

    request = DecisionRequest(
        bot_id=isolated.live_canary_bot_id,
        request_id="harness-canary-disarmed-fail-closed",
        timestamp_utc=datetime.now(timezone.utc),
        symbol="XAUUSD",
        timeframe="M5",
        bid=2400,
        ask=2400.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        account=AccountSnapshot(
            login=isolated.live_canary_account_login,
            balance=10_000,
            equity=10_000,
            margin_free=10_000,
        ),
    )
    candidate = DecisionResponse(
        action=DecisionAction.HOLD,
        reason="harness_synthetic_candidate",
        stop_loss_price=2399,
        take_profit_price=2402,
        metadata={"candidate": True, "suggested_action": "buy", "suggested_size": 0.01},
    )
    result = apply_live_canary_guard(
        request,
        candidate,
        isolated,
        persistent_risk_state_available=True,
    )
    failed_checks = [check.name for check in result.risk_checks if not check.passed]
    passed = (
        result.action == DecisionAction.HOLD
        and result.size == 0
        and result.metadata.get("live_canary_eligible") is False
        and failed_checks == ["live_canary_armed"]
    )
    return {
        "id": "canary-disarmed-fail-closed",
        "name": "Fail-closed do canário desarmado",
        "passed": passed,
        "safe": True,
        "description": "Confirma que LIVE_CANARY_ARMED=false converte um candidato executável em hold.",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "observed": {
            "action": result.action.value,
            "size": result.size,
            "live_canary_eligible": result.metadata.get("live_canary_eligible"),
            "failed_checks": failed_checks,
        },
    }
