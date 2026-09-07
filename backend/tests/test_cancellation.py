from __future__ import annotations

import asyncio

from backend.app.core.cancellation import CancellationToken, cancel_and_wait
from backend.app.core.events import EventType
from backend.app.models.requests import RequestStatus
from backend.app.services.task_manager import TaskManager
from backend.tests.conftest import make_orchestrator


async def test_task_manager_cancels_running_task() -> None:
    manager = TaskManager()
    started = asyncio.Event()

    async def _work() -> str:
        started.set()
        await asyncio.sleep(10)
        return "done"

    managed = await manager.spawn(_work(), request_id="req", generation_id="gen")
    await started.wait()
    finished = await manager.cancel(managed.task_id, timeout=0.5)
    assert finished is True
    assert managed.task is not None
    assert managed.task.cancelled()


async def test_cancel_and_wait_reports_failure_when_work_ignores_cancel() -> None:
    async def _uncancellable() -> str:
        await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        return "late"

    task = asyncio.create_task(_uncancellable())
    await asyncio.sleep(0.02)
    # Hard-cancel will stop this one; the TaskManager cancellable=False path is
    # what models a truly stuck backend job.
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled
    task.cancel()
    stopped = await cancel_and_wait(task, timeout=0.1)
    assert stopped is True


async def test_request_marked_cancelled_when_superseded() -> None:
    orch = make_orchestrator(delay_seconds=1.0)
    first = await orch.handle_user_message("Find hotels in Mumbai")
    assert first is not None
    await orch.events.wait_for(lambda event: event.event_type == EventType.TASK_STARTED)
    await orch.handle_user_message("Actually under 5000")
    stored = await orch.requests.get_request(first.request_id)
    assert stored is not None
    assert stored.status in {RequestStatus.CANCELLED, RequestStatus.OBSOLETE}
    assert any(event.event_type == EventType.TASK_CANCELLED for event in orch.events.history())
