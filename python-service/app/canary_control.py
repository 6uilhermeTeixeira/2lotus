import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings


def canary_prerequisites(settings: Settings) -> dict[str, bool]:
    return {
        "supabase_store": settings.results_store == "SUPABASE",
        "supabase_credentials": settings.supabase_results_configured,
        "account_allowlisted": settings.live_canary_account_login > 0,
        "bot_allowlisted": settings.live_canary_bot_id == "core-combined-v3",
        "ingest_authentication": bool(settings.ingest_api_key),
        "dashboard_authentication": settings.dashboard_auth_configured,
        "lot_limit": 0 < settings.live_canary_max_lot <= 0.01,
        "spread_limit": settings.live_canary_max_spread_points > 0,
    }


def append_security_audit(
    path: Path,
    *,
    actor: str,
    setting: str,
    previous: bool,
    current: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "setting": setting,
            "previous": previous,
            "current": current,
        },
        separators=(",", ":"),
    ) + "\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, record.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
