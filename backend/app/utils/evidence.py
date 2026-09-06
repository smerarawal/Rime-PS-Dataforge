"""Latency/leak evidence, computed from real EventBus timestamps.

Orchestrator.latencies (LatencyTracker) already computes some of these
internally but never exposes a snapshot anywhere. Rather than touching
Atharva's Orchestrator to add an accessor, this subscribes to the same
EventBus independently and derives evidence purely from event history —
zero coupling to internals, safe to drop into any orchestrator instance.
"""

from __future__ import annotations

import statistics
from typing import Any

from backend.app.core.events import AppEvent, EventBus, EventType


class EvidenceCollector:
    """Subscribe to an EventBus and derive interrupt/latency/leak evidence."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._interruptions: dict[str, AppEvent] = {}  # request_id -> INTERRUPTION event
        self._invalidated_before: dict[str, float] = {}  # request_id -> ts.timestamp()
        self.tts_stop_latencies_ms: list[float] = []
        self.leaks: list[dict[str, Any]] = []
        self._unsubscribe = bus.subscribe(self._on_event)

    def close(self) -> None:
        self._unsubscribe()

    def _on_event(self, event: AppEvent) -> None:
        if event.event_type == EventType.INTERRUPTION and event.request_id:
            self._interruptions[event.request_id] = event

        elif event.event_type == EventType.TTS_STOP and event.request_id:
            start = self._interruptions.get(event.request_id)
            if start is not None:
                latency_ms = (event.timestamp - start.timestamp).total_seconds() * 1000.0
                self.tts_stop_latencies_ms.append(latency_ms)

        elif event.event_type == EventType.REQUEST_INVALIDATED and event.request_id:
            self._invalidated_before[event.request_id] = event.timestamp.timestamp()

        elif event.event_type in (EventType.TTS_START, EventType.ASSISTANT_RESPONSE_READY):
            # A leak: this request/generation was already invalidated
            # *before* the orchestrator tried to speak/deliver it anyway.
            # Should never happen if fencing in _speak()/_deliver_response
            # is working — this is the acceptance-test-style check.
            invalidated_at = self._invalidated_before.get(event.request_id or "")
            if invalidated_at is not None and event.timestamp.timestamp() > invalidated_at:
                self.leaks.append(
                    {
                        "event_type": event.event_type.value,
                        "request_id": event.request_id,
                        "generation_id": event.generation_id,
                    }
                )

    def summary(self) -> dict[str, Any]:
        latencies = sorted(self.tts_stop_latencies_ms)

        def pct(p: float) -> float | None:
            if not latencies:
                return None
            idx = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
            return latencies[idx]

        return {
            "trials": len(latencies),
            "tts_stop_latency_ms": {
                "p50": pct(0.50),
                "p95": pct(0.95),
                "mean": statistics.fmean(latencies) if latencies else None,
                "max": max(latencies) if latencies else None,
            },
            "leak_count": len(self.leaks),
            "leaks": self.leaks,
        }
