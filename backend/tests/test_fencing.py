from __future__ import annotations

from backend.app.core.events import EventType
from backend.app.core.fencing import ResultValidator
from backend.app.core.request_manager import RequestManager
from backend.app.models.requests import RequestStatus
from backend.tests.conftest import make_orchestrator


async def test_validator_rejects_non_current_generation() -> None:
    manager = RequestManager()
    first = await manager.new_request("A")
    second = await manager.new_request("B")
    validator = ResultValidator(manager)

    rejected = await validator.validate(first.request_id, first.generation_id)
    accepted = await validator.validate(second.request_id, second.generation_id)
    assert rejected.discarded
    assert rejected.reason == "not_current_request"
    assert accepted.accepted
    assert second.status == RequestStatus.ACTIVE


async def test_older_result_discarded_when_newer_finishes_first() -> None:
    """TEST 1 — basic fencing: B accepted, A discarded."""

    orch = make_orchestrator(
        call_plan=[
            {"delay_seconds": 0.25, "ignore_cancellation": True},
            {"delay_seconds": 0.05, "ignore_cancellation": False},
        ]
    )
    first = await orch.handle_user_message("Find hotels in Mumbai")
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    second = await orch.handle_user_message("Actually under 5000")
    assert first is not None
    assert second is not None

    await orch.events.wait_for(
        lambda event: event.event_type == EventType.RESULT_ACCEPTED
        and event.request_id == second.request_id
    )
    await orch.events.wait_for(
        lambda event: event.event_type == EventType.STALE_RESULT_DISCARDED
        and event.request_id == first.request_id
    )

    accepted = orch.events.of_type(EventType.RESULT_ACCEPTED)
    discarded = orch.events.of_type(EventType.STALE_RESULT_DISCARDED)
    assert [event.request_id for event in accepted] == [second.request_id]
    assert any(event.request_id == first.request_id for event in discarded)
    state = orch.get_state()
    assert state.current_parameters["city"] == "Mumbai"
    assert state.current_parameters["budget_max"] == 5000
    assert state.last_assistant_message is not None
    assert "under 5000" in (state.last_assistant_message or "")
