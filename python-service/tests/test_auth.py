import asyncio
import base64
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main as main_module
from app.auth import (
    SESSION_COOKIE_NAME,
    create_session_value,
    has_valid_session,
    password_matches,
    read_login_password,
)
from app.main import app


def settings(**overrides: object) -> SimpleNamespace:
    values = {
        "dashboard_auth_configured": True,
        "dashboard_password": "correct-password",
        "session_secret": "s" * 32,
        "session_max_age_seconds": 3600,
        "login_rate_limit_attempts": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def request_with_cookie(value: str | None) -> Request:
    headers = [] if value is None else [(b"cookie", f"{SESSION_COOKIE_NAME}={value}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


class AuthenticationTests(unittest.TestCase):
    def test_created_session_is_valid(self) -> None:
        configured = settings()
        with patch("app.auth.time.time", return_value=1_000):
            value = create_session_value(configured)
        with patch("app.auth.time.time", return_value=1_100):
            self.assertTrue(has_valid_session(request_with_cookie(value), configured))

    def test_session_requires_configured_auth_and_cookie(self) -> None:
        self.assertFalse(has_valid_session(request_with_cookie(None), settings()))
        self.assertFalse(
            has_valid_session(request_with_cookie("irrelevant"), settings(dashboard_auth_configured=False))
        )

    def test_expired_and_future_sessions_are_rejected(self) -> None:
        configured = settings(session_max_age_seconds=60)
        with patch("app.auth.time.time", return_value=1_000):
            value = create_session_value(configured)
        with patch("app.auth.time.time", return_value=1_061):
            self.assertFalse(has_valid_session(request_with_cookie(value), configured))
        with patch("app.auth.time.time", return_value=999):
            self.assertFalse(has_valid_session(request_with_cookie(value), configured))

    def test_tampered_and_malformed_sessions_are_rejected(self) -> None:
        configured = settings()
        tampered = base64.urlsafe_b64encode(b"1000.invalid-signature").decode()
        malformed_values = (tampered, "not-base64!", base64.urlsafe_b64encode(b"invalid").decode())
        for value in malformed_values:
            with self.subTest(value=value):
                self.assertFalse(has_valid_session(request_with_cookie(value), configured))

    def test_login_password_is_read_from_urlencoded_body(self) -> None:
        body = b"password=correct+horse&ignored=value"

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/login", "headers": []}, receive)
        self.assertEqual(asyncio.run(read_login_password(request)), "correct horse")

    def test_password_matching_supports_plain_and_scrypt_values(self) -> None:
        self.assertTrue(password_matches("correct-password", settings()))
        self.assertFalse(password_matches("wrong", settings()))
        self.assertFalse(password_matches("", settings(dashboard_password="")))

        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(b"correct horse", salt=salt, n=2**14, r=8, p=1).hex()
        encrypted = settings(dashboard_password=f"scrypt${salt.hex()}${digest}")
        self.assertTrue(password_matches("correct horse", encrypted))
        self.assertFalse(password_matches("wrong", encrypted))

    def test_invalid_scrypt_configuration_is_rejected(self) -> None:
        for configured in ("scrypt$missing", "scrypt$not-hex$digest"):
            with self.subTest(configured=configured):
                self.assertFalse(password_matches("password", settings(dashboard_password=configured)))

    def test_login_and_logout_cookie_contract(self) -> None:
        configured = settings()
        with patch.object(main_module, "settings", configured):
            client = TestClient(app, follow_redirects=False)
            failed = client.post("/login", data={"password": "wrong"})
            successful = client.post("/login", data={"password": "correct-password"})
            logged_out = client.post("/logout")

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(successful.status_code, 303)
        self.assertEqual(successful.headers["location"], "/overview")
        cookie = successful.headers["set-cookie"]
        self.assertIn(f"{SESSION_COOKIE_NAME}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(logged_out.status_code, 303)
        self.assertIn("Max-Age=0", logged_out.headers["set-cookie"])

    def test_login_reports_missing_configuration(self) -> None:
        configured = settings(dashboard_auth_configured=False)
        with patch.object(main_module, "settings", configured):
            response = TestClient(app).post("/login", data={"password": "anything"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
