from __future__ import annotations

import asyncio

from backend.app.core.request_manager import RequestManager
from backend.app.models.requests import RequestStatus


async def test_new_request_uses_unique_ids() -> None:
    manager = RequestManager()
    first = await manager.new_request("Find hotels in Mumbai", intent="search_hotels")
    second = await manager.new_request("Actually under 5000", intent="modify_search")
    assert first.request_id != second.request_id
    assert first.generation_id != second.generation_id
    assert first.request_id.startswith("req_")
    assert second.generation_id.startswith("gen_")
    assert second.parent_request_id == first.request_id
    assert second.sequence_number == 2


async def test_invalidate_makes_previous_not_current() -> None:
    manager = RequestManager()
    first = await manager.new_request("one")
    assert await manager.is_current(first.request_id, first.generation_id)
    await manager.invalidate_current_request()
    assert not await manager.is_current(first.request_id, first.generation_id)
    stored = await manager.get_request(first.request_id)
    assert stored is not None
    assert stored.status == RequestStatus.OBSOLETE


async def test_mark_cancelled_and_completed() -> None:
    manager = RequestManager()
    request = await manager.new_request("one")
    cancelled = await manager.mark_cancelled(request.request_id)
    assert cancelled.status == RequestStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert not await manager.is_current(request.request_id, request.generation_id)

    later = await manager.new_request("two")
    completed = await manager.mark_completed(later.request_id)
    assert completed.status == RequestStatus.COMPLETED
    assert completed.completed_at is not None


async def test_concurrent_new_requests_leave_one_current() -> None:
    manager = RequestManager()

    async def _create(text: str) -> None:
        await manager.new_request(text)

    await asyncio.gather(*[_create(f"msg-{index}") for index in range(20)])
    current = await manager.get_current_request()
    assert current is not None
    assert await manager.is_current(current.request_id, current.generation_id)
    assert current.sequence_number == 20
