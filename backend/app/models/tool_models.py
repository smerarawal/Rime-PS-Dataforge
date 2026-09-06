"""Tool contracts shared by the registry and the orchestrator."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_name: str
    ok: bool
    data: Any = None
    error: str | None = None
    request_id: str
    generation_id: str
    task_id: str | None = None
