import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any


logger = logging.getLogger("2lotus.telemetry")
_lock = Lock()
_counters: Counter[tuple[str, str, str]] = Counter()
_durations: dict[tuple[str, str], list[float]] = defaultdict(list)


def emit_event(
    *,
    run_id: str,
    step: str,
    status: str,
    duration_ms: float = 0.0,
    level: int = logging.INFO,
    integration: str | None = None,
    error_code: str | None = None,
    message: str = "2Lotus decision step",
    **fields: Any,
) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": logging.getLevelName(level).lower(),
        "message": message,
        "harness_id": "2lotus-shadow-decision",
        "run_id": run_id,
        "step": step,
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "executor": "fastapi",
    }
    if integration:
        event["integration"] = integration
    if error_code:
        event["error_code"] = error_code
    event.update({key: value for key, value in fields.items() if value is not None})
    logger.log(level, json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str))
    with _lock:
        _counters[(step, status, error_code or "")] += 1
        samples = _durations[(step, status)]
        samples.append(duration_ms)
        if len(samples) > 1000:
            del samples[:len(samples) - 1000]


class StepTimer:
    def __init__(self, run_id: str, step: str, *, integration: str | None = None) -> None:
        self.run_id = run_id
        self.step = step
        self.integration = integration
        self.started_at = monotonic()

    def finish(self, status: str = "passed", **fields: Any) -> float:
        duration_ms = (monotonic() - self.started_at) * 1000
        emit_event(
            run_id=self.run_id,
            step=self.step,
            status=status,
            duration_ms=duration_ms,
            integration=self.integration,
            level=logging.ERROR if status == "failed" else logging.INFO,
            **fields,
        )
        return duration_ms


def metrics_snapshot() -> dict[str, object]:
    with _lock:
        counters = [
            {"step": step, "status": status, "error_code": error_code or None, "count": count}
            for (step, status, error_code), count in sorted(_counters.items())
        ]
        durations = []
        for (step, status), values in sorted(_durations.items()):
            if not values:
                continue
            ordered = sorted(values)
            durations.append({
                "step": step,
                "status": status,
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            })
        return {"harness_id": "2lotus-shadow-decision", "counters": counters, "durations": durations}
