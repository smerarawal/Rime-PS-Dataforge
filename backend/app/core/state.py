"""Conversation state owner. No hidden globals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from backend.app.models.conversation import ConversationState, ConversationStatus
from backend.app.utils.timing import utcnow


class ConversationStore:
    """Mutable store with a single lock. Callers always receive snapshots."""

    def __init__(self, conversation_id: str) -> None:
        self._lock = asyncio.Lock()
        self._state = ConversationState(conversation_id=conversation_id)

    @property
    def conversation_id(self) -> str:
        return self._state.conversation_id

    def snapshot_sync(self) -> ConversationState:
        return self._state.model_copy(deep=True)

    async def snapshot(self) -> ConversationState:
        async with self._lock:
            return self._state.model_copy(deep=True)

    async def apply(self, mutator: Callable[[ConversationState], None]) -> ConversationState:
        async with self._lock:
            mutator(self._state)
            self._state.timestamps.updated_at = utcnow()
            return self._state.model_copy(deep=True)

    async def set_status(self, status: ConversationStatus) -> ConversationState:
        return await self.apply(lambda state: setattr(state, "status", status))

    async def bind_request(
        self,
        request_id: str | None,
        generation_id: str | None,
        intent: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ConversationState:
        def _mutate(state: ConversationState) -> None:
            state.current_request_id = request_id
            state.generation_id = generation_id
            if intent is not None:
                state.current_intent = intent
            if parameters is not None:
                state.previous_parameters = dict(state.current_parameters)
                state.current_parameters = dict(parameters)

        return await self.apply(_mutate)
