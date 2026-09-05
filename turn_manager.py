"""
turn_manager.py

Tracks conversational "turns" with a monotonically increasing turn id.
Any async work (LLM generation, tool calls, queued TTS) is stamped with
the turn id active when it started. Before that work is allowed to reach
the user, it must be checked against the *current* turn id — if the turn
has moved on, the result is discarded, not surfaced.

Core design principle: the staleness check happens right before a result
is used/spoken, not just once when the work started. Cancellation
(task.cancel()) is best-effort and can fail to land in time — the
point-of-use check is what actually guarantees correctness.
"""

import time
import asyncio


class StaleResultError(Exception):
    """Raised when a result is discarded because its turn is no longer current."""
    pass


class TurnManager:
    def __init__(self):
        self.current_turn_id = 0
        self._log = []
        # Tracks only the currently-running TOOL task (e.g. a slow order
        # lookup), not the broader LLM generation task. Cancelling the whole
        # generation task turned out to be too broad — it also disrupted the
        # framework's own bookkeeping for generating a fresh reply to the
        # NEW turn, causing it to fall back to a generic "I don't know"
        # instead of actually answering. Cancelling only the narrow tool
        # task avoids that side effect while still stopping a slow lookup
        # dead rather than letting it run to completion.
        self.active_tool_task = None

    def start_new_turn(self) -> int:
        """Call this whenever a new user utterance is committed, or on interrupt."""
        self.current_turn_id += 1
        self._log_event("new_turn", self.current_turn_id)
        return self.current_turn_id

    def stamp(self) -> int:
        """Call this the instant a piece of async work starts. Returns the
        turn id active right now, to be checked again later."""
        return self.current_turn_id

    def is_stale(self, stamped_id: int) -> bool:
        """The critical check — call this again right before speaking/using
        a result, not just once when the work started."""
        stale = stamped_id != self.current_turn_id
        print(f"[TURN_MANAGER] is_stale check: stamped={stamped_id}, current={self.current_turn_id}, stale={stale}")
        if stale:
            self._log_event("discarded_stale", stamped_id)
        return stale

    async def guard(self, coro, stamped_id: int):
        """Wrap any async call; raises StaleResultError if the turn moved on
        by the time the result comes back."""
        result = await coro
        if self.is_stale(stamped_id):
            raise StaleResultError(
                f"Result for turn {stamped_id} is stale (current: {self.current_turn_id})"
            )
        return result

    async def cancel_and_fence(self, task: asyncio.Task, stamped_id: int):
        """Best-effort cancellation + staleness fallback. Cancellation is not
        trusted alone — is_stale() at point-of-use is what guarantees
        correctness even if the task finishes despite being cancelled."""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            self._log_event("task_cancelled", stamped_id)
            return None
        except Exception as e:
            self._log_event("task_error_during_cancel", stamped_id, str(e))
            return None

        # Task finished despite cancel() being called — fence check catches it here.
        if self.is_stale(stamped_id):
            return None
        return task.result()

    def cancel_active_tool_task(self):
        """Called on interrupt, in addition to start_new_turn(). Cancels
        only the currently in-flight TOOL task (e.g. a slow order lookup's
        asyncio.sleep), not the broader generation task the framework
        itself depends on. This closes the gap where a tool-call
        continuation, invoked fresh AFTER the turn has already advanced,
        would otherwise get re-stamped with the new current turn id and
        slip past is_stale() undetected — without the side effect of
        disrupting the framework's own reply-generation orchestration for
        the NEW turn."""
        if self.active_tool_task is not None and not self.active_tool_task.done():
            self.active_tool_task.cancel()
            self._log_event("active_tool_task_cancelled_on_interrupt", self.current_turn_id)

    def _log_event(self, event: str, turn_id: int, detail: str = ""):
        self._log.append({
            "event": event,
            "turn_id": turn_id,
            "timestamp": time.time(),
            "detail": detail,
        })

    def audit_log(self):
        """Returns the full event log — used as reproducibility evidence
        for RIME_EVIDENCE.md."""
        return list(self._log)
