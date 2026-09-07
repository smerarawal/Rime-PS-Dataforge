"""Concurrency-safe request and generation lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.app.core.errors import InvalidRequestError
from backend.app.models.requests import Request, RequestStatus
from backend.app.utils.ids import new_id
from backend.app.utils.timing import utcnow


class RequestManager:
    """Owns request identity. The current pair is (request_id, generation_id)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[str, Request] = {}
        self._current_request_id: str | None = None
        self._current_generation_id: str | None = None
        self._sequence = 0

    async def new_request(
        self,
        user_input: str,
        intent: str | None = None,
        parameters: dict[str, Any] | None = None,
        parent_request_id: str | None = None,
    ) -> Request:
        if not user_input or not user_input.strip():
            raise InvalidRequestError("user_input must be a non-empty string")

        async with self._lock:
            parent = parent_request_id if parent_request_id is not None else self._current_request_id
            if self._current_request_id:
                self._invalidate_unlocked(self._current_request_id)

            self._sequence += 1
            request = Request(
                request_id=new_id("req"),
                generation_id=new_id("gen"),
                parent_request_id=parent,
                user_input=user_input.strip(),
                intent=intent,
                parameters=dict(parameters or {}),
                status=RequestStatus.ACTIVE,
                sequence_number=self._sequence,
            )
            self._requests[request.request_id] = request
            self._current_request_id = request.request_id
            self._current_generation_id = request.generation_id
            return request.model_copy(deep=True)

    async def invalidate_current_request(self) -> Request | None:
        async with self._lock:
            if not self._current_request_id:
                return None
            invalidated = self._invalidate_unlocked(self._current_request_id)
            self._current_request_id = None
            self._current_generation_id = None
            return invalidated

    async def mark_cancelled(self, request_id: str) -> Request:
        async with self._lock:
            request = self._require(request_id)
            if request.status != RequestStatus.COMPLETED:
                request.status = RequestStatus.CANCELLED
                request.cancelled_at = utcnow()
            if self._current_request_id == request_id:
                self._current_request_id = None
                self._current_generation_id = None
            return request.model_copy(deep=True)

    async def mark_completed(self, request_id: str) -> Request:
        async with self._lock:
            request = self._require(request_id)
            if request.status in {RequestStatus.CANCELLED, RequestStatus.OBSOLETE}:
                return request.model_copy(deep=True)
            request.status = RequestStatus.COMPLETED
            request.completed_at = utcnow()
            return request.model_copy(deep=True)

    async def is_current(self, request_id: str, generation_id: str) -> bool:
        async with self._lock:
            return self._is_current_unlocked(request_id, generation_id)

    def is_current_sync(self, request_id: str, generation_id: str) -> bool:
        return self._is_current_unlocked(request_id, generation_id)

    async def get_current_request(self) -> Request | None:
        async with self._lock:
            if not self._current_request_id:
                return None
            request = self._requests.get(self._current_request_id)
            return request.model_copy(deep=True) if request else None

    async def get_request(self, request_id: str) -> Request | None:
        async with self._lock:
            request = self._requests.get(request_id)
            return request.model_copy(deep=True) if request else None

    async def current_ids(self) -> tuple[str | None, str | None]:
        async with self._lock:
            return self._current_request_id, self._current_generation_id

    def _is_current_unlocked(self, request_id: str, generation_id: str) -> bool:
        request = self._requests.get(request_id)
        if request is None:
            return False
        if self._current_request_id != request_id:
            return False
        if self._current_generation_id != generation_id:
            return False
        if request.generation_id != generation_id:
            return False
        if request.status in {RequestStatus.CANCELLED, RequestStatus.OBSOLETE}:
            return False
        return True

    def _invalidate_unlocked(self, request_id: str) -> Request | None:
        request = self._requests.get(request_id)
        if request is None:
            return None
        if request.status != RequestStatus.COMPLETED:
            request.status = RequestStatus.OBSOLETE
            request.cancelled_at = request.cancelled_at or utcnow()
        return request.model_copy(deep=True)

    def _require(self, request_id: str) -> Request:
        request = self._requests.get(request_id)
        if request is None:
            raise InvalidRequestError(f"unknown request_id: {request_id}")
        return request
