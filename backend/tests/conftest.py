from __future__ import annotations

import pytest

from backend.app.adapters.rime import MockTTSProvider
from backend.app.core.orchestrator import Orchestrator
from backend.app.llm.mock import MockLLMProvider
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.app.tools.registry import ToolRegistry


def make_orchestrator(
    conversation_id: str = "conv_test",
    *,
    tool: FakeSlowSearchTool | None = None,
    delay_seconds: float = 0.05,
    speak_bridging: bool = False,
    cancel_timeout: float = 0.05,
    ignore_cancellation: bool = False,
    call_plan: list | None = None,
) -> Orchestrator:
    search = tool or FakeSlowSearchTool(
        delay_seconds=delay_seconds,
        ignore_cancellation=ignore_cancellation,
        call_plan=call_plan,
    )
    registry = ToolRegistry()
    registry.register(search)
    return Orchestrator(
        conversation_id,
        llm=MockLLMProvider(),
        tts=MockTTSProvider(chunk_delay_seconds=0.0),
        tools=registry,
        speak_bridging=speak_bridging,
        cancel_timeout=cancel_timeout,
    )


@pytest.fixture
def orchestrator() -> Orchestrator:
    return make_orchestrator()
