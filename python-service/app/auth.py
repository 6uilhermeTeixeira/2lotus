import base64
import hashlib
import hmac
import time
from urllib.parse import parse_qs

from fastapi import Request

from .config import Settings

SESSION_COOKIE_NAME = "2lotus_dashboard_session"


def _signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_value(settings: Settings) -> str:
    issued_at = str(int(time.time()))
    signature = _signature(issued_at, settings.session_secret or "")
    return base64.urlsafe_b64encode(f"{issued_at}.{signature}".encode()).decode()


def has_valid_session(request: Request, settings: Settings) -> bool:
    if not settings.dashboard_auth_configured:
        return False
    raw_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_value:
        return False
    try:
        issued_at, signature = base64.urlsafe_b64decode(raw_value.encode()).decode().split(".", maxsplit=1)
        expected_signature = _signature(issued_at, settings.session_secret or "")
        age = int(time.time()) - int(issued_at)
    except (ValueError, UnicodeDecodeError):
        return False
    return 0 <= age <= settings.session_max_age_seconds and hmac.compare_digest(signature, expected_signature)


async def read_login_password(request: Request) -> str:
    body = (await request.body()).decode("utf-8")
    return parse_qs(body).get("password", [""])[0]


def password_matches(password: str, settings: Settings) -> bool:
    configured = settings.dashboard_password or ""
    if configured.startswith("scrypt$"):
        try:
            _, salt_hex, digest_hex = configured.split("$", maxsplit=2)
            actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1).hex()
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual, digest_hex)
    return bool(configured) and hmac.compare_digest(password, configured)
