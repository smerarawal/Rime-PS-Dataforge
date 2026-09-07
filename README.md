# Rime PS — DataForge

A voice-native order-status / customer support agent built for the Rime
hackathon challenge. Rime provides all spoken output. The engineering work
targets three things that decide whether a voice agent feels usable:

1. **Perceived response time** — how long after the user stops talking do they
   hear anything, and which stage is to blame when that number is bad.
2. **Interruption and recovery** — when the user talks over the agent, audio
   stops, in-flight work is fenced so it can never be spoken late, and the
   conversation state is corrected to what the user actually heard.
3. **Continuity during tool work** — during a slow backend call the user can
   ask for status, add a constraint, back-channel, or cancel, without the work
   being thrown away and without a delayed result landing in the wrong
   conversational state.

Measurements, and an explicit account of what is *not* measured, are in
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## What is in this repository

Three workstreams were developed on separate branches and are now merged into
one tree. They overlap deliberately — two of them solve the interruption
problem independently, at different layers.

| Path | What it is | Runs as |
|---|---|---|
| `agent.py`, `turn_manager.py`, `continuity.py`, `latency.py`, `playback.py`, `tools.py` | **The LiveKit voice agent.** Real microphone-to-Rime pipeline with turn fencing, latency instrumentation, and intent-routed continuity. | `python agent.py dev` |
| `backend/app/**` | **The orchestrator service.** A transport-independent state machine for the same problem, with `(request_id, generation_id)` fencing, a Gemini/mock LLM, a raw Rime websocket adapter, and its own FastAPI + WebSocket API. | `uvicorn backend.app.main:app` |
| `metrics.py`, `metrics_ws_bridge.py`, `prisha-react/` | **Live observability.** Agent events pushed over a websocket to a React dashboard, so interruption behaviour is visible while you talk to it. | `npm run dev` in `prisha-react/` |
| `agent-starter-react/` | LiveKit's standard voice frontend — the thing you actually talk into. | `npm run dev` |

**On the overlap between the two runtimes:** `turn_manager.py` and
`backend/app/core/` are two answers to the same question, not one system in
two files. The agent tracks a monotonic **turn id**; the backend tracks a
**(request_id, generation_id)** pair with a central `ResultValidator`. Both
enforce the same rule — a result is checked against current state at the
point of use, never only when the work started. They are kept separate on
purpose: the agent's version has to live inside LiveKit's node callbacks,
the backend's has to survive its own transport. `docs/INTEGRATION_GUIDE.md`
and `docs/ARCHITECTURE.md` cover the backend's contracts.

## The problem this solves

When a user interrupts an agent mid-response (including while a backend tool
call is still running), most voice agents either keep talking over the user,
or stop the audio but still let the in-flight LLM/tool result "leak" back in
and get spoken a moment later, out of context.

This project treats every user utterance as a **turn** with a monotonically
increasing id. Any async work (LLM generation, tool calls, queued TTS) is
stamped with the turn id active when it started, and is checked **again, right
at the point of use** — not just once when the work began — against the
*current* turn id. If the turn has moved on, the result is discarded.

That was validated against a real bug found during development: without
fencing, an interrupted agent would pause briefly, then resume speaking a
stale answer to a question the user had already abandoned.

The second half of the problem is subtler. Not every interruption means "throw
everything away". "Are you still there?" during a four-second lookup is not a
new request — cancelling the lookup and starting over is the wrong response,
and so is ignoring the user. So an utterance is routed by intent *before* the
turn machinery decides the fate of the work in flight.

## Architecture — the voice agent

