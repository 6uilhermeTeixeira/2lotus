import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app


class OperationalEventTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "events": [{
                "schema_version": "1.0",
                "event_id": "event-0001",
                "runner_id": "2lotus-test",
                "event_type": "heartbeat",
                "emitted_at_utc": "2026-09-29T10:00:00Z",
                "strategy_id": "zigzag-scalper-v2",
                "strategy_version": "2.0.0",
                "account_environment": "demo",
                "payload": {"status": "ok"},
            }]
        }

    def test_batch_is_authenticated_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            with (
                patch.object(main_module.settings, "operational_event_token", "test-token"),
                patch.object(main_module.settings, "sqlite_path", Path(directory) / "runtime.db"),
            ):
                client = TestClient(app)
                first = client.post(
                    "/api/v1/events/batch",
                    headers={"Authorization": "Bearer test-token"},
                    json=self.payload(),
                )
                second = client.post(
                    "/api/v1/events/batch",
                    headers={"Authorization": "Bearer test-token"},
                    json=self.payload(),
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["accepted"], ["event-0001"])
        self.assertEqual(second.json()["duplicates"], ["event-0001"])

    def test_batch_rejects_invalid_token(self) -> None:
        with patch.object(main_module.settings, "operational_event_token", "test-token"):
            response = TestClient(app).post("/api/v1/events/batch", json=self.payload())
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
