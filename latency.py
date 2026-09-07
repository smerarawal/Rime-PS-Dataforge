"""
latency.py

Direction 1: perceived response time.

Measures the delay the user actually experiences — from the moment they stop
speaking to the moment the first Rime audio frame is handed to playback — and
breaks that interval into the stages that produce it, so a regression can be
attributed to a stage rather than guessed at.

Two pieces live here:

* ``LatencyTracker`` — per-turn stage timeline plus p50/p95 aggregation.
  Instrumentation points are named after the pipeline stage that *completes*
  at that instant, so a stage's cost is the gap between consecutive marks.

* ``GapCover`` — reduces *perceived* delay when actual delay cannot be
  reduced further. If nothing audible has started by ``threshold_ms``, it
  speaks one short acknowledgement so the user hears the agent engage rather
  than silence, and it stands down the instant real audio starts or the turn
  goes stale. This is a perception fix and is measured separately from TTFA:
  ``ttfa_ms`` always reports the first real audio, and
  ``time_to_any_audio_ms`` reports what the user actually heard first.

Timing uses ``time.perf_counter()`` for intervals (monotonic, not affected by
wall-clock adjustment) and ``time.time()`` only for correlating with the
event log in metrics.py.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from typing import Awaitable, Callable, Iterable, Optional


# Ordered pipeline stages. The turn "starts" at USER_SPEECH_END: everything
# before that is the user still talking, and counting it would flatter the
# numbers.
USER_SPEECH_END = "user_speech_end"
STT_FINAL = "stt_final"
LLM_REQUEST = "llm_request"
LLM_FIRST_TOKEN = "llm_first_token"
TTS_REQUEST = "tts_request"
TTS_FIRST_BYTE = "tts_first_byte"
FIRST_AUDIO_FRAME = "first_audio_frame"

STAGE_ORDER = (
    USER_SPEECH_END,
    STT_FINAL,
    LLM_REQUEST,
    LLM_FIRST_TOKEN,
    TTS_REQUEST,
    TTS_FIRST_BYTE,
    FIRST_AUDIO_FRAME,
)

# Human-readable name for the interval ending at each stage.
STAGE_INTERVAL_NAMES = {
    STT_FINAL: "endpointing + STT finalization",
    LLM_REQUEST: "turn bookkeeping before LLM dispatch",
    LLM_FIRST_TOKEN: "LLM time-to-first-token",
    TTS_REQUEST: "first sentence buffered for TTS",
    TTS_FIRST_BYTE: "Rime time-to-first-byte",
    FIRST_AUDIO_FRAME: "decode + handoff to playback",
}


def _percentile(sorted_values, pct: float):
    """Nearest-rank percentile. Stated explicitly rather than interpolated so
    small-N results are reported honestly."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


def _stats(values: Iterable[float]) -> Optional[dict]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {
        "n": len(values),
        "min_ms": round(values[0], 1),
        "p50_ms": round(statistics.median(values), 1),
        "p95_ms": round(_percentile(values, 0.95), 1),
        "max_ms": round(values[-1], 1),
        "mean_ms": round(statistics.fmean(values), 1),
    }