```
User speaks
  → Silero VAD (speech boundary detection; model preloaded in prewarm)
  → [latency clock starts at end-of-speech]
  → Deepgram STT, streaming (nova-3)
  → TurnManager.route_utterance() on each committed utterance
        ├── status / constraint / backchannel → work carried forward, answered now
        ├── cancel                            → work cancelled + fenced
        └── new request                       → work superseded + fenced
  → Groq LLM (streaming), fenced per chunk in Assistant.llm_node
  → Rime TTS (streaming websocket), fenced per audio frame in Assistant.tts_node
  → Playback  [latency clock stops at first audio frame]
       ↘ every step also emits a MetricsLog event → ws://localhost:8765 → dashboard

Interrupt path (can fire at any point):
  → VAD detects the user talking over the agent
  → route_utterance() decides what survives, then advances the turn id
  → in-flight LLM generation stops yielding on the next chunk
  → already-synthesized audio stops on the next frame (tts_node fence)
  → PlaybackLedger truncates the history to what was actually played
  → GapCover, if it was waiting to speak a filler, stands down
```

### Modules

| File | Responsibility |
|---|---|
| [turn_manager.py](turn_manager.py) | Turn ids, stamping, point-of-use staleness, the in-flight work registry, and utterance routing |
| [continuity.py](continuity.py) | Intent classification (status / constraint / cancel / backchannel / new request) and the `PendingWork` record |
| [latency.py](latency.py) | Per-stage TTFA timeline, p50/p95 aggregation, and `GapCover` |
| [playback.py](playback.py) | What was synthesized vs what was actually played; truncation to what the user heard |
| [metrics.py](metrics.py) | Event log with a realtime `subscribe()` hook, exported as evidence |
| [metrics_ws_bridge.py](metrics_ws_bridge.py) | Pushes those events to the dashboard over a websocket |
| [tools.py](tools.py) | The deliberately slow order lookup, fenced on both its success and failure paths |
| [agent.py](agent.py) | The live pipeline, wiring all of the above into LiveKit |

### Key mechanisms

- `is_stale(stamped_id)` — the critical check, called again immediately before
  a result is used, never only at the start.
- `route_utterance(text)` — classifies, then applies the turn bookkeeping the
  classification implies. Runs in well under a millisecond, with no model
  round-trip, because it sits on the interrupt path.
- `carry_forward(work)` — the one sanctioned way for work to survive a turn
  boundary, used only for utterances the user meant as "keep going", and
  written to the audit log every time.
- `PlaybackLedger.heard_text(turn_id)` — the post-interrupt reconstruction,
  biased to under-report rather than claim the user heard something they
  did not.

## Observability

`agent.py` starts the metrics bridge in-process, so runtime events are
observable without a second metrics process. Events are **pushed** as they are
recorded (`MetricsLog.subscribe`) rather than polled, and a client connecting
mid-session is sent the recent backlog first.

The dashboard in `prisha-react/` shows current turn, connection and
interaction state, interruption events, audio-stop latency, stale results
discarded vs leaked, and a turn-by-turn timeline with the assistant text for
each turn. Endpoint: `ws://localhost:8765`. Details in
[README_PRISHA.md](README_PRISHA.md).

Events currently emitted: `utterance_routed`, `interrupt_detected`,
`audio_stopped`, `llm_first_token`, `first_audio_frame`,
`stale_result_discarded`, `stale_audio_discarded`, `history_reconciled`,
`continuity_reply`, `assistant_item_added`.

## Tech stack

| Layer | Choice |
|---|---|
| Transport / orchestration | LiveKit Agents (Python) |
| TTS (primary spoken output) | Rime, via `livekit-plugins-rime`, `use_websocket=True` |
| STT | Deepgram, streaming (`nova-3`) |
| VAD | Silero (preloaded in `prewarm_fnc`) |
| LLM | Groq — `openai/gpt-oss-20b` (see Known limitations) |
| Orchestrator service | FastAPI + pydantic, Gemini or mock LLM, raw Rime websocket adapter |
| Dashboards | React + Vite (`prisha-react`), Next.js (`agent-starter-react`) |

