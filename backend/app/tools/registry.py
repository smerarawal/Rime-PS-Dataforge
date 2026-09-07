"""Named tool registry. The orchestrator looks up tools by name only."""

from __future__ import annotations

from backend.app.core.errors import InvalidRequestError
from backend.app.models.tool_models import ToolSpec
from backend.app.tools.base import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise InvalidRequestError(f"unknown tool: {name}")
        return tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)
