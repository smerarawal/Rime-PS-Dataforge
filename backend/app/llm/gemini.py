"""Gemini 2.5 Flash provider. Isolated behind LLMProvider."""

from __future__ import annotations

import json
from typing import Any

from backend.app.core.errors import LLMError, MalformedLLMOutputError, ProviderUnavailableError
from backend.app.llm.base import LLMProvider
from backend.app.llm.schemas import IntentAnalysis
from backend.app.utils.logging import get_logger, log_operation

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You analyze user speech for a realtime hotel-search voice agent.
Return ONLY JSON matching this schema:
{
  "intent": string,
  "parameters": object,
  "is_follow_up": boolean,
  "is_interruption": boolean,
  "target_request": string | null,
  "requested_action": "execute_tool" | "respond" | "cancel",
  "tool_name": string | null,
  "confidence": number
}

Rules:
- If the user is modifying a previous search (budget, dates, filters) set is_follow_up=true
  and include ONLY the changed parameters.
- Preserve implied context by not resetting unspecified parameters.
- Use tool_name="search_hotels" for hotel search or modifications.
- Use requested_action="execute_tool" when a tool should run.
- Never include secrets, API keys, or chain-of-thought.
"""


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        if not api_key or not api_key.strip():
            raise ProviderUnavailableError("GEMINI_API_KEY is missing")
        try:
            from google import genai
        except ImportError as exc:
            raise ProviderUnavailableError("google-genai is not installed") from exc

        self._model_name = model
        self._client = genai.Client(api_key=api_key.strip())

    async def analyze_intent(
        self,
        user_input: str,
        *,
        current_parameters: dict[str, Any],
        current_intent: str | None,
        conversation_status: str,
        current_request_id: str | None = None,
    ) -> IntentAnalysis:
        prompt = (
            f"{_SYSTEM_PROMPT}\n"
            f"current_intent: {current_intent}\n"
            f"current_parameters: {json.dumps(current_parameters)}\n"
            f"conversation_status: {conversation_status}\n"
            f"current_request_id: {current_request_id}\n"
            f"user_input: {user_input}\n"
        )
        try:
            response = await self._generate(prompt)
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            log_operation(logger, "gemini_failure", error=str(exc))
            raise LLMError(f"Gemini request failed: {exc}") from exc

        return self._parse(response)

    async def _generate(self, prompt: str) -> str:
        import asyncio

        def _call() -> str:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                },
            )
            text = getattr(response, "text", None)
            if not text:
                raise LLMError("Gemini returned an empty response")
            return text

        return await asyncio.to_thread(_call)

    def _parse(self, raw: str) -> IntentAnalysis:
        try:
            payload = json.loads(raw)
            return IntentAnalysis.model_validate(payload)
        except Exception as exc:
            raise MalformedLLMOutputError(f"Gemini output was not valid IntentAnalysis: {exc}") from exc
