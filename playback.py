"""
playback.py

Direction 2, second half: "keep application state consistent with what the
user actually heard."

Stopping audio is not enough. If the agent generated three sentences, the
user heard one and a half, and the conversation history still records all
three, then every later turn is reasoning against a transcript the user never
received — the agent will say "as I mentioned, your refund takes 5-7 days"
about a sentence that was cut off mid-word. That divergence is silent and
compounds.

``PlaybackLedger`` tracks, per utterance:

* the text handed to TTS (``note_synthesized``), and
* the audio actually pushed toward playback (``note_frame`` /
  ``note_played_seconds``).

On interruption, ``heard_text()`` truncates the synthesized text to the
fraction of audio that actually made it out, snapped back to a word boundary,
so the history can be rewritten to what the user plausibly heard.

Honesty about what this is: an *estimate*. The exact cut point depends on
client-side jitter buffers we cannot observe from the agent process, and text
does not map to audio duration uniformly (a long word takes longer than a
short one). It is deliberately conservative — it rounds down, preferring to
record slightly less than the user heard rather than more, because claiming
the user heard something they did not is the failure mode that actually
corrupts the conversation. ``truncation_confidence`` reports how coarse the
estimate is for a given utterance.
"""

from __future__ import annotations

import time
from typing import Optional


# Appended to a truncated utterance so the LLM can see the turn was cut off,
# rather than reading a clean sentence that merely happens to end early.
TRUNCATION_MARKER = "— [interrupted here; the user did not hear the rest]"

# Fallback speaking rate, used only when the true synthesized duration is not
# available. ~14 characters/second is roughly 165 wpm, which is where Rime's
# default delivery lands for this agent's short conversational replies.
#
# This fallback exists because of how the fence works in the live pipeline: on
# interruption we STOP pulling frames, so the audio for the un-heard tail is
# never counted. Without a text-derived denominator, every interrupted
# utterance would appear to have played in full — the exact over-report this
# ledger is supposed to prevent.
DEFAULT_CHARS_PER_SECOND = 14.0


class Utterance:
    """One agent speech attempt within a turn."""

    def __init__(self, turn_id: int, started_at: float, chars_per_second: float = DEFAULT_CHARS_PER_SECOND):
        self.turn_id = turn_id
        self.started_at = started_at
        self.synthesized_text: str = ""
        self.played_audio_s: float = 0.0
        self.interrupted_at: Optional[float] = None
        self.finished: bool = False
        self._chars_per_second = chars_per_second
        # Real synthesized duration, when the caller can supply it. Preferred
        # over the text estimate whenever it is present.
        self._explicit_synth_s: float = 0.0

    @property
    def estimated_synth_s(self) -> float:
        if not self.synthesized_text or self._chars_per_second <= 0:
            return 0.0
        return len(self.synthesized_text) / self._chars_per_second

    @property
    def synthesized_audio_s(self) -> float:
        """Total audio this utterance amounts to. A measured value wins; the
        text estimate is the fallback; and it can never be less than what was
        demonstrably played."""
        base = self._explicit_synth_s if self._explicit_synth_s > 0 else self.estimated_synth_s
        return max(base, self.played_audio_s)

    @property
    def played_fraction(self) -> float:
        """Fraction of the synthesized audio that reached playback.

        With no audio accounting at all (a text-only or mocked path) we cannot
        claim anything was heard, so this is 0.0 — the conservative direction.
        """
        if self.synthesized_audio_s <= 0:
            return 0.0
        return min(1.0, self.played_audio_s / self.synthesized_audio_s)


