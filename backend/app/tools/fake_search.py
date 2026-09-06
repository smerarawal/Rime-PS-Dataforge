"""Deterministic slow search tool for tests and the interruption demo."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.app.core.cancellation import CancellationToken
from backend.app.models.tool_models import ToolResult
from backend.app.tools.base import BaseTool

FAKE_HOTELS: dict[str, list[dict[str, Any]]] = {
    "mumbai": [
        {"id": "h1", "name": "Marine Bay Inn", "city": "Mumbai", "price_inr": 3200, "rating": 4.2},
        {"id": "h2", "name": "Gateway Stay", "city": "Mumbai", "price_inr": 4500, "rating": 4.0},
        {"id": "h3", "name": "Colaba Heights", "city": "Mumbai", "price_inr": 7800, "rating": 4.6},
        {"id": "h4", "name": "Bandra Budget Lodge", "city": "Mumbai", "price_inr": 2100, "rating": 3.8},
    ],
    "delhi": [
        {"id": "d1", "name": "Connaught Rest", "city": "Delhi", "price_inr": 3900, "rating": 4.1},
        {"id": "d2", "name": "Karol Bagh Inn", "city": "Delhi", "price_inr": 2800, "rating": 3.9},
        {"id": "d3", "name": "India Gate Suites", "city": "Delhi", "price_inr": 8200, "rating": 4.7},
    ],
}


class FakeSlowSearchTool(BaseTool):
    name = "search_hotels"
    description = "Search for hotels by city and optional maximum budget. Simulated long-running work."
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "budget_max": {"type": "integer"},
        },
        "required": ["city"],
    }

    def __init__(
        self,
        delay_seconds: float = 4.0,
        ignore_cancellation: bool = False,
        call_plan: list[dict[str, Any]] | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.ignore_cancellation = ignore_cancellation
        self.call_plan = list(call_plan or [])
        self.started = started
        self.release = release
        self.calls = 0

    async def execute(
        self,
        parameters: dict[str, Any],
        token: CancellationToken | None = None,
        *,
        request_id: str,
        generation_id: str,
        task_id: str | None = None,
    ) -> ToolResult:
        self.calls += 1
        plan = self._plan_for_call()
        delay = float(plan.get("delay_seconds", self.delay_seconds))
        ignore = bool(plan.get("ignore_cancellation", self.ignore_cancellation))

        if self.started is not None:
            self.started.set()

        try:
            if self.release is not None:
                await self.release.wait()
            else:
                await self._sleep(delay, token=token, ignore_cancellation=ignore)
        except asyncio.CancelledError:
            if ignore:
                await asyncio.sleep(delay)
            else:
                raise

        city = str(parameters.get("city") or "").strip()
        budget = parameters.get("budget_max")
        hotels = self.search_hotels(city, budget_max=budget)
        return ToolResult(
            tool_name=self.name,
            ok=True,
            data={
                "city": city,
                "budget_max": budget,
                "hotels": hotels,
                "count": len(hotels),
            },
            request_id=request_id,
            generation_id=generation_id,
            task_id=task_id,
        )

    def search_hotels(
        self,
        city: str,
        budget_max: int | float | None = None,
    ) -> list[dict[str, Any]]:
        key = city.strip().lower()
        hotels = [dict(item) for item in FAKE_HOTELS.get(key, [])]
        if budget_max is not None and budget_max != "":
            limit = int(budget_max)
            hotels = [hotel for hotel in hotels if hotel["price_inr"] <= limit]
        return hotels

    def will_ignore_cancellation(self) -> bool:
        index = self.calls
        if 0 <= index < len(self.call_plan):
            return bool(self.call_plan[index].get("ignore_cancellation", self.ignore_cancellation))
        return self.ignore_cancellation

    def _plan_for_call(self) -> dict[str, Any]:
        index = self.calls - 1
        if 0 <= index < len(self.call_plan):
            return self.call_plan[index]
        return {}

    async def _sleep(
        self,
        delay: float,
        token: CancellationToken | None,
        ignore_cancellation: bool,
    ) -> None:
        remaining = delay
        step = 0.05
        while remaining > 0:
            if token is not None and token.is_cancelled and not ignore_cancellation:
                raise asyncio.CancelledError()
            await asyncio.sleep(min(step, remaining))
            remaining -= step
