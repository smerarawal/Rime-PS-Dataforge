"""Frontend event mapping. The orchestrator never assumes UI details."""

from __future__ import annotations

from typing import Any

from backend.app.core.events import AppEvent, EventType


class FrontendEventAdapter:
    """Translate internal events into the documented WebSocket protocol."""

    def to_message(self, event: AppEvent) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": event.event_type.value,
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "conversation_id": event.conversation_id,
            "request_id": event.request_id,
            "generation_id": event.generation_id,
        }
        message.update(event.payload)
        return message

    def parse_client_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = payload.get("type")
        if message_type == "user_message":
            return {"action": "user_message", "text": str(payload.get("text") or "")}
        if message_type == "interrupt":
            return {"action": "interrupt", "reason": str(payload.get("reason") or "user")}
        return {"action": "unknown", "raw": payload}


FRONTEND_EVENT_TYPES = [item.value for item in EventType]