**Rime configuration used in testing:** model `mistv2`, speaker `cove`,
websocket transport, 48 kHz in the agent / 24 kHz in the backend adapter.
*(Verify against Rime's live catalog before the final demo — this may change.)*

## Setup

1. Python dependencies:
   ```bash
   pip install livekit-agents livekit-plugins-rime livekit-plugins-deepgram \
               livekit-plugins-silero livekit-plugins-openai python-dotenv \
               websockets pytest pytest-asyncio
   pip install -r requirements.txt     # backend service only
   ```
2. Copy `.env.example` to `.env` and fill in the section for whichever runtime
   you are starting. Never commit `.env`.
3. Run the tests — these need no credentials:
   ```bash
   pytest -q                                  # agent suite
   pytest backend/tests -q                    # orchestrator suite
   python acceptance_test.py --trials 20      # exits non-zero on failure
   ```
4. Run the voice agent:
   ```bash
   python agent.py dev
   # or, to exercise the interruption/continuity paths on demand:
   STRESS_TEST_TOOL_DELAY_MS=4000 python agent.py dev
   ```
   Verified against livekit-agents 1.8.0, which prints a deprecation notice
   for `dev` and points at `lk agent dev` (LiveKit CLI) for hot-reload.
   `python agent.py dev` still works; `python agent.py start` is the
   production form.
   On shutdown it writes `live_session_latency.json` and
   `live_session_metrics.json` and prints a latency summary.
5. Run the voice frontend (what you talk into):
   ```bash
   cd agent-starter-react && npm install && npm run dev     # localhost:3000
   ```
   Copy its `.env.example` to `.env.local` with the same LiveKit credentials.
6. Run the observability dashboard, alongside the agent:
   ```bash
   cd prisha-react && npm install && npm run dev
   ```
7. Run the orchestrator service, if you want that runtime:
   ```bash
   uvicorn backend.app.main:app --reload
   python -m backend.demo.interruption_demo
   ```

### Things worth trying once connected

- Ask about order 1002 with `STRESS_TEST_TOOL_DELAY_MS=4000`, then say **"are
  you still there?"** — you get a status answer and the lookup still completes.
- Same setup, then say **"actually, what's your return policy?"** — the lookup
  is killed and its result is never spoken.
- Interrupt a long answer halfway, then ask **"what did you just say?"** — the
  agent only knows the part you actually heard.
- Watch the dashboard while doing any of the above.

## Known limitations

- **No verified end-to-end TTFA number yet.** The pipeline is fully
  instrumented and a live run emits real p50/p95 plus a per-stage breakdown,
  but that run has not been recorded into RIME_EVIDENCE.md. The offline
  numbers there are logic timings and are labelled as such.
- **Two overlapping implementations.** The agent's `TurnManager` and the
  backend's `RequestManager`/`ResultValidator` both solve interruption
  correctness. They are merged into one repository, not into one runtime.
  Consolidating them is a real decision that has not been made.
- **Offline tests cannot prove the audio half of any claim.** No audio engine,
  no room, no Rime connection in that process.
- **Post-interrupt truncation is an estimate.** Client-side buffering is not
  observable from the agent process. It is biased to under-report, and each
  utterance carries a confidence of `exact` / `estimated` / `unknown`.
- **Intent classification is regex-based.** Unrecognised input falls through to
  "new request", so the failure mode is a discarded lookup, not a stale answer.
- **Cancellation is best-effort, not authoritative.** `task.cancel()` is not
  guaranteed to land before a task completes; the point-of-use check is the
  real guarantee.
- **Groq model selection is a live tradeoff.** Currently `openai/gpt-oss-20b`
  for speed; `openai/gpt-oss-120b` is the fallback if answer quality needs to
  improve at the cost of latency.
- **History reconciliation depends on a moving API.** On failure it degrades to
  keeping the full text and logs a warning rather than breaking the session.
- **No TTS fallback provider** in the agent path if Rime is unavailable.

## Status

The voice loop works end-to-end in a real LiveKit room. All three directions
are implemented and integrated: latency instrumentation with a per-stage
breakdown and gap covering; interruption fencing at the LLM node, the TTS node
and both tool paths, plus playback-accurate state recovery; and intent-routed
continuity during tool work. The orchestrator service and the observability
dashboard are merged in from their branches. Outstanding: record a live run's
real latency and audio-stop numbers into RIME_EVIDENCE.md.
