"""Deterministic interruption demo. No LiveKit, Rime, or Gemini required.

Run from the repository root:

    python -m backend.demo.interruption_demo
"""

from __future__ import annotations

import asyncio
import time

from backend.app.adapters.rime import MockTTSProvider
from backend.app.core.events import AppEvent, EventType
from backend.app.core.orchestrator import Orchestrator
from backend.app.llm.mock import MockLLMProvider
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.app.tools.registry import ToolRegistry

_LABELS = {
    EventType.REQUEST_CREATED: "REQUEST_CREATED",
    EventType.TASK_STARTED: "TASK_STARTED",
    EventType.INTERRUPTION: "INTERRUPTION",
    EventType.REQUEST_INVALIDATED: "REQUEST_INVALIDATED",
    EventType.TASK_CANCELLED: "TASK_CANCELLED",
    EventType.TASK_COMPLETED: "TASK_COMPLETED",
    EventType.RESULT_ACCEPTED: "RESULT_ACCEPTED",
    EventType.STALE_RESULT_DISCARDED: "STALE_RESULT_DISCARDED",
    EventType.ASSISTANT_RESPONSE_READY: "ASSISTANT_RESPONSE_READY",
    EventType.TTS_START: "TTS_START",
    EventType.TTS_STOP: "TTS_STOP",
}


class _AliasBook:
    def __init__(self) -> None:
        self._request: dict[str, str] = {}
        self._generation: dict[str, str] = {}
        self._task: dict[str, str] = {}

    def request(self, request_id: str | None) -> str:
        if not request_id:
            return "-"
        if request_id not in self._request:
            self._request[request_id] = chr(ord("A") + len(self._request))
        return self._request[request_id]

    def generation(self, generation_id: str | None) -> str:
        if not generation_id:
            return "-"
        if generation_id not in self._generation:
            self._generation[generation_id] = str(len(self._generation) + 1)
        return self._generation[generation_id]

    def task(self, task_id: str | None) -> str:
        if not task_id:
            return "-"
        if task_id not in self._task:
            self._task[task_id] = f"T{len(self._task) + 1}"
        return self._task[task_id]


def _format_event(event: AppEvent, started: float, aliases: _AliasBook) -> str | None:
    label = _LABELS.get(event.event_type)
    if label is None:
        return None
    elapsed = event.timestamp.timestamp() - started
    request = aliases.request(event.request_id)
    generation = aliases.generation(event.generation_id)
    task = aliases.task(event.payload.get("task_id"))
    extra = ""
    if event.event_type in {EventType.TASK_STARTED, EventType.TASK_CANCELLED, EventType.TASK_COMPLETED}:
        extra = f" task={task}"
    if event.event_type == EventType.STALE_RESULT_DISCARDED:
        extra = f" reason={event.payload.get('reason')}"
    return f"[{elapsed:05.2f}] {label:<24} request={request} gen={generation}{extra}"


async def run_demo() -> list[str]:
    tool = FakeSlowSearchTool(
        call_plan=[
            {"delay_seconds": 5.0, "ignore_cancellation": True},
            {"delay_seconds": 2.0, "ignore_cancellation": False},
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    orch = Orchestrator(
        "conv_demo",
        llm=MockLLMProvider(),
        tts=MockTTSProvider(chunk_delay_seconds=0.01),
        tools=registry,
        speak_bridging=True,
        cancel_timeout=0.2,
    )

    wall_start = time.time()
    print("=== Rime interruption demo (mock mode) ===")
    print("User: Find hotels in Mumbai.")
    await orch.handle_user_message("Find hotels in Mumbai.")

    await asyncio.sleep(1.2)
    print("User: Actually, under five thousand.")
    await orch.handle_user_message("Actually, under five thousand.")

    await asyncio.sleep(4.2)

    aliases = _AliasBook()
    lines = ["=== Event timeline ==="]
    for event in orch.events.history():
        formatted = _format_event(event, wall_start, aliases)
        if formatted:
            lines.append(formatted)
            print(formatted)

    state = orch.get_state()
    lines.append("=== Final state ===")
    lines.append(f"parameters={state.current_parameters}")
    lines.append(f"last_assistant={state.last_assistant_message}")
    lines.append(f"interruption_count={state.interruption_count}")
    print("=== Final state ===")
    print(f"parameters={state.current_parameters}")
    print(f"last_assistant={state.last_assistant_message}")
    print(f"interruption_count={state.interruption_count}")
    print(f"latencies_ms={orch.latencies.snapshot()['latencies_ms']}")
    return lines


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