class PlaybackLedger:
    """Per-turn record of what was synthesized versus what was played."""

    def __init__(self, clock=time.perf_counter):
        self._clock = clock
        self._utterances: dict = {}

    # --- recording -------------------------------------------------------

    def begin_utterance(self, turn_id: int) -> Utterance:
        u = Utterance(turn_id, self._clock())
        self._utterances[turn_id] = u
        return u

    def _get(self, turn_id: int) -> Optional[Utterance]:
        return self._utterances.get(turn_id)

    def note_synthesized(self, turn_id: int, text: str) -> None:
        """Text handed to TTS. Called per streamed chunk; chunks concatenate
        in the order they were sent, which is the order they are spoken."""
        u = self._get(turn_id) or self.begin_utterance(turn_id)
        if not text:
            return
        if u.synthesized_text and not u.synthesized_text.endswith(" ") and not text.startswith(" "):
            u.synthesized_text += text
        else:
            u.synthesized_text += text

    def note_frame(self, turn_id: int, samples: int, sample_rate: int) -> None:
        """One audio frame handed toward playback. Frame duration is the only
        unit that ties text position to elapsed speech, so it is what the
        truncation estimate is built on."""
        if sample_rate <= 0:
            return
        self.note_played_seconds(turn_id, samples / sample_rate)

    def note_played_seconds(self, turn_id: int, seconds: float) -> None:
        u = self._get(turn_id) or self.begin_utterance(turn_id)
        u.played_audio_s += seconds

    def note_synthesized_seconds(self, turn_id: int, seconds: float) -> None:
        """Total audio duration TTS produced for this utterance, including
        anything still sitting in a buffer when the interrupt landed. Overrides
        the text-length estimate when the caller can measure it for real."""
        u = self._get(turn_id) or self.begin_utterance(turn_id)
        u._explicit_synth_s += seconds

    def note_interrupted(self, turn_id: int) -> None:
        u = self._get(turn_id)
        if u is not None and u.interrupted_at is None:
            u.interrupted_at = self._clock()

    def note_finished(self, turn_id: int) -> None:
        """The utterance completed without interruption — everything
        synthesized was heard."""
        u = self._get(turn_id)
        if u is not None:
            u.finished = True

    # --- reading ---------------------------------------------------------

    def utterance(self, turn_id: int) -> Optional[Utterance]:
        return self._get(turn_id)

    def was_interrupted(self, turn_id: int) -> bool:
        u = self._get(turn_id)
        return u is not None and u.interrupted_at is not None and not u.finished

    def heard_text(self, turn_id: int, marker: str = TRUNCATION_MARKER) -> Optional[str]:
        """Best-effort reconstruction of what the user actually heard.

        Returns None when there is no record for the turn. Returns the full
        text unchanged when the utterance finished normally. Otherwise
        truncates to the played fraction at a word boundary and appends
        ``marker`` so downstream turns can see the cut.
        """
        u = self._get(turn_id)
        if u is None:
            return None
        text = u.synthesized_text
        if u.finished or u.interrupted_at is None:
            return text
        if not text:
            return ""

        fraction = u.played_fraction
        if fraction >= 1.0:
            return text

        cut = int(len(text) * fraction)
        if cut <= 0:
            # Nothing audible reached the user; from their side this utterance
            # never happened.
            return ""

        # Snap back to the last completed word so history never contains a
        # half-word the user could not have parsed.
        snapped = text.rfind(" ", 0, cut)
        if snapped > 0:
            cut = snapped
        heard = text[:cut].rstrip()
        if not heard:
            return ""
        return f"{heard} {marker}" if marker else heard

    def truncation_confidence(self, turn_id: int) -> str:
        """How much to trust ``heard_text`` for this utterance.

        ``exact``  — the utterance finished; no estimation involved.
        ``estimated`` — real audio accounting exists; cut point is interpolated.
        ``unknown`` — no audio duration was recorded, so we assume nothing was
                      heard rather than guessing.
        """
        u = self._get(turn_id)
        if u is None:
            return "unknown"
        if u.finished or u.interrupted_at is None:
            return "exact"
        if u.played_audio_s <= 0:
            # No playback was accounted for at all, so we cannot claim any of
            # it was heard. heard_text() returns "" in this case rather than
            # guessing at a cut point.
            return "unknown"
        return "estimated"

    def report(self, turn_id: int) -> Optional[dict]:
        u = self._get(turn_id)
        if u is None:
            return None
        return {
            "turn_id": turn_id,
            "synthesized_chars": len(u.synthesized_text),
            "synthesized_audio_s": round(u.synthesized_audio_s, 3),
            "played_audio_s": round(u.played_audio_s, 3),
            "played_fraction": round(u.played_fraction, 3),
            "interrupted": self.was_interrupted(turn_id),
            "confidence": self.truncation_confidence(turn_id),
            "heard_text": self.heard_text(turn_id),
        }

    def reset(self) -> None:
        self._utterances.clear()
