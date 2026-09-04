"""
test_turn_manager.py

Run with: pytest test_turn_manager.py -v
Requires: pip install pytest pytest-asyncio
"""

import asyncio
import pytest
from turn_manager import TurnManager, StaleResultError


def test_start_new_turn_increments():
    tm = TurnManager()
    assert tm.start_new_turn() == 1
    assert tm.start_new_turn() == 2


def test_stamp_returns_current_turn():
    tm = TurnManager()
    tm.start_new_turn()  # turn 1
    assert tm.stamp() == 1


def test_is_stale_false_when_turn_unchanged():
    tm = TurnManager()
    stamped = tm.stamp()  # turn 0
    assert tm.is_stale(stamped) is False


def test_is_stale_true_after_new_turn():
    tm = TurnManager()
    stamped = tm.stamp()  # turn 0
    tm.start_new_turn()   # turn 1
    assert tm.is_stale(stamped) is True


@pytest.mark.asyncio
async def test_guard_returns_result_when_not_stale():
    tm = TurnManager()
    stamped = tm.stamp()

    async def fast_work():
        return "ok"

    result = await tm.guard(fast_work(), stamped)
    assert result == "ok"


@pytest.mark.asyncio
async def test_guard_raises_on_stale():
    tm = TurnManager()
    stamped = tm.stamp()

    async def slow_work():
        await asyncio.sleep(0.1)
        return "done"

    task = asyncio.create_task(tm.guard(slow_work(), stamped))
    await asyncio.sleep(0.02)
    tm.start_new_turn()  # invalidate mid-flight

    with pytest.raises(StaleResultError):
        await task


@pytest.mark.asyncio
async def test_cancel_and_fence_returns_none_on_cancel():
    tm = TurnManager()
    stamped = tm.stamp()

    async def slow_work():
        await asyncio.sleep(1)
        return "should not see this"

    task = asyncio.create_task(slow_work())
    await asyncio.sleep(0.01)
    result = await tm.cancel_and_fence(task, stamped)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_and_fence_fallback_catches_finished_stale_task():
    """Simulates the exact race Atharva reported: cancel() is called but the
    task finishes anyway before cancellation lands. The fence check must
    still catch it."""
    tm = TurnManager()
    stamped = tm.stamp()

    async def fast_uncancellable_work():
        # Shield simulates a task that finishes despite cancel() being called
        await asyncio.sleep(0.01)
        return "leaked result"

    task = asyncio.create_task(asyncio.shield(fast_uncancellable_work()))
    await asyncio.sleep(0.02)  # let it finish first
    tm.start_new_turn()        # now mark it stale

    result = await tm.cancel_and_fence(task, stamped)
    assert result is None  # must be discarded even though the task "succeeded"


def test_audit_log_records_events():
    tm = TurnManager()
    tm.start_new_turn()
    stamped = tm.stamp()
    tm.start_new_turn()
    tm.is_stale(stamped)

    log = tm.audit_log()
    events = [entry["event"] for entry in log]
    assert "new_turn" in events
    assert "discarded_stale" in events
