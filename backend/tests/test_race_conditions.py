from __future__ import annotations

from backend.app.core.events import EventType
from backend.app.models.requests import RequestStatus
from backend.tests.conftest import make_orchestrator


async def test_cancellation_failure_still_fenced() -> None:
    """TEST 3 — fencing is stronger than cancellation."""

    orch = make_orchestrator(
        call_plan=[
            {"delay_seconds": 0.3, "ignore_cancellation": True},
            {"delay_seconds": 0.05, "ignore_cancellation": False},
        ]
    )
    first = await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    second = await orch.handle_user_message("Actually under 5000")
    assert first is not None
    assert second is not None

    stored = await orch.requests.get_request(first.request_id)
    assert stored is not None
    assert stored.status in {RequestStatus.CANCELLED, RequestStatus.OBSOLETE}

    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == second.request_id
    )
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.STALE_RESULT_DISCARDED
        and event.request_id == first.request_id
    )
    accepted_ids = [event.request_id for event in orch.events.of_type(EventType.RESULT_ACCEPTED)]
    assert accepted_ids == [second.request_id]


async def test_rapid_interruptions_only_last_delivers() -> None:
    """TEST 5 — A, interrupt, B, interrupt, C. Only C delivers."""

    orch = make_orchestrator(
        call_plan=[
            {"delay_seconds": 0.35, "ignore_cancellation": True},
            {"delay_seconds": 0.25, "ignore_cancellation": True},
            {"delay_seconds": 0.05, "ignore_cancellation": False},
        ]
    )
    first = await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    second = await orch.handle_user_message("Wait, in Delhi")
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.TASK_STARTED
        and event.request_id == second.request_id
    )
    third = await orch.handle_user_message("Actually under 5000")
    assert first and second and third

    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == third.request_id,
        timeout=2.0,
    )
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.STALE_RESULT_DISCARDED
        and event.request_id == first.request_id,
        timeout=2.0,
    )
    accepted = [event.request_id for event in orch.events.of_type(EventType.RESULT_ACCEPTED)]
    assert accepted == [third.request_id]
    finals = [
        event
        for event in orch.events.of_type(EventType.ASSISTANT_RESPONSE_READY)
        if "I found" in event.payload.get("text", "")
    ]
    assert len(finals) == 1
    assert finals[0].request_id == third.request_id
    state = orch.get_state()
    assert state.current_parameters["city"] == "Delhi"
    assert state.current_parameters["budget_max"] == 5000


async def test_out_of_order_tool_completion() -> None:
    """TEST 6 — only the current generation may propagate."""

    orch = make_orchestrator(
        call_plan=[
            {"delay_seconds": 0.30, "ignore_cancellation": True},
            {"delay_seconds": 0.18, "ignore_cancellation": True},
            {"delay_seconds": 0.04, "ignore_cancellation": False},
        ]
    )
    request_a = await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    request_b = await orch.handle_user_message("Find hotels in Delhi")
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.TASK_STARTED
        and event.request_id == request_b.request_id
    )
    request_c = await orch.handle_user_message("Find hotels in Mumbai")
    assert request_a and request_b and request_c

    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == request_c.request_id,
        timeout=2.0,
    )
    # Older tasks finish later and must be discarded.
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.STALE_RESULT_DISCARDED
        and event.request_id == request_a.request_id,
        timeout=2.0,
    )
    accepted = [event.request_id for event in orch.events.of_type(EventType.RESULT_ACCEPTED)]
    discarded = {event.request_id for event in orch.events.of_type(EventType.STALE_RESULT_DISCARDED)}
    assert accepted == [request_c.request_id]
    assert request_a.request_id in discarded
    assert request_b.request_id in discarded
