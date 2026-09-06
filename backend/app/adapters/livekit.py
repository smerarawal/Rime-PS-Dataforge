"""Realtime input adapter. LiveKit stays outside the orchestrator."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from backend.app.core.events import AppEvent, EventBus, EventType
from backend.app.utils.ids import new_id
from backend.app.utils.timing import utcnow


class OrchestratorHandle(Protocol):
    async def handle_user_message(self, text: str, source: str = "user") -> None: ...
    async def handle_interrupt(self, reason: str = "user") -> None: ...

    @property
    def conversation_id(self) -> str: ...


class RealtimeInputAdapter(ABC):
    """Boundary for Nikunj. Convert transport events into orchestrator calls."""

    name: str

    @abstractmethod
    async def on_user_speech_started(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_user_speech_stopped(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_user_transcript_final(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_user_interrupted(self) -> None:
        raise NotImplementedError


class MockRealtimeInputAdapter(RealtimeInputAdapter):
    name = "mock"

    def __init__(self, orchestrator: OrchestratorHandle, event_bus: EventBus) -> None:
        self._orchestrator = orchestrator
        self._events = event_bus
        self.speech_active = False

    async def on_user_speech_started(self) -> None:
        self.speech_active = True
        await self._events.emit(
            AppEvent(
                conversation_id=self._orchestrator.conversation_id,
                event_type=EventType.USER_TURN,
                payload={"phase": "speech_started", "source": "realtime"},
            )
        )

    async def on_user_speech_stopped(self) -> None:
        self.speech_active = False

    async def on_user_transcript_final(self, text: str) -> None:
        self.speech_active = False
        await self._orchestrator.handle_user_message(text, source="realtime")

    async def on_user_interrupted(self) -> None:
        await self._orchestrator.handle_interrupt(reason="realtime")

    def translate(self, livekit_event: dict[str, Any]) -> dict[str, Any]:
        """Pure mapping helper documenting the LiveKit -> internal event shape."""

        raw_type = str(livekit_event.get("type") or livekit_event.get("event") or "")
        mapping = {
            "USER_SPEECH_STARTED": "user_speech_started",
            "USER_SPEECH_STOPPED": "user_speech_stopped",
            "USER_TRANSCRIPT_FINAL": "user_transcript_final",
            "USER_INTERRUPTED": "user_interrupted",
        }
        return {
            "internal_type": mapping.get(raw_type, "unknown"),
            "text": livekit_event.get("text"),
            "event_id": new_id("lk"),
            "timestamp": utcnow().isoformat(),
        }
