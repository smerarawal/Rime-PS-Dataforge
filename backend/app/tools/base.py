"""Async tool interface. Tools do not fence their own results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from backend.app.core.cancellation import CancellationToken
from backend.app.models.tool_models import ToolResult, ToolSpec


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    @abstractmethod
    async def execute(
        self,
        parameters: dict[str, Any],
        token: CancellationToken | None = None,
        *,
        request_id: str,
        generation_id: str,
        task_id: str | None = None,
    ) -> ToolResult:
        raise NotImplementedError

    async def cancel(self) -> None:
        """Optional cooperative cancellation hook."""
        return None