class TurnTimeline:
    """Stage marks for one turn. First mark for a stage wins — a stage can be
    hit repeatedly (every LLM chunk passes the same code path) and only the
    first occurrence is the latency-relevant one."""

    def __init__(self, turn_id: int, started_at: float, wall_started_at: float):
        self.turn_id = turn_id
        self.started_at = started_at
        self.wall_started_at = wall_started_at
        self.marks: dict[str, float] = {USER_SPEECH_END: started_at}
        # Set when GapCover spoke a filler for this turn, so perceived and
        # real time-to-audio stay distinguishable.
        self.filler_at: Optional[float] = None
        self.filler_text: Optional[str] = None
        self.aborted: bool = False

    def mark(self, stage: str, at: float) -> Optional[float]:
        if stage in self.marks:
            return None
        self.marks[stage] = at
        return round((at - self.started_at) * 1000, 1)

    def offset_ms(self, stage: str) -> Optional[float]:
        at = self.marks.get(stage)
        if at is None:
            return None
        return round((at - self.started_at) * 1000, 1)

    def ttfa_ms(self) -> Optional[float]:
        return self.offset_ms(FIRST_AUDIO_FRAME)

    def time_to_any_audio_ms(self) -> Optional[float]:
        """What the user actually perceived: the filler, if one was spoken,
        otherwise the first real audio frame."""
        candidates = [self.ttfa_ms()]
        if self.filler_at is not None:
            candidates.append(round((self.filler_at - self.started_at) * 1000, 1))
        candidates = [c for c in candidates if c is not None]
        return min(candidates) if candidates else None

    def breakdown(self) -> list:
        """Cost of each stage, as the gap from the previous stage that was
        actually recorded. Stages that never fired are skipped rather than
        silently folded into their neighbour."""
        out = []
        prev_stage = USER_SPEECH_END
        prev_at = self.started_at
        for stage in STAGE_ORDER[1:]:
            at = self.marks.get(stage)
            if at is None:
                continue
            out.append({
                "stage": stage,
                "from": prev_stage,
                "what": STAGE_INTERVAL_NAMES.get(stage, stage),
                "ms": round((at - prev_at) * 1000, 1),
                "cumulative_ms": round((at - self.started_at) * 1000, 1),
            })
            prev_stage, prev_at = stage, at
        return out

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "wall_started_at": self.wall_started_at,
            "aborted": self.aborted,
            "ttfa_ms": self.ttfa_ms(),
            "time_to_any_audio_ms": self.time_to_any_audio_ms(),
            "filler_spoken": self.filler_text,
            "breakdown": self.breakdown(),
        }


