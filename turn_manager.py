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
