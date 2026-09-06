"""Per-conversation orchestrator registry. Frontend does not own state."""

from __future__ import annotations

from collections.abc import Callable

from backend.app.core.orchestrator import Orchestrator
from backend.app.factory import create_orchestrator


class SessionRegistry:
    def __init__(self, factory: Callable[[str], Orchestrator] | None = None) -> None:
        self._factory = factory or create_orchestrator
        self._sessions: dict[str, Orchestrator] = {}

    def get_or_create(self, conversation_id: str) -> Orchestrator:
        if conversation_id not in self._sessions:
            self._sessions[conversation_id] = self._factory(conversation_id)
        return self._sessions[conversation_id]

    def get(self, conversation_id: str) -> Orchestrator | None:
        return self._sessions.get(conversation_id)
