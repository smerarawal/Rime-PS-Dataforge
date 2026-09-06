"""Rule-based LLM used in mock mode and all automated tests."""

from __future__ import annotations

import re
from typing import Any

from backend.app.llm.base import LLMProvider
from backend.app.llm.schemas import IntentAnalysis

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_FOLLOW_UP_HINTS = (
    "actually",
    "wait",
    "instead",
    "make it",
    "change",
    "cheaper",
    "under",
    "below",
    "less than",
    "budget",
)

_BUSY_STATUSES = {"THINKING", "EXECUTING", "SPEAKING", "CANCELLING"}


class MockLLMProvider(LLMProvider):
    name = "mock"

    async def analyze_intent(
        self,
        user_input: str,
        *,
        current_parameters: dict[str, Any],
        current_intent: str | None,
        conversation_status: str,
        current_request_id: str | None = None,
    ) -> IntentAnalysis:
        text = user_input.strip()
        lowered = text.lower()
        parameters: dict[str, Any] = {}

        city = _extract_city(text)
        if city:
            parameters["city"] = city

        budget = _extract_budget(lowered)
        if budget is not None:
            parameters["budget_max"] = budget

        has_context = bool(current_parameters)
        mentions_city = "city" in parameters
        mentions_budget = "budget_max" in parameters
        follow_hint = any(hint in lowered for hint in _FOLLOW_UP_HINTS)

        is_follow_up = False
        if has_context and follow_hint:
            is_follow_up = True
        elif has_context and mentions_budget and not mentions_city:
            is_follow_up = True
        elif has_context and not mentions_city and not mentions_budget and follow_hint:
            is_follow_up = True

        is_interruption = is_follow_up and conversation_status in _BUSY_STATUSES

        if mentions_city or current_parameters.get("city") or "hotel" in lowered:
            intent = "modify_search" if is_follow_up else "search_hotels"
            return IntentAnalysis(
                intent=intent,
                parameters=parameters,
                is_follow_up=is_follow_up,
                is_interruption=is_interruption,
                target_request=current_request_id if is_follow_up else None,
                requested_action="execute_tool",
                tool_name="search_hotels",
            )

        return IntentAnalysis(
            intent="chat",
            parameters=parameters,
            is_follow_up=is_follow_up,
            is_interruption=is_interruption,
            requested_action="respond",
            tool_name=None,
        )


def _extract_city(text: str) -> str | None:
    match = re.search(r"\bin\s+([A-Za-z][A-Za-z\s]{1,40}?)(?:[.!?,]|$)", text, flags=re.IGNORECASE)
    if not match:
        return None
    city = match.group(1).strip(" .,!?")
    city = re.sub(r"\s+", " ", city)
    if city.lower() in {"the", "a", "an"}:
        return None
    return city.title()


def _extract_budget(lowered: str) -> int | None:
    numeric = re.search(r"(?:under|below|less than|budget(?:\s+of)?)\s*₹?\s*(\d[\d,]*)", lowered)
    if numeric:
        return int(numeric.group(1).replace(",", ""))

    words = re.search(
        r"(?:under|below|less than)\s+([a-z]+)(?:\s+(thousand|hundred))?",
        lowered,
    )
    if not words:
        return None
    amount = _NUMBER_WORDS.get(words.group(1))
    if amount is None:
        return None
    scale = words.group(2)
    if scale == "thousand":
        return amount * 1000
    if scale == "hundred":
        return amount * 100
    return amount
