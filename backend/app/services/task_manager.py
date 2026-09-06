"""Asyncio task lifecycle. Every tool run is a managed task."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from backend.app.core.cancellation import CancellationToken, TaskLifecycle, cancel_and_wait
from backend.app.utils.ids import new_id
from backend.app.utils.timing import utcnow


class ManagedTask:
    def __init__(
        self,
        task_id: str,
        request_id: str,
        generation_id: str,
        token: CancellationToken,
    ) -> None:
        self.task_id = task_id
        self.request_id = request_id
        self.generation_id = generation_id
        self.token = token
        self.status = TaskLifecycle.PENDING
        self.task: asyncio.Task[Any] | None = None
        self.cancellable = True
        self.created_at = utcnow()
        self.started_at = None
        self.finished_at = None
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "generation_id": self.generation_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
        }


class TaskManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, ManagedTask] = {}

    async def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        request_id: str,
        generation_id: str,
        task_id: str | None = None,
        token: CancellationToken | None = None,
        cancellable: bool = True,
    ) -> ManagedTask:
        managed = ManagedTask(
            task_id=task_id or new_id("task"),
            request_id=request_id,
            generation_id=generation_id,
            token=token or CancellationToken(),
        )
        managed.cancellable = cancellable
        managed.status = TaskLifecycle.RUNNING
        managed.started_at = utcnow()

        async def _runner() -> Any:
            try:
                return await coro
            except asyncio.CancelledError:
                managed.status = TaskLifecycle.CANCELLED
                managed.finished_at = utcnow()
                raise
            except Exception as exc:
                managed.status = TaskLifecycle.FAILED
                managed.finished_at = utcnow()
                managed.error = str(exc)
                raise
            else:
                if managed.status != TaskLifecycle.CANCELLED:
                    managed.status = TaskLifecycle.COMPLETED
                    managed.finished_at = utcnow()

        managed.task = asyncio.create_task(_runner(), name=managed.task_id)
        async with self._lock:
            self._tasks[managed.task_id] = managed
        managed.task.add_done_callback(lambda _t: self._schedule_remove(managed.task_id))
        return managed

    def _schedule_remove(self, task_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._remove_if_done(task_id))

    async def _remove_if_done(self, task_id: str) -> None:
        async with self._lock:
            managed = self._tasks.get(task_id)
            if managed and managed.task and managed.task.done():
                # Keep a short window for status queries; do not leak forever.
                return

    async def cancel(self, task_id: str, timeout: float = 1.0) -> bool:
        async with self._lock:
            managed = self._tasks.get(task_id)
        if managed is None or managed.task is None:
            return True
        managed.token.cancel()
        managed.status = TaskLifecycle.CANCEL_REQUESTED
        if not managed.cancellable:
            return False
        finished = await cancel_and_wait(managed.task, timeout=timeout)
        if managed.task.cancelled():
            managed.status = TaskLifecycle.CANCELLED
            managed.finished_at = utcnow()
        return finished

    async def cancel_for_request(
        self,
        request_id: str,
        timeout: float = 1.0,
    ) -> list[str]:
        async with self._lock:
            targets = [
                managed.task_id
                for managed in self._tasks.values()
                if managed.request_id == request_id and managed.task and not managed.task.done()
            ]
        for task_id in targets:
            await self.cancel(task_id, timeout=timeout)
        return targets

    async def cancel_for_generation(
        self,
        generation_id: str,
        timeout: float = 1.0,
    ) -> list[str]:
        async with self._lock:
            targets = [
                managed.task_id
                for managed in self._tasks.values()
                if managed.generation_id == generation_id
                and managed.task
                and not managed.task.done()
            ]
        for task_id in targets:
            await self.cancel(task_id, timeout=timeout)
        return targets

    async def active_task_ids(self) -> list[str]:
        async with self._lock:
            return [
                task_id
                for task_id, managed in self._tasks.items()
                if managed.task is not None and not managed.task.done()
            ]

    async def get(self, task_id: str) -> ManagedTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def status_report(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [managed.to_dict() for managed in self._tasks.values()]

    async def wait(self, task_id: str) -> Any:
        async with self._lock:
            managed = self._tasks.get(task_id)
        if managed is None or managed.task is None:
            raise KeyError(task_id)
        return await managed.task

    async def cancel_all(self, timeout: float = 1.0) -> None:
        async with self._lock:
            ids = list(self._tasks)
        for task_id in ids:
            await self.cancel(task_id, timeout=timeout)
