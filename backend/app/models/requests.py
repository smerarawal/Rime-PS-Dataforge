"""Request lifecycle models owned by RequestManager."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.app.utils.timing import utcnow


class RequestStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    OBSOLETE = "OBSOLETE"


class Request(BaseModel):
    request_id: str
    generation_id: str
    parent_request_id: str | None = None
    user_input: str
    intent: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: RequestStatus = RequestStatus.ACTIVE
    sequence_number: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    cancelled_at: datetime | None = None
    completed_at: datetime | None = None

    def is_terminal(self) -> bool:
        return self.status in {
            RequestStatus.CANCELLED,
            RequestStatus.COMPLETED,
            RequestStatus.OBSOLETE,
        }
