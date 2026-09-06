# RIME_EVIDENCE.md

Evidence for the `RimeTTSProvider` adapter (`backend/app/adapters/rime.py`) and
its interrupt-handling behavior inside the real `Orchestrator`
(`Rime-PS-Dataforge`, branch `atharva`). This is the submission's actual
architecture — not the earlier `dataforge_rime/` standalone prototype, which
was an exploration phase superseded by this Orchestrator design.

## 1. API correctness (verified against live Rime API)

`backend/tests/test_rime_live_manual.py` makes real calls to `wss://users-ws.rime.ai/ws3`
(requires `RIME_API_KEY`, not part of the automated pytest suite — run manually):

- Confirmed the audio-chunk JSON key is always `data` (not `audio`). The
  adapter was simplified accordingly (commit `7df8017`).
- Confirmed `RimeTTSProvider.speak()` receives real, non-empty audio bytes
  through the full adapter code path, not just a raw connection.

## 2. Unit-level correctness (`test_rime_adapter_manual.py`, mocked websocket)

1. Normal completion — streams fully, `completed_at` set, audio bytes received.
2. Interruption via `stop()` mid-stream — `completed_at` stays `None`,
   `stopped_at` gets set, stream halts.
3. Idempotent double-`stop()` — does not raise.

## 3. Full-system acceptance test (`test_rime_full_system_acceptance.py`)

This is the test that matters: the **real** `Orchestrator` wired to the
**real** `RimeTTSProvider` (websocket mocked so it's deterministic/CI-safe —
no network or API key needed), interrupting **while audio is genuinely
mid-stream** (200 chunks, 10ms delay each) — not the instant-completion case
that `MockTTSProvider` (`chunk_delay_seconds=0.0`) exercises elsewhere in the
suite.

**20 trials, interrupt-during-real-TTS-stream:**

| Metric | Value |
|---|---|
| Leaks (stale audio/response reaching the user after invalidation) | **0 / 20** |
| `tts_stop_latency_ms` p50 | 0.14 ms |
| `tts_stop_latency_ms` p95 | 0.21 ms |
| `tts_stop_latency_ms` max | 0.24 ms |

Leak detection method: `EvidenceCollector` (`backend/app/utils/evidence.py`)
subscribes to the `Orchestrator`'s `EventBus` independently (no changes to
`Orchestrator` itself) and flags any `TTS_START` or `ASSISTANT_RESPONSE_READY`
event whose `request_id` was already `REQUEST_INVALIDATED` beforehand. Zero
such events occurred across 20 trials.

Latency measurement: elapsed time between the `INTERRUPTION` event and the
`TTS_STOP` event for the same `request_id`, both real `EventBus` timestamps.
Sub-millisecond latency is expected here — `RimeTTSProvider.stop()` flips its
internal flag before any I/O, so the fencing/staleness check on the very next
loop iteration is what actually halts the stream; closing the websocket
afterward is a secondary belt-and-suspenders cutoff.

## 4. Full automated suite

```
pytest -q
```
**26 passed** (24 pre-existing + 2 new: single-trial and 20-trial acceptance tests).

## 5. Known gaps / not yet covered

- All of the above (§3) mocks the websocket layer for determinism. A live,
  real-API run of the same interrupt-during-stream scenario has not been
  done — `test_rime_live_manual.py` verifies API correctness but doesn't
  exercise the interrupt race. Recommended as a final manual check with a
  real `RIME_API_KEY` before submission, if time allows.
- These numbers cover the `RimeTTSProvider` adapter and `Orchestrator`
  fencing only. End-to-end browser/LiveKit-room testing (real mic input,
  real playback) is Nikunj's `RealtimeInputAdapter` scope, not covered here.
