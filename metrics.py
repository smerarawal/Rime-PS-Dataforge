"""
metrics.py

Captures per-turn timing and cancellation events, independent of
LiveKit's own internal debug logging.

IMPORTANT: events recorded from acceptance_test.py (a pure-Python,
no-audio sandbox) can only ever produce logic/cancellation latency
numbers — never real audio-stop latency. Events recorded from a live
agent.py session CAN include real audio-stop timestamps, but only if
agent.py's own event handlers record `audio_stopped` at the moment
playback actually halts (e.g. a LiveKit track/publish event), not at the
moment cancellation is merely requested. Check agent.py's instrumentation
before treating any `audio_stopped` event as proof of real playback
stopping.

Usage: call MetricsLog.record(event, **fields) at the relevant points,
then call MetricsLog.export_json(path) after a session/test run.
"""

import json
import time


class MetricsLog:
    _events = []

    @classmethod
    def record(cls, event: str, **fields):
        entry = {"event": event, "timestamp": time.time(), **fields}
        cls._events.append(entry)
        print(f"[METRIC] {event} {fields}")

    @classmethod
    def all_events(cls):
        return list(cls._events)

    @classmethod
    def export_json(cls, path: str = "metrics_log.json"):
        with open(path, "w") as f:
            json.dump(cls._events, f, indent=2)
        return path

    @classmethod
    def reset(cls):
        cls._events = []

    @classmethod
    def interval_ms(cls, turn_id: int, start_event: str, end_event: str):
        """Given a turn id, finds the gap between the first occurrence of
        start_event and the first occurrence of end_event for that turn,
        in milliseconds. Returns None if either wasn't recorded.

        Generic replacement for the old audio_stop_latency_ms method,
        which hardcoded 'interrupt_detected' -> 'audio_stopped' and
        implicitly assumed 'audio_stopped' meant real audio had stopped.
        Use this for BOTH the sandbox's interrupt_detected ->
        tool_cancellation_resolved interval AND, separately, a live
        session's interrupt_detected -> audio_stopped interval — but
        label results with which one you used.
        """
        start_ts = None
        end_ts = None
        for e in cls._events:
            if e.get("turn_id") == turn_id:
                if e["event"] == start_event and start_ts is None:
                    start_ts = e["timestamp"]
                if e["event"] == end_event and end_ts is None:
                    end_ts = e["timestamp"]
        if start_ts is not None and end_ts is not None:
            return round((end_ts - start_ts) * 1000, 1)
        return None
