from __future__ import annotations

from backend.app.core.events import EventType
from backend.app.services.context_manager import ContextManager
from backend.tests.conftest import make_orchestrator


def test_context_manager_merges_follow_up_budget() -> None:
    manager = ContextManager()
    merged = manager.merge(
        {"city": "Mumbai"},
        {"budget_max": 5000},
        is_follow_up=True,
    )
    assert merged == {"city": "Mumbai", "budget_max": 5000}


def test_context_manager_replaces_on_new_request() -> None:
    manager = ContextManager()
    merged = manager.merge(
        {"city": "Mumbai", "budget_max": 5000},
        {"city": "Delhi"},
        is_follow_up=False,
    )
    assert merged == {"city": "Delhi"}


async def test_follow_up_keeps_city() -> None:
    """TEST 4 — context preservation."""

    orch = make_orchestrator(delay_seconds=0.02)
    first = await orch.handle_user_message("Find hotels in Mumbai")
    assert first is not None
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == first.request_id
    )
    second = await orch.handle_user_message("Actually under 5000")
    assert second is not None
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == second.request_id
    )
    state = orch.get_state()
    assert state.current_parameters["city"] == "Mumbai"
    assert state.current_parameters["budget_max"] == 5000
    assert state.previous_parameters["city"] == "Mumbai"
