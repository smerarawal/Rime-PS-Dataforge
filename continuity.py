"""
continuity.py

Direction 3: conversation continuity during tool work.

The problem: the agent calls a slow backend (order lookup, booking, records
fetch) and the user talks during it. Treating every mid-tool utterance as a
brand-new turn is wrong in both directions —

* "are you still there?" or "make it the express one" should NOT throw away a
  lookup that is 3 seconds into a 4-second call, and
* "actually, never mind, what's your return policy?" MUST throw it away, and
  must also stop the result from being narrated later.

So a mid-tool utterance is routed by intent before the turn machinery decides
whether the in-flight work survives:

    STATUS      "are you still there", "any luck"      -> answer from the work
                                                          registry, keep work
    CONSTRAINT  "make it express", "the other order"   -> attach, keep work
    CANCEL      "never mind", "forget it", "stop"      -> cancel + fence work
    AFFIRMATION "ok", "sure", "mhm"                    -> backchannel, ignore
    NEW_REQUEST anything else                          -> supersede + fence

Work that survives is *carried forward*: its turn stamp is advanced to the new
turn id so the point-of-use staleness check still passes when the result
arrives. Carrying forward is the deliberate, narrow exception to "any work
stamped with an old turn is stale" — it is only ever applied to work the user
explicitly asked to keep, and it is recorded in the audit log so it can never
quietly become the default path.

Classification is pattern-based, not model-based, on purpose: it runs inside
the interrupt path where an extra LLM round-trip would cost more latency than
the whole feature saves. Low-confidence matches fall through to NEW_REQUEST,
which is the safe default — the worst case is that a surviving lookup is
discarded and re-run, never that a stale result is spoken.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    NEW_REQUEST = "new_request"
    STATUS_REQUEST = "status_request"
    CONSTRAINT = "constraint"
    CANCEL = "cancel"
    AFFIRMATION = "affirmation"


# Ordered by precedence: CANCEL is checked before everything else, because
# mistaking "stop, forget the order — what's your return policy" for a
# constraint would keep work the user explicitly killed.
_CANCEL_PATTERNS = (
    r"\bnever\s?mind\b",
    r"\bforget (it|that|about it)\b",
    r"\bcancel (that|it|the (lookup|order check|search))\b",
    r"\bstop\b",
    r"\bdon'?t bother\b",
    r"\bskip (it|that)\b",
    r"\bleave it\b",
)

_STATUS_PATTERNS = (
    r"\bare you (still )?(there|working on it)\b",
    r"\b(any|what'?s the) (luck|progress|status|update)\b",
    r"\bhow('?s| is) (it|that) going\b",
    r"\bhow much longer\b",
    r"\bstill (checking|looking|there|working)\b",
    r"\bwhat'?s taking so long\b",
    r"\bhello\??$",
    r"\byou there\b",
    r"\bdid you (get|find) (it|that|anything)\b",
)

# Constraint = a refinement of work already under way. These must be
# modification-shaped, and only count while work is pending.
#
# Deliberately NOT included: a bare discourse marker like "actually", "also"
# or "and". Those open a modification ("actually make it express") just as
# readily as they open a completely new question ("actually, what's your
# return policy?"), so matching on them alone keeps work the user has
# abandoned — the exact failure this whole layer exists to prevent. The marker
# has to be followed by something that names the work.
_CONSTRAINT_PATTERNS = (
    r"\bmake (it|that|them)\b",
    r"\buse (the )?[\w-]+ (one|instead)\b",
    r"\b[\w-]+ (one )?instead\b",
    r"\bthe other (one|order|address|card)\b",
    r"\b(can|could) you also\b",
    r"\balso (check|look|see|add|include)\b",
    r"\bwhile you'?re (at it|there)\b",
    r"\b(order|tracking)\s*(number|#)?\s*\d{3,}\b",
    r"\bi meant\b",
    r"\bnot that one\b",
)

_AFFIRMATION_PATTERNS = (
    r"^(ok(ay)?|sure|yeah|yep|yes|right|mhm+|uh huh|got it|thanks|thank you|cool|alright)[.!]?$",
)


def _matches(patterns, text: str) -> Optional[str]:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return None


class Classification:
    def __init__(self, intent: Intent, matched: Optional[str] = None, text: str = ""):
        self.intent = intent
        self.matched = matched
        self.text = text

    def __repr__(self) -> str:
        return f"Classification({self.intent.value}, matched={self.matched!r})"

    @property
    def keeps_work(self) -> bool:
        """Whether in-flight tool work survives this utterance."""
        return self.intent in (Intent.STATUS_REQUEST, Intent.CONSTRAINT, Intent.AFFIRMATION)


class UtteranceRouter:
    """Classifies a user utterance in the context of whether tool work is in
    flight. With nothing pending, only AFFIRMATION and CANCEL remain
    meaningful — a status question about nothing, or a constraint on nothing,
    is just a new request."""

    def classify(self, text: str, work_pending: bool) -> Classification:
        raw = (text or "").strip()
        if not raw:
            return Classification(Intent.AFFIRMATION, "empty", raw)

        m = _matches(_AFFIRMATION_PATTERNS, raw)
        if m:
            return Classification(Intent.AFFIRMATION, m, raw)

        m = _matches(_CANCEL_PATTERNS, raw)
        if m:
            # With nothing running there is nothing to cancel, but the user
            # still said something — treat it as a new request so the agent
            # responds rather than silently swallowing the utterance.
            return Classification(Intent.CANCEL if work_pending else Intent.NEW_REQUEST, m, raw)

        if work_pending:
            m = _matches(_STATUS_PATTERNS, raw)
            if m:
                return Classification(Intent.STATUS_REQUEST, m, raw)

            m = _matches(_CONSTRAINT_PATTERNS, raw)
            if m:
                return Classification(Intent.CONSTRAINT, m, raw)

        return Classification(Intent.NEW_REQUEST, None, raw)


class PendingWork:
    """One in-flight tool call, described well enough to talk about it out
    loud without waiting for it to finish."""

    def __init__(self, work_id: int, turn_id: int, description: str, task=None, clock=time.perf_counter):
        self.work_id = work_id
        self.turn_id = turn_id
        self.description = description
        self.task = task
        self.constraints: list = []
        self.started_at = clock()
        self.done_at: Optional[float] = None
        self.cancelled = False
        self._clock = clock

    def elapsed_s(self) -> float:
        end = self.done_at if self.done_at is not None else self._clock()
        return end - self.started_at

    def is_running(self) -> bool:
        if self.done_at is not None or self.cancelled:
            return False
        if self.task is not None:
            return not self.task.done()
        return True

    def as_dict(self) -> dict:
        return {
            "work_id": self.work_id,
            "turn_id": self.turn_id,
            "description": self.description,
            "constraints": list(self.constraints),
            "elapsed_s": round(self.elapsed_s(), 3),
            "running": self.is_running(),
            "cancelled": self.cancelled,
        }


def status_sentence(work: PendingWork) -> str:
    """A spoken status line. Says what is happening and roughly how long it
    has been — a bare "still working on it" is what makes users repeat
    themselves, which is how a turn ends up superseded for no reason."""
    secs = work.elapsed_s()
    if secs < 2:
        how_long = "just started"
    elif secs < 10:
        how_long = f"about {int(round(secs))} seconds in"
    else:
        how_long = f"{int(round(secs))} seconds in so far"
    tail = ""
    if work.constraints:
        tail = f" I've got your note about {work.constraints[-1]}."
    return f"Still {work.description} — {how_long}.{tail}"


def cancel_sentence(work: PendingWork) -> str:
    return f"Okay, I've stopped {work.description}. What would you like instead?"


def constraint_sentence(work: PendingWork, constraint: str) -> str:
    return f"Got it — {constraint}. Still {work.description}."
