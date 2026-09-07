# Evidence

Three claims, what backs each one, and — just as important — what does *not*
back it. Every number below is reproducible from a clean checkout.

Reproduce the offline evidence:

```bash
pytest -q                                          # 73 tests
python acceptance_test.py --trials 20              # exits non-zero on failure
```

Reproduce the live evidence (needs real credentials in `.env`):

```bash
STRESS_TEST_TOOL_DELAY_MS=4000 python agent.py dev
# talk, interrupt, then Ctrl-C — writes live_session_latency.json
#                                     and live_session_metrics.json
```

---

## The one thing to read first: what the offline numbers are not

`acceptance_test.py` runs in a plain Python process. There is no audio engine,
no LiveKit room and no Rime connection in it. So it can prove the **logic** —
that an interrupted lookup is cancelled, that a stale result cannot pass the
fence, that a status question keeps work alive — and it can time that logic.

It cannot measure real Rime audio stopping, and it cannot measure real
end-to-end time-to-first-audio. Those two numbers come only from a live run,
which is instrumented to emit them to `live_session_latency.json`.

The two are kept as separate numbers throughout this document and in the JSON
output. They are not interchangeable and are never presented as each other.

---

## Claim A — Perceived response time

**Claim.** The delay the user perceives is measured end to end, attributed to
the stage that caused it, and covered when it runs long.

### What is instrumented

`latency.LatencyTracker` timestamps the whole path, starting at VAD
end-of-speech — *not* at transcript arrival, because endpointing delay is
latency the user feels and excluding it would flatter the numbers:

| Interval ends at | What it measures | Where it is marked |
|---|---|---|
| `stt_final` | endpointing + STT finalization | `on_user_turn` in [agent.py](agent.py) |
| `llm_request` | turn bookkeeping before LLM dispatch | `Assistant.llm_node` |
| `llm_first_token` | Groq time-to-first-token | `Assistant.llm_node`, first chunk |
| `tts_request` | first sentence buffered for TTS | `Assistant.tts_node` |
| `tts_first_byte` | Rime time-to-first-byte | `Assistant.tts_node`, first frame |
| `first_audio_frame` | decode + handoff to playback | `Assistant.tts_node`, first frame |

`summary()` reports p50/p95 per stage plus over-budget turns, and
`slowest_stage()` names the stage to attack first. A live run prints this on
shutdown and writes it to `live_session_latency.json`.

### What is done to reduce the delay

| Change | Why | Where |
|---|---|---|
| VAD model loaded in `prewarm_fnc` | Cold start is latency only the first caller pays, which makes it invisible in a p50 and very visible to that caller. | `prewarm` in [agent.py](agent.py) |
| Rime over websocket, streaming | Avoids a fresh connection handshake inside the response path. | `rime.TTS(use_websocket=True)` |
| TTS sample rate pinned to 48 kHz | Matches the room rate, so the native resampler runs less often. | `rime.TTS(sample_rate=48000)` |
| `min_endpointing_delay=0.5` | Pure additive latency on every turn. 0.5s is the floor found that does not clip people mid-sentence; going lower trades barge-in correctness for TTFA. The tradeoff stays visible as the first row of the breakdown. | `AgentSession(...)` |
| Immediate tool acknowledgement | A slow lookup no longer starts with silence. | `lookup_order_status` in [tools.py](tools.py) |
| Intent routing is pattern-based, not model-based | It runs on the interrupt path; an extra LLM round-trip there would cost more than the feature saves. Asserted under 5ms. | [continuity.py](continuity.py) |
| `GapCover` filler past a threshold | When actual delay cannot be reduced further, the user hears the agent engage instead of dead air. | [latency.py](latency.py) |

### Measured — offline (`python acceptance_test.py`)

| Metric | Result |
|---|---|
| Instrumentation overhead | ~7.5 µs per turn (measured over 1000 turns) |
| Perceived time-to-audio when the answer is slow, p50 | ~308 ms against a 300 ms filler threshold |
| Perceived time-to-audio, p95 | ~316 ms |
| Routing decision cost on the interrupt path (worst of 6 scenarios) | 0.27 ms |

The first row matters more than it looks: latency instrumentation that itself
costs latency is worse than none, so it is measured rather than assumed.

### Measured — live

Not yet filled in. Running the live command above populates
`live_session_latency.json` with real TTFA p50/p95 and the per-stage
breakdown; paste the printed `[LATENCY SUMMARY]` block here. Until that is
done, **this project has no verified end-to-end TTFA number** and does not
claim one.

### Honest limits

- `GapCover` improves perceived latency, not real latency. `ttfa_ms` always
  reports the first *real* audio frame and is never allowed to count the
  filler; `time_to_any_audio_ms` reports what the user actually heard first.
  Both are in the JSON, separately, on purpose.
- The first-audio mark is when the frame is handed to playback from the agent
  process. Network transit and the client's jitter buffer add to what the
  user experiences and are not observable from here.

---

## Claim B — Interruption and recovery

**Claim.** When the user interrupts mid-response — including while a tool call
is running — queued audio stops, the stale result is never spoken, and the
conversation state matches what the user actually heard.

### Mechanism

Three layers, because each one alone has a hole:

1. **Cancellation** (`cancel_work`, `cancel_active_tool_task`) — stops the
   in-flight lookup immediately. Best-effort: `task.cancel()` is not
   guaranteed to land before a task completes.
2. **Point-of-use fencing** (`is_stale` / `is_work_stale`, checked immediately
   before a result is used, never once at the start) — this is the actual
   correctness guarantee, and it holds even when cancellation loses the race.
   It runs in `llm_node` per chunk, in `tts_node` per audio frame, and in the
   tool's success *and* timeout paths.
