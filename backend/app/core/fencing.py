"""Stale-result fencing. This is the correctness guarantee, not cancellation."""

from __future__ import annotations

from pydantic import BaseModel

from backend.app.core.request_manager import RequestManager
from backend.app.models.requests import RequestStatus


class FenceDecision(BaseModel):
    accepted: bool
    reason: str
    request_id: str
    generation_id: str

    @property
    def discarded(self) -> bool:
        return not self.accepted


class ResultValidator:
    """Central gate. Tools never decide whether their own result is current."""

    def __init__(self, request_manager: RequestManager) -> None:
        self._requests = request_manager

    async def validate(self, request_id: str, generation_id: str) -> FenceDecision:
        request = await self._requests.get_request(request_id)
        if request is None:
            return FenceDecision(
                accepted=False,
                reason="unknown_request",
                request_id=request_id,
                generation_id=generation_id,
            )

        current_id, current_generation = await self._requests.current_ids()

        if current_id != request_id:
            return FenceDecision(
                accepted=False,
                reason="not_current_request",
                request_id=request_id,
                generation_id=generation_id,
            )
        if current_generation != generation_id or request.generation_id != generation_id:
            return FenceDecision(
                accepted=False,
                reason="not_current_generation",
                request_id=request_id,
                generation_id=generation_id,
            )
        if request.status == RequestStatus.CANCELLED:
            return FenceDecision(
                accepted=False,
                reason="request_cancelled",
                request_id=request_id,
                generation_id=generation_id,
            )
        if request.status == RequestStatus.OBSOLETE:
            return FenceDecision(
                accepted=False,
                reason="request_obsolete",
                request_id=request_id,
                generation_id=generation_id,
            )
        return FenceDecision(
            accepted=True,
            reason="current",
            request_id=request_id,
            generation_id=generation_id,
        )
