from copy import copy

from app.config import get_settings
from app.live_guard import apply_live_canary_guard
from app.schemas import AccountSnapshot, DecisionAction, DecisionRequest, DecisionResponse


def request(login: int = 123) -> DecisionRequest:
    return DecisionRequest(
        bot_id="core-portfolio",
        request_id="guard-test",
        timestamp_utc="2026-08-09T12:00:00Z",
        symbol="XAUUSD",
        timeframe="M5",
        bid=2400,
        ask=2400.1,
        spread_points=10,
        point_size=0.01,
        value_per_point_per_lot=1,
        account=AccountSnapshot(login=login, balance=10000, equity=10000, margin_free=9000),
    )


def candidate() -> DecisionResponse:
    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="shadow",
        stop_loss_price=2399,
        take_profit_price=2402,
        metadata={"candidate": True, "suggested_action": "buy", "suggested_size": 0.5},
    )


def configured_settings():
    settings = copy(get_settings())
    settings.enable_live_decisions = True
    settings.live_canary_armed = True
    settings.live_canary_account_login = 123
    settings.live_canary_bot_id = "core-portfolio"
    settings.live_canary_max_lot = 0.01
    settings.live_canary_max_spread_points = 15
    return settings


def test_guard_promotes_and_caps_only_when_all_gates_pass():
    response = apply_live_canary_guard(request(), candidate(), configured_settings(), persistent_risk_state_available=True)
    assert response.action == DecisionAction.BUY
    assert response.size == 0.01
    assert response.reason == "live_canary_guard_passed"


def test_guard_fails_closed_for_wrong_account():
    response = apply_live_canary_guard(request(login=999), candidate(), configured_settings(), persistent_risk_state_available=True)
    assert response.action == DecisionAction.HOLD
    assert response.size == 0
    assert response.metadata["live_canary_eligible"] is False


def test_guard_fails_closed_without_persistent_state():
    response = apply_live_canary_guard(request(), candidate(), configured_settings(), persistent_risk_state_available=False)
    assert response.action == DecisionAction.HOLD
