"""
test_latency.py — Direction 1 (perceived response time).

Run with: pytest test_latency.py -v

The tracker is driven with an injected clock so the assertions are about the
measurement logic, not about how fast the test machine happens to be. The
real numbers come from a live run (see RIME_EVIDENCE.md); what is tested here
is that the instrument does not lie: stage attribution, percentiles, exclusion
of aborted turns, and the perceived-vs-real distinction when a filler is used.
"""

import asyncio

import pytest

import latency
from latency import FillerPolicy, GapCover, LatencyTracker


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000
        return self.t


def _run_turn(tracker, clock, turn_id, stt=120, llm_req=5, ttft=300, tts_req=40, ttfb=180, frame=60):
    tracker.begin_turn(turn_id)
    clock.advance_ms(stt);      tracker.mark(turn_id, latency.STT_FINAL)
    clock.advance_ms(llm_req);  tracker.mark(turn_id, latency.LLM_REQUEST)
    clock.advance_ms(ttft);     tracker.mark(turn_id, latency.LLM_FIRST_TOKEN)
    clock.advance_ms(tts_req);  tracker.mark(turn_id, latency.TTS_REQUEST)
    clock.advance_ms(ttfb);     tracker.mark(turn_id, latency.TTS_FIRST_BYTE)
    clock.advance_ms(frame);    tracker.mark(turn_id, latency.FIRST_AUDIO_FRAME)


def test_ttfa_is_measured_from_speech_end_not_transcript():
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    _run_turn(tracker, clock, 1)
    # 120 + 5 + 300 + 40 + 180 + 60
    assert tracker.timeline(1).ttfa_ms() == pytest.approx(705, abs=0.5)


def test_stage_breakdown_attributes_cost_to_the_right_stage():
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    _run_turn(tracker, clock, 1)
    rows = {r["stage"]: r["ms"] for r in tracker.timeline(1).breakdown()}
    assert rows[latency.LLM_FIRST_TOKEN] == pytest.approx(300, abs=0.5)
    assert rows[latency.TTS_FIRST_BYTE] == pytest.approx(180, abs=0.5)
    assert rows[latency.STT_FINAL] == pytest.approx(120, abs=0.5)


def test_missing_stage_is_skipped_not_folded_into_its_neighbour():
    """A stage that never fired must not have its gap silently attributed to
    the previous stage — that would blame the wrong component."""
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin_turn(1)
    clock.advance_ms(100); tracker.mark(1, latency.STT_FINAL)
    clock.advance_ms(400); tracker.mark(1, latency.FIRST_AUDIO_FRAME)
    stages = [r["stage"] for r in tracker.timeline(1).breakdown()]
    assert latency.LLM_FIRST_TOKEN not in stages
    row = [r for r in tracker.timeline(1).breakdown() if r["stage"] == latency.FIRST_AUDIO_FRAME][0]
    assert row["from"] == latency.STT_FINAL


def test_first_mark_wins_so_repeated_chunks_do_not_move_the_number():
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin_turn(1)
    clock.advance_ms(200)
    first = tracker.mark(1, latency.LLM_FIRST_TOKEN)
    clock.advance_ms(500)
    second = tracker.mark(1, latency.LLM_FIRST_TOKEN)
    assert first == pytest.approx(200, abs=0.5)
    assert second is None
    assert tracker.timeline(1).offset_ms(latency.LLM_FIRST_TOKEN) == pytest.approx(200, abs=0.5)


def test_mark_for_unknown_turn_is_ignored_not_invented():
    tracker = LatencyTracker(clock=FakeClock())
    assert tracker.mark(99, latency.FIRST_AUDIO_FRAME) is None
    assert tracker.summary()["turns_measured"] == 0


def test_aborted_turns_are_excluded_from_percentiles():
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    _run_turn(tracker, clock, 1)
    tracker.begin_turn(2)          # user interrupted before any audio
    clock.advance_ms(300)
    tracker.mark_aborted(2)
    summary = tracker.summary()
    assert summary["turns_measured"] == 1
    assert summary["turns_aborted_before_audio"] == 1


