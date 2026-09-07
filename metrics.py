"""
metrics.py

Captures per-turn timing and audio-cancellation events, independent of
LiveKit's own internal debug logging. This is the data RIME_EVIDENCE.md's
results section is built from — timestamps here answer exactly the
acceptance test's question: how fast did audio actually stop, and was a
stale result ever spoken.

IMPORTANT: events recorded from acceptance_test.py (a pure-Python, no-audio
sandbox) can only ever produce logic/cancellation latency numbers — never
real audio-stop latency. Events recorded from a live agent.py session CAN
include real audio-stop timestamps, but only if agent.py's own handlers
record `audio_stopped` at the moment playback actually halts, not at the
moment cancellation is merely requested. Check agent.py's instrumentation
before treating any `audio_stopped` event as proof of real playback
stopping. `audio_stop_latency_ms()` below cannot make that distinction for
you — it only subtracts two timestamps.

Usage: call MetricsLog.record(event, **fields) at the relevant points in
agent.py, then call MetricsLog.export_json(path) after a session/test run.

For realtime consumers (e.g. a WebSocket bridge): call
MetricsLog.subscribe(callback) to be notified synchronously as each event
is recorded. NOTE: record() runs inside the agent's async event loop, so a
subscriber callback is called synchronously and must not block — if you
need to hand the event off to a WebSocket server, use something
non-blocking like `loop.call_soon_threadsafe(...)` or push into an
`asyncio.Queue` and drain it separately, rather than doing slow/blocking
work directly inside the callback.
"""

import json
import time


class MetricsLog:
    _events = []
    _subscribers = []

    @classmethod
    def record(cls, event: str, **fields):
        entry = {"event": event, "timestamp": time.time(), **fields}
        cls._events.append(entry)
        print(f"[METRIC] {event} {fields}")
        for callback in cls._subscribers:
            try:
                callback(entry)
            except Exception as e:
                # A broken subscriber (e.g. a disconnected websocket client)
                # must never break the actual agent pipeline — log and move on.
                print(f"[METRIC] subscriber callback failed: {e}")

    @classmethod
    def subscribe(cls, callback):
        """Register a callback(entry: dict) to be called synchronously every
        time a new event is recorded. Used by the WebSocket bridge to push
        events to connected frontend clients in realtime. Returns an
        unsubscribe function."""
        cls._subscribers.append(callback)

        def unsubscribe():
            if callback in cls._subscribers:
                cls._subscribers.remove(callback)

        return unsubscribe

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
        """Gap between the first occurrence of start_event and the first
        occurrence of end_event for a turn, in ms. None if either is missing.

        Generic on purpose: use it for the sandbox's
        interrupt_detected -> tool_cancellation_resolved interval AND for a
        live session's interrupt_detected -> audio_stopped interval — but
        label results with which one was used. They are not the same claim.
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

    @classmethod
    def audio_stop_latency_ms(cls, turn_id: int):
        """Given a turn id, finds the gap between interrupt_detected and
        audio_stopped for that turn, in milliseconds. Returns None if
        either event wasn't recorded."""
        interrupt_ts = None
        stop_ts = None
        for e in cls._events:
            if e.get("turn_id") == turn_id:
                if e["event"] == "interrupt_detected" and interrupt_ts is None:
                    interrupt_ts = e["timestamp"]
                if e["event"] == "audio_stopped" and stop_ts is None:
                    stop_ts = e["timestamp"]
        if interrupt_ts is not None and stop_ts is not None:
            return round((stop_ts - interrupt_ts) * 1000, 1)
        return None
