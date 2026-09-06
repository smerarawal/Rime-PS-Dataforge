"""Orchestrator: the single owner of conversation state."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.app.adapters.rime import MockTTSProvider, TTSProvider
from backend.app.core.cancellation import CancellationToken
from backend.app.core.errors import InvalidRequestError, LLMError, ToolExecutionError
from backend.app.core.events import AppEvent, EventBus, EventType
from backend.app.core.fencing import ResultValidator
from backend.app.core.request_manager import RequestManager
from backend.app.core.state import ConversationStore
from backend.app.llm.base import LLMProvider
from backend.app.llm.mock import MockLLMProvider
from backend.app.llm.schemas import IntentAnalysis
from backend.app.models.conversation import ConversationState, ConversationStatus
from backend.app.models.requests import Request
from backend.app.models.tool_models import ToolResult
from backend.app.services.context_manager import ContextManager
from backend.app.services.task_manager import ManagedTask, TaskManager
from backend.app.tools.base import BaseTool
from backend.app.tools.registry import ToolRegistry
from backend.app.utils.ids import new_id
from backend.app.utils.logging import get_logger, log_operation
from backend.app.utils.timing import LatencyTracker, utcnow

logger = get_logger(__name__)

_BUSY = {
    ConversationStatus.THINKING,
    ConversationStatus.EXECUTING,
    ConversationStatus.SPEAKING,
    ConversationStatus.CANCELLING,
}


class Orchestrator:
    def __init__(
        self,
        conversation_id: str,
        *,
        llm: LLMProvider | None = None,
        tts: TTSProvider | None = None,
        tools: ToolRegistry | None = None,
        event_bus: EventBus | None = None,
        request_manager: RequestManager | None = None,
        task_manager: TaskManager | None = None,
        context_manager: ContextManager | None = None,
        speak_bridging: bool = True,
        cancel_timeout: float = 0.2,
    ) -> None:
        self.conversation_id = conversation_id
        self.store = ConversationStore(conversation_id)
        self.requests = request_manager or RequestManager()
        self.tasks = task_manager or TaskManager()
        self.validator = ResultValidator(self.requests)
        self.context = context_manager or ContextManager()
        self.llm = llm or MockLLMProvider()
        self.tts = tts or MockTTSProvider()
        self.tools = tools or ToolRegistry()
        self.events = event_bus or EventBus()
        self.latencies = LatencyTracker()
        self.speak_bridging = speak_bridging
        self.cancel_timeout = cancel_timeout
        self._lock = asyncio.Lock()
        self._seen_event_ids: set[str] = set()

    def get_state(self) -> ConversationState:
        return self.store.snapshot_sync()

    async def handle_user_message(self, text: str, source: str = "user") -> Request | None:
        cleaned = (text or "").strip()
        if not cleaned:
            await self._emit(EventType.ERROR, payload={"error": "empty_user_message"})
            raise InvalidRequestError("user_message text is required")

        self.latencies.mark("user_turn")
        state = await self.store.snapshot()
        await self._emit(EventType.USER_TURN, payload={"text": cleaned, "source": source})

        if state.status in _BUSY:
            await self.handle_interrupt(reason="superseded")
        state = await self.store.snapshot()

        try:
            analysis = await self.llm.analyze_intent(
                cleaned,
                current_parameters=state.current_parameters,
                current_intent=state.current_intent,
                conversation_status=state.status.value,
                current_request_id=state.current_request_id,
            )
        except LLMError as exc:
            await self.store.set_status(ConversationStatus.ERROR)
            await self._emit(EventType.ERROR, payload={"error": "llm_failure", "detail": str(exc)})
            return None

        merged = self.context.merge(
            state.current_parameters,
            analysis.parameters,
            is_follow_up=analysis.is_follow_up,
        )

        await self.store.apply(
            lambda current: (
                setattr(current, "last_user_message", cleaned),
                setattr(current.timestamps, "last_user_at", utcnow()),
            )
        )

        request = await self.start_request(cleaned, analysis, merged)
        if analysis.requested_action == "execute_tool" and analysis.tool_name:
            await self.execute_tool(analysis.tool_name, merged, request)
        elif analysis.requested_action == "respond":
            await self._deliver_response("I can help you search for hotels.", request)
        return request

    async def handle_interrupt(self, reason: str = "user") -> None:
        async with self._lock:
            await self._interrupt_unlocked(reason=reason)

    async def _interrupt_unlocked(self, reason: str) -> None:
        self.latencies.mark("interrupt_received")
        state = self.store.snapshot_sync()
        current = await self.requests.get_current_request()

        await self._emit(
            EventType.INTERRUPTION,
            request_id=state.current_request_id,
            generation_id=state.generation_id,
            payload={"reason": reason},
        )
        log_operation(
            logger,
            "interruption_detected",
            conversation_id=self.conversation_id,
            request_id=state.current_request_id,
            generation_id=state.generation_id,
            reason=reason,
        )

        await self._emit(
            EventType.TTS_STOP,
            request_id=state.current_request_id,
            generation_id=state.generation_id,
            payload={"reason": reason},
        )
        await self.tts.stop()
        self.latencies.mark("tts_stop")
        self.latencies.measure("tts_stop_latency_ms", "interrupt_received", "tts_stop")
        log_operation(
            logger,
            "tts_stopped",
            conversation_id=self.conversation_id,
            request_id=state.current_request_id,
            generation_id=state.generation_id,
        )

        await self.store.apply(
            lambda current_state: (
                setattr(current_state, "interruption_count", current_state.interruption_count + 1),
                setattr(current_state, "status", ConversationStatus.INTERRUPTED),
                setattr(current_state.timestamps, "last_interrupt_at", utcnow()),
            )
        )

        if current:
            await self._cancel_current_unlocked()

        await self._emit_state()

    async def start_request(
        self,
        text: str,
        analysis: IntentAnalysis,
        parameters: dict[str, Any],
    ) -> Request:
        async with self._lock:
            return await self._start_request_unlocked(text, analysis, parameters)

    async def _start_request_unlocked(
        self,
        text: str,
        analysis: IntentAnalysis,
        parameters: dict[str, Any],
    ) -> Request:
        previous = await self.requests.get_current_request()
        if previous:
            await self.requests.invalidate_current_request()
            await self._emit(
                EventType.REQUEST_INVALIDATED,
                request_id=previous.request_id,
                generation_id=previous.generation_id,
                payload={"reason": "superseded_by_new_request"},
            )
            log_operation(
                logger,
                "request_invalidated",
                conversation_id=self.conversation_id,
                request_id=previous.request_id,
                generation_id=previous.generation_id,
            )
            cancelled_ids = await self.tasks.cancel_for_request(
                previous.request_id,
                timeout=self.cancel_timeout,
            )
            for task_id in cancelled_ids:
                await self._emit(
                    EventType.TASK_CANCELLED,
                    request_id=previous.request_id,
                    generation_id=previous.generation_id,
                    payload={"task_id": task_id},
                )
                await self._drop_task(task_id)

        request = await self.requests.new_request(
            user_input=text,
            intent=analysis.intent,
            parameters=parameters,
            parent_request_id=previous.request_id if previous else None,
        )
        await self.store.bind_request(
            request.request_id,
            request.generation_id,
            intent=analysis.intent,
            parameters=parameters,
        )
        await self.store.set_status(ConversationStatus.THINKING)
        await self._emit(
            EventType.REQUEST_CREATED,
            request_id=request.request_id,
            generation_id=request.generation_id,
            payload={
                "intent": analysis.intent,
                "parameters": parameters,
                "is_follow_up": analysis.is_follow_up,
                "sequence_number": request.sequence_number,
            },
        )
        log_operation(
            logger,
            "request_created",
            conversation_id=self.conversation_id,
            request_id=request.request_id,
            generation_id=request.generation_id,
            intent=analysis.intent,
        )
        await self._emit(
            EventType.ASSISTANT_THINKING,
            request_id=request.request_id,
            generation_id=request.generation_id,
            payload={},
        )
        await self._emit_state()
        return request

    async def execute_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        request: Request,
    ) -> ManagedTask:
        tool = self.tools.get(tool_name)
        task_id = new_id("task")
        token = CancellationToken()
        cancellable = not self._tool_ignores_cancel(tool)

        async def _run() -> None:
            await self._emit(
                EventType.TASK_STARTED,
                request_id=request.request_id,
                generation_id=request.generation_id,
                payload={"task_id": task_id, "tool_name": tool_name},
            )
            log_operation(
                logger,
                "task_started",
                conversation_id=self.conversation_id,
                request_id=request.request_id,
                generation_id=request.generation_id,
                task_id=task_id,
                tool_name=tool_name,
            )
            try:
                result = await tool.execute(
                    parameters,
                    token,
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    task_id=task_id,
                )
            except asyncio.CancelledError:
                await self._emit(
                    EventType.TASK_CANCELLED,
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    payload={"task_id": task_id, "reason": "cancelled"},
                )
                log_operation(
                    logger,
                    "task_cancelled",
                    conversation_id=self.conversation_id,
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    task_id=task_id,
                )
                await self._drop_task(task_id)
                raise
            except Exception as exc:
                await self._emit(
                    EventType.ERROR,
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    payload={"error": "tool_failure", "detail": str(exc), "task_id": task_id},
                )
                result = ToolResult(
                    tool_name=tool_name,
                    ok=False,
                    error=str(exc),
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    task_id=task_id,
                )

            await self._emit(
                EventType.TASK_COMPLETED,
                request_id=request.request_id,
                generation_id=request.generation_id,
                payload={"task_id": task_id, "ok": result.ok},
            )
            log_operation(
                logger,
                "tool_completed",
                conversation_id=self.conversation_id,
                request_id=request.request_id,
                generation_id=request.generation_id,
                task_id=task_id,
                ok=result.ok,
            )
            await self._drop_task(task_id)
            await self.process_result(result, request.request_id, request.generation_id)

        managed = await self.tasks.spawn(
            _run(),
            request_id=request.request_id,
            generation_id=request.generation_id,
            task_id=task_id,
            token=token,
            cancellable=cancellable,
        )
        await self.store.apply(
            lambda state: state.active_task_ids.append(task_id)
            if task_id not in state.active_task_ids
            else None
        )
        await self.store.set_status(ConversationStatus.EXECUTING)
        await self._emit_state()

        if self.speak_bridging:
            bridging = self._bridging_text(parameters)
            await self._speak(bridging, request, kind="bridging")
        return managed

    async def process_result(
        self,
        result: ToolResult,
        request_id: str,
        generation_id: str,
    ) -> ToolResult | None:
        async with self._lock:
            decision = await self.validator.validate(request_id, generation_id)
            if decision.discarded:
                self.latencies.mark("stale_result_rejected")
                await self._emit(
                    EventType.STALE_RESULT_DISCARDED,
                    request_id=request_id,
                    generation_id=generation_id,
                    payload={
                        "reason": decision.reason,
                        "task_id": result.task_id,
                    },
                )
                log_operation(
                    logger,
                    "stale_result_discarded",
                    conversation_id=self.conversation_id,
                    request_id=request_id,
                    generation_id=generation_id,
                    task_id=result.task_id,
                    reason=decision.reason,
                )
                return None

            request = await self.requests.get_request(request_id)
            if request is None:
                return None

            await self._emit(
                EventType.RESULT_ACCEPTED,
                request_id=request_id,
                generation_id=generation_id,
                payload={"task_id": result.task_id, "tool_name": result.tool_name},
            )

            if not result.ok:
                await self.store.set_status(ConversationStatus.ERROR)
                await self._emit(
                    EventType.ERROR,
                    request_id=request_id,
                    generation_id=generation_id,
                    payload={"error": "tool_failure", "detail": result.error},
                )
                await self.requests.mark_completed(request_id)
                await self._emit_state()
                raise ToolExecutionError(result.error or "tool failed")

            text = self._format_assistant_response(result, request.parameters)
            await self.requests.mark_completed(request_id)

        await self._deliver_response(text, request)
        return result

    async def cancel_current_request(self) -> None:
        async with self._lock:
            await self._cancel_current_unlocked()

    async def _cancel_current_unlocked(self) -> None:
        current = await self.requests.get_current_request()
        if current is None:
            return

        await self.store.set_status(ConversationStatus.CANCELLING)
        cancelled = await self.requests.mark_cancelled(current.request_id)
        await self._emit(
            EventType.REQUEST_INVALIDATED,
            request_id=cancelled.request_id,
            generation_id=cancelled.generation_id,
            payload={"reason": "cancelled"},
        )
        log_operation(
            logger,
            "request_invalidated",
            conversation_id=self.conversation_id,
            request_id=cancelled.request_id,
            generation_id=cancelled.generation_id,
            reason="cancelled",
        )
        self.latencies.mark("cancel_requested")
        task_ids = await self.tasks.cancel_for_request(current.request_id, timeout=self.cancel_timeout)
        self.latencies.mark("cancel_attempted")
        self.latencies.measure("cancellation_latency_ms", "cancel_requested", "cancel_attempted")
        for task_id in task_ids:
            await self._emit(
                EventType.TASK_CANCELLED,
                request_id=current.request_id,
                generation_id=current.generation_id,
                payload={"task_id": task_id},
            )
            log_operation(
                logger,
                "task_cancelled",
                conversation_id=self.conversation_id,
                request_id=current.request_id,
                generation_id=current.generation_id,
                task_id=task_id,
            )
            await self._drop_task(task_id)
        await self.store.apply(lambda state: setattr(state, "current_request_id", None))
        await self.store.apply(lambda state: setattr(state, "generation_id", None))

    async def _deliver_response(self, text: str, request: Request) -> None:
        decision = await self.validator.validate(request.request_id, request.generation_id)
        if decision.discarded:
            await self._emit(
                EventType.STALE_RESULT_DISCARDED,
                request_id=request.request_id,
                generation_id=request.generation_id,
                payload={"reason": "stale_response", "text": text},
            )
            return

        await self.store.apply(
            lambda state: (
                setattr(state, "last_assistant_message", text),
                setattr(state.timestamps, "last_assistant_at", utcnow()),
            )
        )
        await self._emit(
            EventType.ASSISTANT_RESPONSE_READY,
            request_id=request.request_id,
            generation_id=request.generation_id,
            payload={"text": text},
        )
        log_operation(
            logger,
            "final_response_delivered",
            conversation_id=self.conversation_id,
            request_id=request.request_id,
            generation_id=request.generation_id,
        )
        self.latencies.mark("valid_response")
        if "user_turn" in self.latencies._marks:
            self.latencies.measure("time_to_valid_response_ms", "user_turn", "valid_response")
        await self._speak(text, request, kind="final")
        still_current = await self.validator.validate(request.request_id, request.generation_id)
        if still_current.accepted:
            await self.store.set_status(ConversationStatus.COMPLETED)
            await self._emit_state()

    async def _speak(self, text: str, request: Request, kind: str) -> None:
        decision = await self.validator.validate(request.request_id, request.generation_id)
        if decision.discarded:
            return
        await self.store.set_status(ConversationStatus.SPEAKING)
        await self._emit(
            EventType.TTS_START,
            request_id=request.request_id,
            generation_id=request.generation_id,
            payload={"text": text, "kind": kind},
        )
        log_operation(
            logger,
            "tts_started",
            conversation_id=self.conversation_id,
            request_id=request.request_id,
            generation_id=request.generation_id,
            kind=kind,
        )
        try:
            await self.tts.speak(
                text,
                request_id=request.request_id,
                generation_id=request.generation_id,
                conversation_id=self.conversation_id,
            )
        except Exception as exc:
            await self._emit(
                EventType.ERROR,
                request_id=request.request_id,
                generation_id=request.generation_id,
                payload={"error": "tts_failure", "detail": str(exc)},
            )

    async def _emit(
        self,
        event_type: EventType,
        *,
        request_id: str | None = None,
        generation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AppEvent:
        event = AppEvent(
            conversation_id=self.conversation_id,
            request_id=request_id,
            generation_id=generation_id,
            event_type=event_type,
            payload=payload or {},
        )
        await self.events.emit(event)
        return event

    async def _emit_state(self) -> None:
        state = await self.store.snapshot()
        await self._emit(
            EventType.STATE_UPDATED,
            request_id=state.current_request_id,
            generation_id=state.generation_id,
            payload={"state": state.to_json_dict()},
        )

    async def _drop_task(self, task_id: str) -> None:
        await self.store.apply(
            lambda state: setattr(
                state,
                "active_task_ids",
                [item for item in state.active_task_ids if item != task_id],
            )
        )

    def _tool_ignores_cancel(self, tool: BaseTool) -> bool:
        checker = getattr(tool, "will_ignore_cancellation", None)
        if callable(checker):
            return bool(checker())
        return bool(getattr(tool, "ignore_cancellation", False))

    def _bridging_text(self, parameters: dict[str, Any]) -> str:
        city = parameters.get("city") or "your area"
        budget = parameters.get("budget_max")
        if budget:
            return f"Let me search for hotels in {city} under {budget}."
        return f"Let me search for hotels in {city}."

    def _format_assistant_response(self, result: ToolResult, parameters: dict[str, Any]) -> str:
        data = result.data or {}
        hotels = data.get("hotels") or []
        city = parameters.get("city") or data.get("city") or "that city"
        budget = parameters.get("budget_max")
        if not hotels:
            extra = f" under {budget}" if budget else ""
            return f"I could not find hotels in {city}{extra}."
        intro = f"I found {len(hotels)} hotels in {city}"
        if budget:
            intro += f" under {budget}"
        details = "; ".join(
            f"{hotel['name']} at {hotel['price_inr']}" for hotel in hotels[:4]
        )
        return f"{intro}. {details}."

    def remember_event(self, event_id: str) -> bool:
        """Return True if this external event id is new."""

        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        if len(self._seen_event_ids) > 256:
            self._seen_event_ids = set(list(self._seen_event_ids)[-128:])
        return True