def test_over_budget_tail_is_reported_separately_from_p50():
    clock = FakeClock()
    tracker = LatencyTracker(budget_ms=800, clock=clock)
    for i in range(1, 10):
        _run_turn(tracker, clock, i)                      # ~705ms, under budget
    _run_turn(tracker, clock, 10, ttft=3000)              # blown tail
    summary = tracker.summary()
    assert summary["ttfa"]["p50_ms"] < 800
    assert summary["over_budget_turns"] == [10]
    assert summary["over_budget_rate"] == pytest.approx(0.1, abs=0.01)


def test_slowest_stage_points_at_the_real_culprit():
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    for i in range(1, 4):
        _run_turn(tracker, clock, i, ttfb=900)
    assert tracker.slowest_stage() == latency.TTS_FIRST_BYTE


def test_filler_changes_perceived_time_but_not_reported_ttfa():
    """The filler must never be allowed to flatter the TTFA number — that is
    the metric that says whether the pipeline is actually fast."""
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin_turn(1)
    clock.advance_ms(700)
    tracker.note_filler(1, "One moment.")
    clock.advance_ms(2000)
    tracker.mark(1, latency.FIRST_AUDIO_FRAME)

    tl = tracker.timeline(1)
    assert tl.ttfa_ms() == pytest.approx(2700, abs=1)
    assert tl.time_to_any_audio_ms() == pytest.approx(700, abs=1)


def test_re_begun_turn_is_counted_once():
    """A false endpoint followed by the real one must not double-count."""
    clock = FakeClock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin_turn(1)
    clock.advance_ms(200)
    _run_turn(tracker, clock, 1)  # begins turn 1 again from the later speech end
    assert tracker.summary()["turns_measured"] == 1


# --- GapCover --------------------------------------------------------------

@pytest.mark.asyncio
async def test_gap_cover_stays_silent_when_audio_arrives_in_time():
    spoken = []
    policy = FillerPolicy(threshold_ms=200)
    cover = GapCover(policy, speak=lambda p: _record(spoken, p))
    started = asyncio.Event()

    async def audio_soon():
        await asyncio.sleep(0.02)
        started.set()

    asyncio.create_task(audio_soon())
    result = await cover.run(1, started, is_stale=lambda: False)
    assert result is None
    assert spoken == []


@pytest.mark.asyncio
async def test_gap_cover_speaks_when_the_gap_runs_long():
    spoken = []
    policy = FillerPolicy(threshold_ms=50)
    tracker = LatencyTracker()
    tracker.begin_turn(1)
    cover = GapCover(policy, speak=lambda p: _record(spoken, p), tracker=tracker)
    result = await cover.run(1, asyncio.Event(), is_stale=lambda: False)
    assert result in FillerPolicy.DEFAULT_PHRASES
    assert spoken == [result]
    assert tracker.timeline(1).filler_text == result


@pytest.mark.asyncio
async def test_gap_cover_does_not_speak_into_a_stale_turn():
    """The user interrupted while we were waiting out the threshold. Speaking
    now would talk over them with filler for an abandoned question."""
    spoken = []
    policy = FillerPolicy(threshold_ms=30)
    cover = GapCover(policy, speak=lambda p: _record(spoken, p))
    result = await cover.run(1, asyncio.Event(), is_stale=lambda: True)
    assert result is None
    assert spoken == []


def test_filler_policy_does_not_repeat_itself_back_to_back():
    clock = FakeClock()
    policy = FillerPolicy(threshold_ms=100, min_gap_s=10, clock=clock)
    assert policy.should_fill(150, audio_started=False) is True
    first = policy.take_phrase()
    # Immediately afterwards, another gap: too soon to fill again.
    assert policy.should_fill(150, audio_started=False) is False
    clock.advance_ms(11_000)
    assert policy.should_fill(150, audio_started=False) is True
    assert policy.take_phrase() != first  # rotates rather than repeating


def test_filler_policy_never_fires_once_audio_started():
    policy = FillerPolicy(threshold_ms=10)
    assert policy.should_fill(5000, audio_started=True) is False


async def _record(sink, phrase):
    sink.append(phrase)
