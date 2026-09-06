"""Composition root. Providers are selected here, not inside the orchestrator."""

from __future__ import annotations

from backend.app.adapters.rime import MockTTSProvider, RimeTTSProvider, TTSProvider
from backend.app.config import Settings, get_settings
from backend.app.core.events import EventBus
from backend.app.core.orchestrator import Orchestrator
from backend.app.llm.base import LLMProvider
from backend.app.llm.mock import MockLLMProvider
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.app.tools.registry import ToolRegistry
from backend.app.utils.logging import get_logger, log_operation

logger = get_logger(__name__)


def build_llm(settings: Settings | None = None) -> LLMProvider:
    cfg = settings or get_settings()
    if cfg.llm_provider == "gemini":
        if not cfg.gemini_configured():
            log_operation(logger, "gemini_missing_key_fallback_mock")
            return MockLLMProvider()
        try:
            from backend.app.llm.gemini import GeminiProvider

            return GeminiProvider(api_key=cfg.gemini_api_key, model=cfg.gemini_model)
        except Exception as exc:
            log_operation(logger, "gemini_init_failed_fallback_mock", error=str(exc))
            return MockLLMProvider()
    return MockLLMProvider()


def build_tts(settings: Settings | None = None) -> TTSProvider:
    cfg = settings or get_settings()
    if cfg.tts_provider == "rime":
        if not cfg.rime_configured():
            log_operation(logger, "rime_missing_key_fallback_mock")
            return MockTTSProvider()
        try:
            return RimeTTSProvider(
                api_key=cfg.rime_api_key,
                model_id=cfg.rime_model_id,
                speaker=cfg.rime_speaker,
                sampling_rate=cfg.rime_sampling_rate,
            )
        except Exception as exc:
            log_operation(logger, "rime_init_failed_fallback_mock", error=str(exc))
            return MockTTSProvider()
    return MockTTSProvider()


def build_tools(
    settings: Settings | None = None,
    search_tool: FakeSlowSearchTool | None = None,
) -> ToolRegistry:
    cfg = settings or get_settings()
    registry = ToolRegistry()
    registry.register(search_tool or FakeSlowSearchTool(delay_seconds=cfg.tool_delay_seconds))
    return registry


def create_orchestrator(
    conversation_id: str,
    *,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    tts: TTSProvider | None = None,
    tools: ToolRegistry | None = None,
    event_bus: EventBus | None = None,
    speak_bridging: bool = True,
    cancel_timeout: float = 0.2,
) -> Orchestrator:
    cfg = settings or get_settings()
    return Orchestrator(
        conversation_id,
        llm=llm or build_llm(cfg),
        tts=tts or build_tts(cfg),
        tools=tools or build_tools(cfg),
        event_bus=event_bus or EventBus(),
        speak_bridging=speak_bridging,
        cancel_timeout=cancel_timeout,
    )
