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

This module also owns the in-flight *work registry* that Direction 3
(continuity during tool work) is built on. A tool call registers itself as
PendingWork; a mid-tool user utterance is routed through
``route_utterance()``, which decides — before any turn bookkeeping happens —
whether that work should be superseded, kept, or cancelled. Work that is kept
is carried forward onto the new turn id, which is the single, audited
exception to "an old stamp means stale".
"""

import asyncio
import time
from typing import Optional

from continuity import (
    Intent,
    PendingWork,
    UtteranceRouter,
    cancel_sentence,
    constraint_sentence,
    status_sentence,
)


class StaleResultError(Exception):
    """Raised when a result is discarded because its turn is no longer current."""
    pass


class TurnDecision:
    """What ``route_utterance`` decided to do with a user utterance.

    ``reply`` is a sentence the caller should speak immediately (status,
    cancellation acknowledgement, constraint acknowledgement) — it exists so
    the session stays audibly responsive during tool work without waiting on
    the LLM. ``superseded`` says whether the turn id advanced, i.e. whether
    everything stamped with the old turn is now stale.
    """

    def __init__(
        self,
        intent: Intent,
        superseded: bool,
        turn_id: int,
        reply: Optional[str] = None,
        work_kept: bool = False,
        needs_llm: bool = True,
    ):
        self.intent = intent
        self.superseded = superseded
        self.turn_id = turn_id
        self.reply = reply
        self.work_kept = work_kept
        # False when the decision is fully handled by ``reply`` and dispatching
        # the LLM would only produce a redundant second answer.
        self.needs_llm = needs_llm

    def __repr__(self) -> str:
        return (
            f"TurnDecision({self.intent.value}, superseded={self.superseded}, "
            f"turn_id={self.turn_id}, work_kept={self.work_kept}, reply={self.reply!r})"
        )


class TurnManager:
    def __init__(self, router: Optional[UtteranceRouter] = None, clock=time.perf_counter):
        self.current_turn_id = 0
        self._log = []
        self._clock = clock
        self._router = router or UtteranceRouter()

        # Tracks only the currently-running TOOL task (e.g. a slow order
        # lookup), not the broader LLM generation task. Cancelling the whole
        # generation task turned out to be too broad — it also disrupted the
        # framework's own bookkeeping for generating a fresh reply to the
        # NEW turn, causing it to fall back to a generic "I don't know"
        # instead of actually answering. Cancelling only the narrow tool
        # task avoids that side effect while still stopping a slow lookup
        # dead rather than letting it run to completion.
        self.active_tool_task = None

        # In-flight tool work, keyed by id. Kept as a registry rather than a
        # single slot so a status question can describe *what* is running.
        self._work: dict = {}
        self._next_work_id = 1

    # ------------------------------------------------------------------
    # turn identity
    # ------------------------------------------------------------------

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
        cancelled_any = False
        for work in list(self._work.values()):
            if work.task is not None and not work.task.done():
                work.task.cancel()
                work.cancelled = True
                cancelled_any = True
        if self.active_tool_task is not None and not self.active_tool_task.done():
            self.active_tool_task.cancel()
            cancelled_any = True
        if cancelled_any:
            self._log_event("active_tool_task_cancelled_on_interrupt", self.current_turn_id)

    # ------------------------------------------------------------------
    # in-flight work registry (Direction 3)
    # ------------------------------------------------------------------

    def register_work(self, description: str, task=None) -> PendingWork:
        """Register a tool call as in-flight. ``description`` is spoken aloud
        verbatim in status replies, so phrase it as a continuation of "Still
        ..." — e.g. "checking order 1002"."""
        work = PendingWork(self._next_work_id, self.current_turn_id, description, task, clock=self._clock)
        self._work[work.work_id] = work
        self._next_work_id += 1
        if task is not None:
            self.active_tool_task = task
        self._log_event("work_registered", work.turn_id, description)
        return work

    def complete_work(self, work_id: int) -> None:
        work = self._work.get(work_id)
        if work is not None:
            work.done_at = self._clock()
            self._log_event("work_completed", work.turn_id, work.description)
            self._work.pop(work_id, None)
            if work.task is not None and work.task is self.active_tool_task:
                self.active_tool_task = None

    def pending_work(self) -> Optional[PendingWork]:
        """The most recently started work that is still running."""
        running = [w for w in self._work.values() if w.is_running()]
        if not running:
            return None
        return max(running, key=lambda w: w.started_at)

    def has_pending_work(self) -> bool:
        return self.pending_work() is not None

    def carry_forward(self, work: PendingWork) -> int:
        """Advance a piece of in-flight work onto the current turn so its
        point-of-use staleness check still passes.

        This is the ONE sanctioned way for work to survive a turn boundary. It
        is only ever called for utterances the router classified as keeping
        work (status / constraint / backchannel), and every use is written to
        the audit log — if carried-forward work ever gets spoken out of
        context, the log says exactly which utterance authorized it.
        """
        old = work.turn_id
        work.turn_id = self.current_turn_id
        self._log_event("work_carried_forward", self.current_turn_id, f"from_turn={old}: {work.description}")
        return work.turn_id

    def add_constraint(self, work: PendingWork, constraint: str) -> None:
        work.constraints.append(constraint)
        self._log_event("constraint_added", work.turn_id, constraint)

    def cancel_work(self, work: PendingWork, reason: str = "user_cancelled") -> None:
        work.cancelled = True
        work.done_at = self._clock()
        if work.task is not None and not work.task.done():
            work.task.cancel()
        self._work.pop(work.work_id, None)
        if work.task is not None and work.task is self.active_tool_task:
            self.active_tool_task = None
        self._log_event("work_cancelled", work.turn_id, reason)

    def is_work_stale(self, work: PendingWork) -> bool:
        """Point-of-use check for tool work. Reads ``work.turn_id`` live, so
        work that was carried forward passes and work that was superseded does
        not — the caller must not cache the stamp."""
        if work.cancelled:
            return True
        return self.is_stale(work.turn_id)

    # ------------------------------------------------------------------
    # utterance routing (Direction 3)
    # ------------------------------------------------------------------

    def route_utterance(self, text: str) -> TurnDecision:
        """Decide what a user utterance means for in-flight work, then apply
        the turn bookkeeping that decision implies.

        Every branch advances the turn id. That is deliberate: the utterance
        is a real user turn either way, LiveKit will have stopped playback for
        it, and stale *LLM generation* from the previous turn must always be
        fenced. What differs between branches is whether in-flight tool work
        is carried forward onto the new turn or left behind as stale.
        """
        work = self.pending_work()
        classification = self._router.classify(text, work_pending=work is not None)
        intent = classification.intent

        previous_turn = self.current_turn_id
        new_turn = self.start_new_turn()
        self._log_event("utterance_routed", new_turn, f"{intent.value}: {text!r}")

        if work is None:
            # Nothing in flight; an ordinary turn.
            return TurnDecision(intent, superseded=True, turn_id=new_turn)

        if intent is Intent.CANCEL:
            self.cancel_work(work, reason="user_cancelled")
            return TurnDecision(
                intent, superseded=True, turn_id=new_turn,
                reply=cancel_sentence(work), work_kept=False, needs_llm=False,
            )

        if intent is Intent.STATUS_REQUEST:
            self.carry_forward(work)
            return TurnDecision(
                intent, superseded=False, turn_id=new_turn,
                reply=status_sentence(work), work_kept=True, needs_llm=False,
            )

        if intent is Intent.CONSTRAINT:
            self.add_constraint(work, classification.text)
            self.carry_forward(work)
            return TurnDecision(
                intent, superseded=False, turn_id=new_turn,
                reply=constraint_sentence(work, classification.text),
                work_kept=True, needs_llm=False,
            )

        if intent is Intent.AFFIRMATION:
            # A backchannel ("mhm", "ok") is not a request for anything. Keep
            # the work and say nothing — answering it would talk over the user
            # for no reason.
            self.carry_forward(work)
            return TurnDecision(
                intent, superseded=False, turn_id=new_turn,
                reply=None, work_kept=True, needs_llm=False,
            )

        # NEW_REQUEST: the user moved on. Kill the work and fence its result.
        self.cancel_work(work, reason="superseded_by_new_request")
        self._log_event("work_superseded", previous_turn, work.description)
        return TurnDecision(intent, superseded=True, turn_id=new_turn, work_kept=False)

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

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
