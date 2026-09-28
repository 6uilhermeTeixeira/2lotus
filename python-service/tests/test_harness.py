import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.harness import run_canary_disarmed_fail_closed_test, run_live_decisions_fail_closed_test
from app.main import app


class HarnessTests(unittest.TestCase):
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            enable_live_decisions=True,
            live_canary_armed=False,
            live_canary_account_login=123,
            live_canary_bot_id="another-bot",
            live_canary_max_lot=0.005,
            live_canary_max_spread_points=2,
        )

    def test_live_decisions_disabled_fails_closed_without_mutating_runtime(self) -> None:
        configured = self.settings()
        original = vars(configured).copy()

        result = run_live_decisions_fail_closed_test(configured)

        self.assertTrue(result["passed"])
        self.assertTrue(result["safe"])
        self.assertEqual(result["observed"]["action"], "hold")
        self.assertEqual(result["observed"]["size"], 0)
        self.assertFalse(result["observed"]["live_canary_eligible"])
        self.assertEqual(result["observed"]["failed_checks"], ["live_live_decisions_enabled"])
        self.assertEqual(vars(configured), original)

    def test_harness_endpoint_requires_dashboard_session(self) -> None:
        response = TestClient(app).post("/api/harness/tests/live-decisions-fail-closed")
        self.assertIn(response.status_code, {401, 503})

    def test_harness_endpoint_returns_result_for_authenticated_operator(self) -> None:
        with patch("app.main._require_api_session"):
            response = TestClient(app).post("/api/harness/tests/live-decisions-fail-closed")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["passed"])

    def test_canary_disarmed_fails_closed_without_mutating_runtime(self) -> None:
        configured = self.settings()
        original = vars(configured).copy()

        result = run_canary_disarmed_fail_closed_test(configured)

        self.assertTrue(result["passed"])
        self.assertTrue(result["safe"])
        self.assertEqual(result["observed"]["action"], "hold")
        self.assertEqual(result["observed"]["size"], 0)
        self.assertFalse(result["observed"]["live_canary_eligible"])
        self.assertEqual(result["observed"]["failed_checks"], ["live_canary_armed"])
        self.assertEqual(vars(configured), original)

    def test_canary_disarmed_endpoint_returns_result_for_authenticated_operator(self) -> None:
        with patch("app.main._require_api_session"):
            response = TestClient(app).post("/api/harness/tests/canary-disarmed-fail-closed")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["passed"])


if __name__ == "__main__":
    unittest.main()
