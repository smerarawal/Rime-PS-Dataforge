"""Cancellation helpers. Cancellation is an optimization; fencing is correctness."""

from __future__ import annotations

import asyncio
from enum import Enum


class TaskLifecycle(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CancellationToken:
    """Cooperative cancel signal that tools may poll. Tasks may ignore it."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


async def cancel_and_wait(task: asyncio.Task[object], timeout: float = 1.0) -> bool:
    """Ask a task to cancel.

    Returns True if the task finished after the cancel request (cancelled or
    completed). Returns False if it is still running — fencing must still reject
    any later result.
    """

    if task.done():
        return True
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return True
    except asyncio.CancelledError:
        return True
    except asyncio.TimeoutError:
        return False
    except Exception:
        return True
