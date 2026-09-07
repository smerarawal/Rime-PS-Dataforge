from __future__ import annotations

from backend.app.adapters.rime import MockTTSProvider
from backend.app.core.events import EventType
from backend.app.llm.mock import MockLLMProvider
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.app.tools.registry import ToolRegistry
from backend.app.core.orchestrator import Orchestrator
from backend.tests.conftest import make_orchestrator


async def test_tts_stop_on_interrupt() -> None:
    """TEST 7 — interruption emits TTS_STOP and stops mock speech."""

    tool = FakeSlowSearchTool(delay_seconds=0.4, ignore_cancellation=True)
    registry = ToolRegistry()
    registry.register(tool)
    tts = MockTTSProvider(chunk_delay_seconds=0.03)
    orch = Orchestrator(
        "conv_tts",
        llm=MockLLMProvider(),
        tts=tts,
        tools=registry,
        speak_bridging=True,
        cancel_timeout=0.05,
    )
    first = await orch.handle_user_message("Find hotels in Mumbai")
    assert first is not None
    await orch.events.wait_for(lambda event: event.event_type == EventType.TTS_START)
    await orch.handle_interrupt(reason="barge_in")
    stops = orch.events.of_type(EventType.TTS_STOP)
    assert stops
    assert tts.stopped_at is not None
    stale_after_stop = [
        event
        for event in orch.events.history()
        if event.timestamp > stops[0].timestamp
        and event.event_type == EventType.ASSISTANT_RESPONSE_READY
        and event.request_id == first.request_id
    ]
    assert stale_after_stop == []


async def test_full_interruption_flow() -> None:
    """TEST 8 — Mumbai search interrupted by budget follow-up."""

    orch = make_orchestrator(
        call_plan=[
            {"delay_seconds": 0.3, "ignore_cancellation": True},
            {"delay_seconds": 0.05, "ignore_cancellation": False},
        ],
        speak_bridging=True,
    )
    first = await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    await orch.events.wait_for(lambda event: event.event_type == EventType.TTS_START)
    second = await orch.handle_user_message("Actually under 5000.")
    assert first and second

    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == second.request_id
    )
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.STALE_RESULT_DISCARDED
        and event.request_id == first.request_id
    )

    types = [event.event_type for event in orch.events.history()]
    assert EventType.REQUEST_CREATED in types
    assert EventType.TASK_STARTED in types
    assert EventType.INTERRUPTION in types
    assert EventType.TTS_STOP in types
    assert EventType.REQUEST_INVALIDATED in types
    assert EventType.STALE_RESULT_DISCARDED in types
    assert EventType.RESULT_ACCEPTED in types
    assert EventType.ASSISTANT_RESPONSE_READY in types

    created = orch.events.of_type(EventType.REQUEST_CREATED)
    assert created[0].request_id == first.request_id
    assert created[-1].request_id == second.request_id

    state = orch.get_state()
    assert state.current_parameters == {"city": "Mumbai", "budget_max": 5000}
    assert state.interruption_count >= 1
    assert "under 5000" in (state.last_assistant_message or "")
    assert all(
        event.request_id != first.request_id
        for event in orch.events.of_type(EventType.RESULT_ACCEPTED)
    )


async def test_duplicate_interrupt_is_safe() -> None:
    orch = make_orchestrator(delay_seconds=0.4)
    await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    await orch.handle_interrupt()
    await orch.handle_interrupt()
    state = orch.get_state()
    assert state.interruption_count >= 2
    assert state.status.value in {"INTERRUPTED", "CANCELLING"}
