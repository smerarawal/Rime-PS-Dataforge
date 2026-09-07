"""LLM provider interface. Application code depends on this, not Gemini."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from backend.app.llm.schemas import IntentAnalysis


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def analyze_intent(
        self,
        user_input: str,
        *,
        current_parameters: dict[str, Any],
        current_intent: str | None,
        conversation_status: str,
        current_request_id: str | None = None,
    ) -> IntentAnalysis:
        raise NotImplementedError

    async def close(self) -> None:
        return None