3. **State reconciliation** (`playback.PlaybackLedger`) — rewrites the last
   assistant message down to what was actually played, so later turns never
   refer back to a sentence the user never heard.

The `tts_node` fence is what stops audio that is *already synthesized*: by the
time the turn advances, several sentences may be sitting in the TTS stream,
and the LLM-level fence cannot reach them.

### Measured — offline, 20 trials with a 4s tool delay

| Metric | Result |
|---|---|
| Stale results leaked | **0 / 20** |
| Tool-cancellation latency (logic only), p50 | 0.0 ms |
| Tool-cancellation latency (logic only), p95 | 16.0 ms |
| Post-interrupt history truncated to played fraction | yes (25% of the utterance in the fixture) |
| Truncation is a clean prefix, no partial words | yes |

Tool-cancellation latency measures how fast this project's own logic reacts.
**It is not the "Rime audio stops within X ms" number** — that requires a live
run, where `interrupt_detected → audio_stopped` is recorded in
`live_session_metrics.json`.

Unit coverage: `test_turn_manager.py` (fencing and the
cancellation-lands-too-late race), `test_playback.py` (truncation, including
the live-path case where un-played frames are never counted).

### Honest limits

- The truncation cut point is an **estimate**. The exact point depends on
  client-side buffering the agent process cannot observe, and text does not
  map to audio duration uniformly. It is deliberately biased to under-report:
  claiming the user heard something they did not is the failure that actually
  corrupts a conversation, so with no playback accounting at all it records
  nothing as heard. `truncation_confidence()` reports `exact` / `estimated` /
  `unknown` per utterance, and the confidence goes into the metrics log.
- History reconciliation depends on LiveKit's chat-context API, which has
  moved between releases. On failure it degrades to the previous behaviour
  (history keeps the full text) and says so in the log rather than throwing.
- Related upstream issue: [`livekit/agents#3702`](https://github.com/livekit/agents/issues/3702)
  — tool-call results can be lost or mishandled during interruption in some
  framework versions. The fencing here is defense-in-depth for exactly that
  class of bug rather than trust in the framework's own handling.

---

## Claim C — Continuity during tool work

**Claim.** During a slow tool call the user can ask for status, add a
constraint, back-channel, or cancel — without losing the work in flight, and
without a delayed result being applied to the wrong conversational state.

### Mechanism

A mid-tool utterance is classified *before* any turn bookkeeping runs, because
whether the work survives depends on what the user actually said:

| Utterance | Intent | In-flight work | Reply |
|---|---|---|---|
| "are you still there?", "any luck?" | `STATUS_REQUEST` | kept, carried forward | "Still checking order 1002 — about 3 seconds in." |
| "while you're at it check the refund" | `CONSTRAINT` | kept, constraint attached to the result | "Got it — … Still checking order 1002." |
| "mhm", "ok" | `AFFIRMATION` | kept | none (talking over a backchannel is worse than silence) |
| "never mind", "forget it" | `CANCEL` | cancelled and fenced | "Okay, I've stopped checking order 1002." |
| anything else | `NEW_REQUEST` | cancelled and fenced | normal LLM turn |

Work that survives is **carried forward** — its turn stamp is advanced to the
new turn id, so the point-of-use fence still passes. This is the single,
narrow exception to "an old stamp means stale". It only ever applies to work
the user explicitly asked to keep, and every use is written to the audit log,
so if carried-forward work were ever spoken out of context the log names the
utterance that authorized it.

Every branch still advances the turn id and still stops playback — the user is
talking, so the agent stops, and stale *LLM generation* is fenced either way.
What differs is only the fate of the tool work.

### Measured — offline (`suite C` of `acceptance_test.py`)

| Utterance | Intent | Work kept | Result delivered | Routing cost |
|---|---|---|---|---|
| "are you still there?" | status_request | yes | yes | 0.23 ms |
| "any luck?" | status_request | yes | yes | 0.11 ms |
| "while you're at it check the refund too" | constraint | yes | yes (constraint applied) | 0.27 ms |
| "mhm" | affirmation | yes | yes | 0.10 ms |
| "never mind" | cancel | no | **never** | 0.16 ms |
| "actually, what's your return policy?" | new_request | no | **never** | 0.25 ms |

"Result delivered = no" is verified by awaiting the task and requiring a
`StaleResultError` or `CancelledError` — the result does not merely go unused,
it cannot be produced.

### Honest limits

- Classification is regex-based. It will misread phrasings the patterns do not
  cover. The failure is designed to be one-directional: anything
  unrecognised falls through to `NEW_REQUEST`, so the worst case is a lookup
  discarded and re-run, never a stale result spoken. `test_continuity.py`
  pins that fallback explicitly, and pins that "stop, actually use the other
  one" resolves to CANCEL rather than CONSTRAINT.
- An earlier version of the constraint patterns matched a bare "actually",
  which swallowed "actually, what's your return policy?" as a constraint and
  kept work the user had abandoned. The patterns now require a phrase that
  names the work; the regression is covered by a test.
- Constraints are attached to the result as a note for the model, not parsed
  into structured backend parameters. For this demo's fake order lookup there
  is nothing to re-parameterize; a real backend would need the constraint
  mapped to actual request fields.

---

## Test inventory

| File | Covers |
|---|---|
| `test_turn_manager.py` | turn ids, stamping, point-of-use fencing, cancel-lands-too-late race, audit log |
| `test_latency.py` | stage attribution, percentiles, aborted-turn exclusion, filler-vs-TTFA separation, GapCover behaviour |
| `test_playback.py` | truncation to played audio, word boundaries, under-report bias, live-path denominator |
| `test_continuity.py` | intent classification, carry-forward, cancellation, and the async path end-to-end |
| `acceptance_test.py` | all three claims as pass/fail gates with exported evidence |
