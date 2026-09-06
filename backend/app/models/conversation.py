"""Conversation state models. The Orchestrator is the only writer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.app.utils.timing import utcnow


class ConversationStatus(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class StateTimestamps(BaseModel):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_user_at: datetime | None = None
    last_assistant_at: datetime | None = None
    last_interrupt_at: datetime | None = None


class ConversationState(BaseModel):
    conversation_id: str
    current_request_id: str | None = None
    generation_id: str | None = None
    current_intent: str | None = None
    current_parameters: dict[str, Any] = Field(default_factory=dict)
    previous_parameters: dict[str, Any] = Field(default_factory=dict)
    active_task_ids: list[str] = Field(default_factory=list)
    status: ConversationStatus = ConversationStatus.IDLE
    last_user_message: str | None = None
    last_assistant_message: str | None = None
    interruption_count: int = 0
    timestamps: StateTimestamps = Field(default_factory=StateTimestamps)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
