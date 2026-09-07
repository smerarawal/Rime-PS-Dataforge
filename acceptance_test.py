"""
acceptance_test.py

The runnable acceptance test for this project's three claims. Exits non-zero
if any claim fails, so it can gate a commit.

  A. Perceived response time — the pipeline is instrumented end to end, the
     instrumentation costs effectively nothing, and when the real answer runs
     long the user hears something within the filler budget instead of silence.

  B. Interruption and recovery — when the user interrupts mid-response,
     including while a tool call is running, the stale result is never
     surfaced, and the conversation record is truncated to what the user
     actually heard.

  C. Continuity during tool work — a status question, an added constraint or a
     backchannel during a slow tool call keeps that call alive and answers the
     user immediately; only a genuinely new request throws it away.

IMPORTANT — what this script can and cannot prove:

This script exercises the real TurnManager / tools.py / playback.py /
latency.py logic, directly and repeatably, without a live microphone or a
running LiveKit room. That makes it a good, fast, scriptable test of the
LOGIC.

It does NOT and CANNOT measure real Rime audio playback stopping, or real
end-to-end TTFA — there is no audio engine, no LiveKit room, and no Rime
connection in this process. The "Rime audio stops within X ms" and "first
audio within X ms" halves of the claims can only be measured from a live run
through the actual pipeline (agent.py + Deepgram/Groq/Rime), which is
instrumented to emit exactly those numbers to live_session_latency.json.
Treat this script's numbers and the live pipeline's numbers as two separate
things in RIME_EVIDENCE.md — do not conflate them.

Usage:
    python acceptance_test.py --trials 20
    python acceptance_test.py --trials 20 --tool-delay-ms 4000
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import latency
from continuity import Intent
from latency import FillerPolicy, GapCover, LatencyTracker
from metrics import MetricsLog
from playback import PlaybackLedger
from tools import _lookup_order_status_impl
from turn_manager import StaleResultError, TurnManager


# ---------------------------------------------------------------------------
# A. Perceived response time
# ---------------------------------------------------------------------------

async def suite_latency(trials: int):
    """Two things are actually measurable in-process:

    1. Instrumentation overhead. Latency instrumentation that itself costs
       latency is worse than none, so it is measured rather than assumed.
    2. Perceived time-to-audio when the real answer is slow. GapCover's job
       is to put something audible in front of the user within its threshold;
       that is a real, timeable property of this code.

    Real pipeline TTFA is NOT measurable here — see the module docstring.
    """
    # 1. overhead
    tracker = LatencyTracker()
    t0 = time.perf_counter()
    for i in range(1000):
        tracker.begin_turn(i)
        for stage in latency.STAGE_ORDER[1:]:
            tracker.mark(i, stage)
    overhead_us_per_turn = (time.perf_counter() - t0) / 1000 * 1_000_000

    # 2. perceived time-to-audio under a slow answer
    threshold_ms = 300.0
    perceived = []
    for _ in range(trials):
        spoken = []
        policy = FillerPolicy(threshold_ms=threshold_ms, min_gap_s=0)
        cover = GapCover(policy, speak=lambda p: _append(spoken, p))
        audio_started = asyncio.Event()
        start = time.perf_counter()

        async def slow_real_audio():
            await asyncio.sleep(1.5)  # the answer the user is actually waiting for
            audio_started.set()

        audio_task = asyncio.create_task(slow_real_audio())
        phrase = await cover.run(1, audio_started, is_stale=lambda: False)
        perceived_ms = (time.perf_counter() - start) * 1000
        audio_task.cancel()
        try:
            await audio_task
        except asyncio.CancelledError:
            pass

        assert phrase is not None and spoken == [phrase]
        perceived.append(perceived_ms)

    p95 = _percentile(sorted(perceived), 0.95)
    # Allowance for scheduler jitter on a loaded machine; the claim is "within
    # the budget", not "to the microsecond".
    passed = p95 <= threshold_ms + 150 and overhead_us_per_turn < 100

    return {
        "suite": "A. perceived response time",
        "passed": passed,
        "instrumentation_overhead_us_per_turn": round(overhead_us_per_turn, 2),
        "filler_threshold_ms": threshold_ms,
        "perceived_time_to_audio_p50_ms": round(statistics.median(perceived), 1),
        "perceived_time_to_audio_p95_ms": round(p95, 1),
        "note": (
            "Perceived-time numbers are for the gap-cover path only. Real "
            "pipeline TTFA comes from a live run (live_session_latency.json)."
        ),
    }


async def _append(sink, phrase):
    sink.append(phrase)


# ---------------------------------------------------------------------------
# B. Interruption and recovery
# ---------------------------------------------------------------------------

async def run_single_trial(trial_num: int, tool_delay_ms: int):
    """One trial: ask for order status, interrupt mid-lookup with a
    different request, actually cancel the in-flight tool task (matching
    what agent.py does in production via route_utterance), and verify the
    stale result never surfaces even if cancellation is slow or doesn't land
    cleanly."""
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = str(tool_delay_ms)

    tm = TurnManager()
    MetricsLog.reset()

    # --- Simulated turn 1: order status request ---
    turn1_id = tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    lookup_task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))
    work.task = lookup_task

    # Give the tool a moment to actually start running before interrupting,
    # so we reliably land mid-flight rather than racing the very start.
    await asyncio.sleep(min(0.5, tool_delay_ms / 2000))

    # --- Interrupt: turn 2 supersedes turn 1 mid-lookup ---
    loop = asyncio.get_event_loop()
    interrupt_ts = loop.time()
    MetricsLog.record("interrupt_detected", turn_id=turn1_id, trial=trial_num)

    # This is the real production path: route the utterance, which classifies
    # it as a new request, advances the turn and cancels the work.
    decision = tm.route_utterance("actually, what's your return policy?")
    assert decision.intent is Intent.NEW_REQUEST
    turn2_id = decision.turn_id

    stale_leaked = False
    outcome = None
    try:
        result = await lookup_task
        # Reached only if the task completed with a real return value
        # despite being cancelled — the fence failed to catch it.
        stale_leaked = True
        outcome = "leaked"
        MetricsLog.record("STALE_RESULT_LEAKED", turn_id=turn1_id, trial=trial_num, result=result)
    except asyncio.CancelledError:
        outcome = "cancelled_cleanly"
        MetricsLog.record("tool_task_cancelled", turn_id=turn1_id, trial=trial_num)
    except StaleResultError:
        outcome = "blocked_by_fence"
        MetricsLog.record("stale_result_correctly_blocked", turn_id=turn1_id, trial=trial_num)
    except Exception as e:
        # Any other exception during teardown still counts as "did not
        # leak a stale result" — record what it actually was rather than
        # silently bucketing it.
        outcome = f"blocked_other:{type(e).__name__}"
        MetricsLog.record("stale_result_blocked_other", turn_id=turn1_id, trial=trial_num, error=str(e))

    cancel_resolved_ts = loop.time()
    MetricsLog.record("tool_cancellation_resolved", turn_id=turn1_id, trial=trial_num)
    tool_cancellation_latency_ms = round((cancel_resolved_ts - interrupt_ts) * 1000, 2)

    return {
        "trial": trial_num,
        "tool_cancellation_latency_ms": tool_cancellation_latency_ms,
        "outcome": outcome,
        "stale_leaked": stale_leaked,
        "turn1_id": turn1_id,
        "turn2_id": turn2_id,
    }


def _recovery_state_check():
    """The second half of the interruption claim: after the cut, does the
    conversation record match what the user actually heard?"""
    full = ("Your order is delayed because of a carrier issue, and the "
            "current estimate is Thursday afternoon.")
    ledger = PlaybackLedger()
    ledger.begin_utterance(1)
    ledger.note_synthesized(1, full)
    ledger.note_synthesized_seconds(1, 6.0)
    ledger.note_played_seconds(1, 1.5)   # user barged in a quarter of the way through
    ledger.note_interrupted(1)

    report = ledger.report(1)
    heard = report["heard_text"]
    body = heard.split("—")[0].strip()
    return {
        "played_fraction": report["played_fraction"],
        "confidence": report["confidence"],
        "history_kept_full_text": heard == full,
        "history_is_a_prefix_of_what_was_said": full.startswith(body),
        "no_partial_words": all(w in full.split() for w in body.split()),
        "passed": (heard != full and full.startswith(body)
                   and all(w in full.split() for w in body.split())),
    }


async def suite_interruption(trials: int, tool_delay_ms: int):
    results = []
    for i in range(trials):
        result = await run_single_trial(i, tool_delay_ms)
        results.append(result)
        status = "LEAKED (FAIL)" if result["stale_leaked"] else f"blocked (pass, {result['outcome']})"
        print(f"  Trial {i}: tool_cancellation={result['tool_cancellation_latency_ms']}ms, stale_result={status}")

    latencies = sorted(r["tool_cancellation_latency_ms"] for r in results)
    leaks = sum(1 for r in results if r["stale_leaked"])
    recovery = _recovery_state_check()

    return {
        "suite": "B. interruption and recovery",
        "passed": leaks == 0 and recovery["passed"],
        "trials": len(results),
        "stale_results_leaked": leaks,
        "tool_cancellation_latency_p50_ms": statistics.median(latencies),
        "tool_cancellation_latency_p95_ms": _percentile(latencies, 0.95),
        "state_recovery": recovery,
        "note": (
            "tool_cancellation_latency measures how fast the in-process "
            "fencing/cancellation logic reacts. It is NOT the 'Rime audio "
            "stops within X ms' claim — that needs a live run."
        ),
    }


# ---------------------------------------------------------------------------
# C. Continuity during tool work
# ---------------------------------------------------------------------------

async def _continuity_scenario(utterance: str, expect_intent: Intent, expect_kept: bool,
                               tool_delay_ms: int = 400):
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = str(tool_delay_ms)
    tm = TurnManager()
    tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))
    work.task = task

    await asyncio.sleep(0.05)                       # land mid-flight
    t0 = time.perf_counter()
    decision = tm.route_utterance(utterance)
    decision_ms = (time.perf_counter() - t0) * 1000  # cost on the interrupt path

    delivered = None
    error = None
    try:
        delivered = await task
    except (StaleResultError, asyncio.CancelledError) as e:
        error = type(e).__name__
    except Exception as e:  # pragma: no cover - surfaced in the report if hit
        error = f"{type(e).__name__}: {e}"

    got_result = delivered is not None
    passed = (
        decision.intent is expect_intent
        and decision.work_kept is expect_kept
        and got_result is expect_kept          # kept work delivers; dropped work never does
    )
    return {
        "utterance": utterance,
        "intent": decision.intent.value,
        "expected_intent": expect_intent.value,
        "work_kept": decision.work_kept,
        "result_delivered": got_result,
        "immediate_reply": decision.reply,
        "routing_decision_ms": round(decision_ms, 3),
        "outcome_if_dropped": error,
        "passed": passed,
    }


async def suite_continuity():
    scenarios = [
        ("are you still there?", Intent.STATUS_REQUEST, True),
        ("any luck?", Intent.STATUS_REQUEST, True),
        ("while you're at it check the refund too", Intent.CONSTRAINT, True),
        ("mhm", Intent.AFFIRMATION, True),
        ("never mind", Intent.CANCEL, False),
        ("actually, what's your return policy?", Intent.NEW_REQUEST, False),
    ]
    rows = []
    for utterance, intent, kept in scenarios:
        row = await _continuity_scenario(utterance, intent, kept)
        rows.append(row)
        mark = "pass" if row["passed"] else "FAIL"
        print(f"  {mark}: {utterance!r} -> {row['intent']} "
              f"(work_kept={row['work_kept']}, delivered={row['result_delivered']}, "
              f"routing={row['routing_decision_ms']}ms)")

    worst_routing = max(r["routing_decision_ms"] for r in rows)
    return {
        "suite": "C. continuity during tool work",
        "passed": all(r["passed"] for r in rows) and worst_routing < 5.0,
        "scenarios": rows,
        "worst_routing_decision_ms": worst_routing,
        "note": (
            "Routing runs on the interrupt path, so its cost is part of "
            "perceived latency — it is asserted to stay under 5ms and needs "
            "no model round-trip."
        ),
    }


# ---------------------------------------------------------------------------

def _percentile(sorted_values, pct):
    """Nearest-rank percentile. Simple and fine for small N, but note in
    RIME_EVIDENCE.md that this is nearest-rank, not interpolated, if N is
    small enough for that distinction to matter."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--tool-delay-ms", type=int, default=4000)
    args = parser.parse_args()

    print("=== A. Perceived response time ===")
    a = await suite_latency(min(args.trials, 10))
    print(f"  instrumentation overhead: {a['instrumentation_overhead_us_per_turn']}us/turn")
    print(f"  perceived time-to-audio (slow answer): p50={a['perceived_time_to_audio_p50_ms']}ms "
          f"p95={a['perceived_time_to_audio_p95_ms']}ms "
          f"(filler threshold {a['filler_threshold_ms']}ms)")

    print("\n=== B. Interruption and recovery ===")
    b = await suite_interruption(args.trials, args.tool_delay_ms)
    print(f"  stale results leaked: {b['stale_results_leaked']}/{b['trials']}")
    print(f"  tool-cancellation latency (logic only, NOT real audio) — "
          f"p50: {b['tool_cancellation_latency_p50_ms']}ms, p95: {b['tool_cancellation_latency_p95_ms']}ms")
    r = b["state_recovery"]
    print(f"  post-interrupt history: truncated to {r['played_fraction']:.0%} of the utterance "
          f"(confidence: {r['confidence']}), prefix-correct={r['history_is_a_prefix_of_what_was_said']}")

    print("\n=== C. Continuity during tool work ===")
    c = await suite_continuity()

    report = {
        "generated_at": time.time(),
        "trials": args.trials,
        "tool_delay_ms": args.tool_delay_ms,
        "suites": [a, b, c],
        "all_passed": all(s["passed"] for s in (a, b, c)),
        "events": MetricsLog.all_events(),
    }
    with open("acceptance_test_results.json", "w") as f:
        json.dump(report, f, indent=2)

    print("\n--- Summary ---")
    for s in (a, b, c):
        print(f"  [{'PASS' if s['passed'] else 'FAIL'}] {s['suite']}")
    print("\nFull report written to acceptance_test_results.json")
    print("\nNOTE: every number above is in-process logic timing. Real Rime "
          "audio-stop latency and real end-to-end TTFA come from a live run "
          "(`python agent.py dev`), which writes live_session_latency.json.")

    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
