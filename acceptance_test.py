"""
acceptance_test.py

The runnable acceptance test for this project's hard voice problem claim:

  "When the user interrupts mid-response — including while a tool call is
  running — Rime audio stops within X ms, the stale tool result is never
  spoken, and the agent's next reply reflects only the user's new request."

IMPORTANT — what this script can and cannot prove:
This script exercises the real TurnManager/tools.py fencing and
cancellation logic, directly and repeatably, without a live microphone or
a running LiveKit room. That makes it a good, fast, scriptable test of
the LOGIC: does an interrupted tool call actually get cancelled, and does
a stale result get blocked if cancellation doesn't land in time.

It does NOT and CANNOT measure real Rime audio playback stopping — there
is no audio engine, no LiveKit room, and no Rime connection in this
process. The "Rime audio stops within X ms" half of the claim can only be
measured from a live run through the actual pipeline (agent.py +
Deepgram/Groq/Rime), instrumented with MetricsLog calls at the real
interrupt-detected and real playback-stopped events. Treat this script's
"tool_cancellation_latency_ms" and the live pipeline's real audio-stop
latency as two separate numbers in RIME_EVIDENCE.md — do not conflate
them, and do not present this script's output as proof of the audio half
of the claim.

Usage:
    STRESS_TEST_TOOL_DELAY_MS=5000 python acceptance_test.py --trials 20

Results are printed as p50/p95 and also exported to
acceptance_test_results.json for RIME_EVIDENCE.md.
"""

import argparse
import asyncio
import os
import statistics

from turn_manager import TurnManager, StaleResultError
from metrics import MetricsLog
from tools import _lookup_order_status_impl


async def run_single_trial(trial_num: int, tool_delay_ms: int):
    """One trial: ask for order status, interrupt mid-lookup with a
    different request, actually cancel the in-flight tool task (matching
    what agent.py does in production via cancel_active_tool_task), and
    verify the stale result never surfaces even if cancellation is slow
    or doesn't land cleanly."""
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = str(tool_delay_ms)

    tm = TurnManager()
    MetricsLog.reset()

    # --- Simulated turn 1: order status request ---
    turn1_id = tm.start_new_turn()

    lookup_task = asyncio.create_task(_lookup_order_status_impl(tm, "1002"))

    # Give the tool a moment to actually start running before interrupting,
    # so we reliably land mid-flight rather than racing the very start.
    await asyncio.sleep(min(0.5, tool_delay_ms / 2000))

    # --- Interrupt: turn 2 supersedes turn 1 mid-lookup ---
    loop = asyncio.get_event_loop()
    interrupt_ts = loop.time()
    MetricsLog.record("interrupt_detected", turn_id=turn1_id, trial=trial_num)
    turn2_id = tm.start_new_turn()

    # Actually cancel the stale task — this is the real production path
    # (cancel_active_tool_task in agent.py), not a no-op. Measuring the
    # time from interrupt to "cancellation resolved" gives an honest,
    # if narrow, latency number: how fast does OUR logic react, before
    # any audio-layer effects are even in the picture.
    lookup_task.cancel()

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


def percentile(sorted_values, pct):
    """Nearest-rank percentile. Simple and fine for small N, but note in
    RIME_EVIDENCE.md that this is nearest-rank, not interpolated, if N is
    small enough for that distinction to matter."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--tool-delay-ms", type=int, default=4000)
    args = parser.parse_args()

    results = []
    for i in range(args.trials):
        result = await run_single_trial(i, args.tool_delay_ms)
        results.append(result)
        status = "LEAKED (FAIL)" if result["stale_leaked"] else f"blocked (pass, {result['outcome']})"
        print(f"Trial {i}: tool_cancellation={result['tool_cancellation_latency_ms']}ms, stale_result={status}")

    latencies = sorted(r["tool_cancellation_latency_ms"] for r in results)
    leaks = sum(1 for r in results if r["stale_leaked"])

    print("\n--- Results ---")
    print(f"Trials: {len(results)}")
    print(f"Stale results leaked: {leaks}/{len(results)}")
    print(f"Tool-cancellation latency (logic only, NOT real audio) — "
          f"p50: {statistics.median(latencies)}ms, p95: {percentile(latencies, 0.95)}ms")
    print("\nNOTE: this number measures how fast the in-process fencing/cancellation")
    print("logic reacts. It is NOT the 'Rime audio stops within Xms' claim — that")
    print("requires a separate live pipeline run (python agent.py dev) with real")
    print("interrupt-to-playback-stop timestamps. Do not substitute this number")
    print("for that one in RIME_EVIDENCE.md.")

    MetricsLog.export_json("acceptance_test_results.json")
    print("\nFull event log written to acceptance_test_results.json")


if __name__ == "__main__":
    asyncio.run(main())
