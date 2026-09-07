"""
test_playback.py — Direction 2, state consistency.

Run with: pytest test_playback.py -v

These tests pin the property that matters: after an interruption, the
conversation record must never claim the user heard something they did not.
The ledger is allowed to under-report (record less than was heard) — that is
the safe direction — and is asserted to never over-report.
"""

import pytest

from playback import TRUNCATION_MARKER, PlaybackLedger


SENTENCE = "Your order is delayed because of a carrier issue and should arrive Thursday."


def _ledger_with(turn_id=1, text=SENTENCE, synth_s=4.0, played_s=0.0):
    ledger = PlaybackLedger()
    ledger.begin_utterance(turn_id)
    ledger.note_synthesized(turn_id, text)
    ledger.note_synthesized_seconds(turn_id, synth_s)
    if played_s:
        ledger.note_played_seconds(turn_id, played_s)
    return ledger


def test_uninterrupted_utterance_is_recorded_in_full():
    ledger = _ledger_with(played_s=4.0)
    ledger.note_finished(1)
    assert ledger.heard_text(1) == SENTENCE
    assert ledger.truncation_confidence(1) == "exact"
    assert ledger.was_interrupted(1) is False


def test_interrupted_utterance_is_truncated_to_what_played():
    ledger = _ledger_with(synth_s=4.0, played_s=1.0)
    ledger.note_interrupted(1)
    heard = ledger.heard_text(1)
    assert heard.endswith(TRUNCATION_MARKER)
    body = heard[: -len(TRUNCATION_MARKER)].strip()
    assert SENTENCE.startswith(body)
    assert len(body) < len(SENTENCE)
    assert ledger.truncation_confidence(1) == "estimated"


def test_truncation_never_cuts_mid_word():
    ledger = _ledger_with(synth_s=10.0, played_s=3.3)
    ledger.note_interrupted(1)
    body = ledger.heard_text(1)[: -len(TRUNCATION_MARKER)].strip()
    # Every word in the reconstruction is a complete word from the original.
    original_words = SENTENCE.split()
    assert body.split() == original_words[: len(body.split())]


def test_nothing_played_means_nothing_heard():
    """Interrupted before a single frame got out: from the user's side this
    utterance never happened, and history must not contain it."""
    ledger = _ledger_with(synth_s=4.0, played_s=0.0)
    ledger.note_interrupted(1)
    assert ledger.heard_text(1) == ""


def test_unknown_audio_accounting_assumes_nothing_was_heard():
    """No duration data at all — the ledger must fail toward under-reporting
    rather than assuming the whole sentence landed."""
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, SENTENCE)
    ledger.note_interrupted(1)
    assert ledger.heard_text(1) == ""
    assert ledger.truncation_confidence(1) == "unknown"


def test_played_fraction_cannot_exceed_one():
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, SENTENCE)
    ledger.note_played_seconds(1, 5.0)  # more accounted-for playback than synthesis
    assert ledger.utterance(1).played_fraction <= 1.0


def test_frames_accumulate_into_played_duration():
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, SENTENCE)
    ledger.note_synthesized_seconds(1, 2.0)
    for _ in range(10):
        ledger.note_frame(1, samples=480, sample_rate=48000)  # 10ms each
    assert ledger.utterance(1).played_audio_s == pytest.approx(0.1)
    assert ledger.utterance(1).played_fraction == pytest.approx(0.05)


def test_streamed_text_chunks_concatenate_in_spoken_order():
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    for chunk in ["Your order ", "is delayed ", "until Thursday."]:
        ledger.note_synthesized(1, chunk)
    ledger.note_synthesized_seconds(1, 3.0)
    ledger.note_played_seconds(1, 3.0)
    ledger.note_finished(1)
    assert ledger.heard_text(1) == "Your order is delayed until Thursday."


def test_report_is_complete_enough_to_audit_a_truncation():
    ledger = _ledger_with(synth_s=4.0, played_s=2.0)
    ledger.note_interrupted(1)
    report = ledger.report(1)
    assert report["interrupted"] is True
    assert report["played_fraction"] == 0.5
    assert report["confidence"] == "estimated"
    assert report["synthesized_chars"] == len(SENTENCE)
    assert len(report["heard_text"]) < len(SENTENCE) + len(TRUNCATION_MARKER)


def test_unknown_turn_reports_nothing_rather_than_guessing():
    ledger = PlaybackLedger()
    assert ledger.report(42) is None
    assert ledger.heard_text(42) is None


def test_live_path_truncates_even_though_unplayed_frames_are_never_counted():
    """Reproduces how the live tts_node behaves: on interruption it STOPS
    pulling frames, so the tail's audio is never accounted for. Without a
    text-derived denominator this would report 100% played and put the full,
    un-heard sentence back into history."""
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, SENTENCE)          # ~5.4s of speech at the default rate
    for _ in range(100):                          # 1.0s of frames actually emitted
        ledger.note_frame(1, samples=480, sample_rate=48000)
    ledger.note_interrupted(1)

    heard = ledger.heard_text(1)
    assert heard != SENTENCE
    assert len(heard) < len(SENTENCE) + len(TRUNCATION_MARKER)
    assert ledger.utterance(1).played_fraction < 0.3


def test_measured_duration_beats_the_text_estimate():
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, SENTENCE)
    ledger.note_synthesized_seconds(1, 2.0)       # measured: this reply was fast
    ledger.note_played_seconds(1, 1.0)
    ledger.note_interrupted(1)
    assert ledger.utterance(1).played_fraction == pytest.approx(0.5)
