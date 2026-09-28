import unittest
from copy import copy

from app.config import get_settings
from app.live_guard import apply_live_canary_guard
from app.schemas import AccountSnapshot, DecisionAction, DecisionRequest, DecisionResponse, PositionSnapshot


def request(login: int = 123) -> DecisionRequest:
    return DecisionRequest(
        bot_id="core-combined-v3",
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


def candidate(*, side: str = "buy", size: float = 0.5) -> DecisionResponse:
    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="shadow",
        stop_loss_price=2399,
        take_profit_price=2402,
        metadata={"candidate": True, "suggested_action": side, "suggested_size": size},
    )


def configured_settings():
    settings = copy(get_settings())
    settings.enable_live_decisions = True
    settings.live_canary_armed = True
    settings.live_canary_account_login = 123
    settings.live_canary_bot_id = "core-combined-v3"
    settings.live_canary_max_lot = 0.01
    settings.live_canary_max_spread_points = 15
    return settings


class LiveCanaryGuardTests(unittest.TestCase):
    def test_promotes_valid_buy_and_caps_lot(self) -> None:
        response = apply_live_canary_guard(
            request(), candidate(size=0.5), configured_settings(), persistent_risk_state_available=True
        )

        self.assertEqual(response.action, DecisionAction.BUY)
        self.assertEqual(response.size, 0.01)
        self.assertEqual(response.reason, "live_canary_guard_passed")
        self.assertEqual(response.metadata["execution_mode"], "live_canary")
        self.assertTrue(response.metadata["live_canary_eligible"])
        self.assertTrue(all(check.passed for check in response.risk_checks))

    def test_promotes_valid_sell_without_increasing_requested_size(self) -> None:
        response = apply_live_canary_guard(
            request(), candidate(side="sell", size=0.01), configured_settings(), persistent_risk_state_available=True
        )

        self.assertEqual(response.action, DecisionAction.SELL)
        self.assertEqual(response.size, 0.01)

    def test_sub_minimum_lot_fails_closed_instead_of_rounding_up(self) -> None:
        response = apply_live_canary_guard(
            request(), candidate(size=0.006), configured_settings(), persistent_risk_state_available=True
        )

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertEqual(response.size, 0)

    def test_every_gate_fails_closed_independently(self) -> None:
        cases = {
            "canary_armed": lambda req, res, cfg: setattr(cfg, "live_canary_armed", False),
            "live_decisions_enabled": lambda req, res, cfg: setattr(cfg, "enable_live_decisions", False),
            "bot_allowlisted": lambda req, res, cfg: setattr(req, "bot_id", "core-zigzag-v3"),
            "account_allowlisted": lambda req, res, cfg: setattr(req.account, "login", 999),
            "candidate_present": lambda req, res, cfg: res.metadata.update(candidate=False),
            "valid_side": lambda req, res, cfg: res.metadata.update(suggested_action="close"),
            "spread_limit": lambda req, res, cfg: setattr(req, "spread_points", 16),
            "position_limit": lambda req, res, cfg: req.positions.append(
                PositionSnapshot(symbol="XAUUSD", side="buy", volume=0.01, entry_price=2400)
            ),
            "protective_orders": lambda req, res, cfg: setattr(res, "stop_loss_price", None),
            "positive_size": lambda req, res, cfg: res.metadata.update(suggested_size=0),
        }

        for failed_gate, mutate in cases.items():
            with self.subTest(failed_gate=failed_gate):
                decision_request = request()
                shadow_candidate = candidate()
                settings = configured_settings()
                mutate(decision_request, shadow_candidate, settings)
                response = apply_live_canary_guard(
                    decision_request, shadow_candidate, settings, persistent_risk_state_available=True
                )
                checks = {check.name: check.passed for check in response.risk_checks}

                self.assertEqual(response.action, DecisionAction.HOLD)
                self.assertEqual(response.size, 0)
                self.assertFalse(response.metadata["live_canary_eligible"])
                self.assertFalse(checks[f"live_{failed_gate}"])

    def test_missing_persistent_risk_state_fails_closed(self) -> None:
        response = apply_live_canary_guard(
            request(), candidate(), configured_settings(), persistent_risk_state_available=False
        )
        checks = {check.name: check.passed for check in response.risk_checks}

        self.assertEqual(response.action, DecisionAction.HOLD)
        self.assertFalse(checks["live_persistent_risk_state"])


if __name__ == "__main__":
    unittest.main()
