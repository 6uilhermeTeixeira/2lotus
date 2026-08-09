from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class RateLimit:
    attempts: int
    window_seconds: int


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: RateLimit) -> None:
        now = monotonic()
        cutoff = now - limit.window_seconds
        with self._lock:
            if len(self._attempts) >= 10_000 and key not in self._attempts:
                expired_keys = [name for name, values in self._attempts.items() if not values or values[-1] < cutoff]
                for name in expired_keys:
                    self._attempts.pop(name, None)
                if len(self._attempts) >= 10_000:
                    raise HTTPException(status_code=429, detail="rate limit capacity reached", headers={"Retry-After": str(limit.window_seconds)})
            attempts = self._attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= limit.attempts:
                raise HTTPException(status_code=429, detail="too many requests", headers={"Retry-After": str(limit.window_seconds)})
            attempts.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


rate_limiter = InMemoryRateLimiter()


def client_key(request: Request, scope: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{scope}:{host}"


def require_same_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.url.path in {"/login", "/decision", "/api/candles", "/api/trades"}:
        return
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
        raise HTTPException(status_code=403, detail="cross-site request blocked")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(status_code=403, detail="origin mismatch")