class LatencyTracker:
    """Collects TurnTimelines and aggregates them.

    ``budget_ms`` is the TTFA target this project holds itself to; turns over
    budget are counted separately so a good p50 cannot hide a bad tail.
    """

    def __init__(self, budget_ms: float = 1200.0, clock: Callable[[], float] = time.perf_counter):
        self.budget_ms = budget_ms
        self._clock = clock
        self._turns: dict = {}
        self._order: list = []

    # --- recording -------------------------------------------------------

    def begin_turn(self, turn_id: int, at: Optional[float] = None) -> TurnTimeline:
        """Call the instant the user's speech ends (VAD end-of-speech), not
        when the transcript arrives — endpointing delay is part of what the
        user perceives and must be inside the measured window."""
        tl = TurnTimeline(turn_id, at if at is not None else self._clock(), time.time())
        # A turn can be re-begun if the user's speech ends more than once
        # before a transcript commits (a false endpoint, then the real one).
        # The latest speech-end is the correct start; keep one entry per turn
        # so it isn't double-counted in the percentiles.
        if turn_id not in self._turns:
            self._order.append(turn_id)
        self._turns[turn_id] = tl
        return tl

    def mark(self, turn_id: int, stage: str, at: Optional[float] = None) -> Optional[float]:
        """Record a stage completion. Returns ms-since-turn-start for the
        first mark of that stage, or None if already marked / turn unknown.

        A turn that was never begun is ignored rather than invented: marks can
        arrive for the greeting or for framework-internal speech that has no
        preceding user utterance, and fabricating a start time for those would
        put meaningless numbers into the percentiles.
        """
        tl = self._turns.get(turn_id)
        if tl is None:
            return None
        return tl.mark(stage, at if at is not None else self._clock())

    def note_filler(self, turn_id: int, text: str, at: Optional[float] = None) -> None:
        tl = self._turns.get(turn_id)
        if tl is None or tl.filler_at is not None:
            return
        tl.filler_at = at if at is not None else self._clock()
        tl.filler_text = text

    def mark_aborted(self, turn_id: int) -> None:
        """A turn the user interrupted before audio started. It has no
        meaningful TTFA and is excluded from the percentiles."""
        tl = self._turns.get(turn_id)
        if tl is not None and tl.ttfa_ms() is None:
            tl.aborted = True

    # --- reading ---------------------------------------------------------

    def timeline(self, turn_id: int) -> Optional[TurnTimeline]:
        return self._turns.get(turn_id)

    def completed_turns(self) -> list:
        return [self._turns[t] for t in self._order
                if not self._turns[t].aborted and self._turns[t].ttfa_ms() is not None]

    def summary(self) -> dict:
        done = self.completed_turns()
        ttfa = [t.ttfa_ms() for t in done]
        perceived = [t.time_to_any_audio_ms() for t in done]

        stage_stats = {}
        for stage in STAGE_ORDER[1:]:
            gaps = []
            for t in done:
                for row in t.breakdown():
                    if row["stage"] == stage:
                        gaps.append(row["ms"])
            s = _stats(gaps)
            if s:
                stage_stats[stage] = {"what": STAGE_INTERVAL_NAMES.get(stage, stage), **s}

        over = [t.turn_id for t in done if t.ttfa_ms() > self.budget_ms]
        return {
            "turns_measured": len(done),
            "turns_aborted_before_audio": sum(1 for t in self._turns.values() if t.aborted),
            "budget_ms": self.budget_ms,
            "over_budget_turns": over,
            "over_budget_rate": round(len(over) / len(done), 3) if done else None,
            "ttfa": _stats(ttfa),
            "perceived_time_to_audio": _stats(perceived),
            "stages": stage_stats,
        }

    def slowest_stage(self) -> Optional[str]:
        """Which stage to attack first — the one with the largest p50."""
        stages = self.summary()["stages"]
        if not stages:
            return None
        return max(stages.items(), key=lambda kv: kv[1]["p50_ms"])[0]

    def export_json(self, path: str = "latency_results.json") -> str:
        payload = {
            "summary": self.summary(),
            "turns": [self._turns[t].as_dict() for t in self._order],
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        return path

    def reset(self) -> None:
        self._turns.clear()
        self._order.clear()


class FillerPolicy:
    """Decides whether a gap should be covered by a short spoken phrase.

    Deliberately conservative: filler that fires too eagerly makes the agent
    sound nervous and *adds* to the time before the real answer, so it only
    fires past a threshold, and never twice in quick succession.
    """

    DEFAULT_PHRASES = (
        "One moment.",
        "Let me check.",
        "Sure — one second.",
    )

    def __init__(
        self,
        threshold_ms: float = 700.0,
        min_gap_s: float = 10.0,
        phrases: Iterable[str] = DEFAULT_PHRASES,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.threshold_ms = threshold_ms
        self.min_gap_s = min_gap_s
        self.phrases = tuple(phrases)
        self._clock = clock
        self._last_used_at: Optional[float] = None
        self._idx = 0

    def should_fill(self, elapsed_ms: float, audio_started: bool) -> bool:
        if audio_started or elapsed_ms < self.threshold_ms:
            return False
        if self._last_used_at is not None and (self._clock() - self._last_used_at) < self.min_gap_s:
            return False
        return True

    def take_phrase(self) -> str:
        """Rotates so repeated gaps in one session don't repeat one phrase."""
        phrase = self.phrases[self._idx % len(self.phrases)]
        self._idx += 1
        self._last_used_at = self._clock()
        return phrase


class GapCover:
    """Runs alongside a turn: waits ``threshold_ms``; if no audio has started
    and the turn is still current, speaks one filler phrase.

    ``speak`` is awaited, ``is_stale`` is polled right before speaking so a
    filler is never emitted into a turn the user has already moved past —
    the same point-of-use rule the rest of the project uses.
    """

    def __init__(
        self,
        policy: FillerPolicy,
        speak: Callable[[str], Awaitable],
        tracker: Optional[LatencyTracker] = None,
    ):
        self.policy = policy
        self._speak = speak
        self._tracker = tracker

    async def run(
        self,
        turn_id: int,
        audio_started: asyncio.Event,
        is_stale: Callable[[], bool],
    ) -> Optional[str]:
        """Returns the phrase spoken, or None if the gap closed on its own."""
        try:
            await asyncio.wait_for(audio_started.wait(), timeout=self.policy.threshold_ms / 1000)
            return None  # real audio beat the threshold — nothing to cover
        except asyncio.TimeoutError:
            pass

        if audio_started.is_set() or is_stale():
            return None
        if not self.policy.should_fill(self.policy.threshold_ms, audio_started.is_set()):
            return None

        phrase = self.policy.take_phrase()
        # Re-check at the actual point of use: the turn may have advanced
        # while we were deciding.
        if is_stale() or audio_started.is_set():
            return None
        await self._speak(phrase)
        if self._tracker is not None:
            self._tracker.note_filler(turn_id, phrase)
        return phrase
