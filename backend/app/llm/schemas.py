"""Structured LLM outputs. Free-form text never drives the orchestrator."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IntentAnalysis(BaseModel):
    intent: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    is_follow_up: bool = False
    is_interruption: bool = False
    target_request: str | None = None
    requested_action: Literal["execute_tool", "respond", "cancel"] = "execute_tool"
    tool_name: str | None = None
    confidence: float = 1.0
