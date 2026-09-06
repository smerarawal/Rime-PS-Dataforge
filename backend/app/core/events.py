"""Internal event model. Adapters subscribe; they do not own state."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.app.utils.ids import new_id
from backend.app.utils.logging import get_logger
from backend.app.utils.timing import utcnow

logger = get_logger(__name__)

Subscriber = Callable[["AppEvent"], Awaitable[None] | None]


class EventType(str, Enum):
    USER_TURN = "user_turn"
    INTERRUPTION = "interruption"
    REQUEST_CREATED = "request_created"
    REQUEST_INVALIDATED = "request_invalidated"
    TASK_STARTED = "task_started"
    TASK_CANCELLED = "task_cancelled"
    TASK_COMPLETED = "task_completed"
    STALE_RESULT_DISCARDED = "stale_result_discarded"
    RESULT_ACCEPTED = "result_accepted"
    ASSISTANT_THINKING = "assistant_thinking"
    ASSISTANT_RESPONSE_READY = "assistant_response_ready"
    TTS_START = "tts_start"
    TTS_STOP = "tts_stop"
    STATE_UPDATED = "state_updated"
    ERROR = "error"


class AppEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    timestamp: datetime = Field(default_factory=utcnow)
    conversation_id: str
    request_id: str | None = None
    generation_id: str | None = None
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class EventBus:
    """Fan-out bus. Subscriber failures never break the orchestrator."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._history: list[AppEvent] = []
        self._lock = asyncio.Lock()

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def emit(self, event: AppEvent) -> AppEvent:
        async with self._lock:
            self._history.append(event)
        for callback in list(self._subscribers):
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("event_subscriber_failed event_type=%s", event.event_type.value)
        return event

    def history(self) -> list[AppEvent]:
        return list(self._history)

    def of_type(self, *types: EventType) -> list[AppEvent]:
        wanted = set(types)
        return [event for event in self._history if event.event_type in wanted]

    async def wait_for(
        self,
        predicate: Callable[[AppEvent], bool],
        timeout: float = 5.0,
    ) -> AppEvent:
        for event in self._history:
            if predicate(event):
                return event

        loop = asyncio.get_running_loop()
        future: asyncio.Future[AppEvent] = loop.create_future()

        def _watch(event: AppEvent) -> None:
            if not future.done() and predicate(event):
                future.set_result(event)

        unsubscribe = self.subscribe(_watch)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()
