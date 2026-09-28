import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from app.schemas import DecisionAction, DecisionResponse
from app.supabase_store import SupabaseRequestError


def payload(request_id: str) -> dict[str, object]:
    return {
        "bot_id": "core-combined-v3",
        "request_id": request_id,
        "timestamp_utc": "2026-09-11T12:00:00Z",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "bid": 2400,
        "ask": 2400.1,
        "spread_points": 10,
        "point_size": 0.01,
        "value_per_point_per_lot": 1,
        "account": {"login": 123, "balance": 10000, "equity": 10000, "margin_free": 9000},
    }


def candidate() -> DecisionResponse:
    return DecisionResponse(
        action=DecisionAction.HOLD,
        reason="shadow_candidate",
        stop_loss_price=2399,
        take_profit_price=2402,
        metadata={"candidate": True, "suggested_action": "buy", "suggested_size": 0.01},
    )


class DecisionIntegrationTests(unittest.TestCase):
    def live_settings(self, stack: ExitStack, *, results_store: str) -> None:
        values = {
            "results_store": results_store,
            "ingest_api_key": "integration-ingest-key",
            "enable_live_decisions": True,
            "live_canary_armed": True,
            "live_canary_account_login": 123,
            "live_canary_bot_id": "core-combined-v3",
            "live_canary_max_lot": 0.01,
            "live_canary_max_spread_points": 15,
        }
        for name, value in values.items():
            stack.enter_context(patch.object(main_module.settings, name, value))

    def test_decision_fails_closed_without_persistent_risk_state(self) -> None:
        with ExitStack() as stack:
            self.live_settings(stack, results_store="JSON")
            stack.enter_context(patch("app.main.evaluate_decision", return_value=candidate()))

            response = TestClient(app).post(
                "/decision",
                headers={"x-2lotus-ingest-key": "integration-ingest-key"},
                json=payload("integration-no-store"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "hold")
        checks = {item["name"]: item["passed"] for item in response.json()["risk_checks"]}
        self.assertFalse(checks["live_persistent_risk_state"])

    def test_decision_promotes_only_with_persistent_store_and_all_gates(self) -> None:
        store = MagicMock()
        store.load_decision_response.return_value = None
        store.load_candles.return_value = []
        store.load_portfolio_risk_state.return_value = {}
        store.record_strategy_candidate.return_value = None
        store.closed_paper_trade_count.return_value = 999

        with ExitStack() as stack:
            self.live_settings(stack, results_store="SUPABASE")
            stack.enter_context(patch("app.main.SupabaseStore", return_value=store))
            stack.enter_context(
                patch(
                    "app.main._find_bot",
                    return_value=SimpleNamespace(
                        id="core-combined-v3",
                        strategy_profile="portfolio_v3_adaptive_union_shadow",
                        symbol="XAUUSD",
                        timeframe="M5",
                    ),
                )
            )
            stack.enter_context(patch("app.main.evaluate_decision", return_value=candidate()))

            response = TestClient(app).post(
                "/decision",
                headers={"x-2lotus-ingest-key": "integration-ingest-key"},
                json=payload("integration-all-gates"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "buy")
        self.assertEqual(response.json()["reason"], "live_canary_guard_passed")
        self.assertEqual(response.json()["size"], 0.01)
        store.save_decision_response.assert_called_once()
        store.record_runtime_status.assert_called_once()

    def test_decision_returns_503_when_persistent_store_is_unavailable(self) -> None:
        store = MagicMock()
        store.load_decision_response.side_effect = SupabaseRequestError(
            "network unavailable", code="network_error", retryable=True
        )
        bot = SimpleNamespace(
            id="core-combined-v3",
            strategy_profile="portfolio_v3_adaptive_union_shadow",
            symbol="XAUUSD",
            timeframe="M5",
        )

        with ExitStack() as stack:
            self.live_settings(stack, results_store="SUPABASE")
            stack.enter_context(patch("app.main.SupabaseStore", return_value=store))
            stack.enter_context(patch("app.main._find_bot", return_value=bot))

            response = TestClient(app).post(
                "/decision",
                headers={"x-2lotus-ingest-key": "integration-ingest-key"},
                json=payload("integration-store-failure"),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "decision dependency unavailable: network_error")
        store.record_runtime_failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
