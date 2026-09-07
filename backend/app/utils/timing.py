"""Clock helpers. Latency is computed only from real timestamps."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def monotonic_now() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: datetime, ended_at: datetime | None = None) -> float:
    end = ended_at or utcnow()
    return (end - started_at).total_seconds() * 1000.0


class LatencyTracker:
    """Collects timestamps from real events. Never invents measurements."""

    def __init__(self) -> None:
        self._marks: dict[str, datetime] = {}
        self._latencies_ms: dict[str, float] = {}

    def mark(self, name: str, at: datetime | None = None) -> datetime:
        stamped = at or utcnow()
        self._marks[name] = stamped
        return stamped

    def measure(self, name: str, start: str, end: str) -> float | None:
        if start not in self._marks or end not in self._marks:
            return None
        value = elapsed_ms(self._marks[start], self._marks[end])
        self._latencies_ms[name] = value
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "marks": {key: value.isoformat() for key, value in self._marks.items()},
            "latencies_ms": dict(self._latencies_ms),
        }
