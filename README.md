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

## Architecture

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
| [metrics.py](metrics.py) | Event log, exported as evidence |
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

## Tech stack

| Layer | Choice |
|---|---|
| Transport / orchestration | LiveKit Agents (Python) |
| TTS (primary spoken output) | Rime, via `livekit-plugins-rime`, `use_websocket=True` |
| STT | Deepgram, streaming (`nova-3`) |
| VAD | Silero (preloaded in `prewarm_fnc`) |
| LLM | Groq — `openai/gpt-oss-20b` (see Known limitations) |

**Rime configuration used in testing:** model `mistv2`, speaker `cove`,
websocket transport, 48 kHz. *(Verify against Rime's live catalog before the
final demo — this may change.)*

## Setup

1. Install dependencies:
   ```bash
   pip install livekit-agents livekit-plugins-rime livekit-plugins-deepgram \
               livekit-plugins-silero livekit-plugins-openai python-dotenv \
               pytest pytest-asyncio
   ```
2. Copy `.env.example` to `.env` and fill in real credentials (LiveKit, Rime,
   Deepgram, Groq). Never commit `.env`.
3. Run the tests — these need no credentials and no LiveKit install:
   ```bash
   pytest -q                                  # 73 tests
   python acceptance_test.py --trials 20      # exits non-zero on failure
   ```
4. Run the agent:
   ```bash
   python agent.py dev
   # or, to exercise the interruption/continuity paths on demand:
   STRESS_TEST_TOOL_DELAY_MS=4000 python agent.py dev
   ```
   On shutdown it writes `live_session_latency.json` and
   `live_session_metrics.json` and prints a latency summary.
5. Run the frontend (in `agent-starter-react/`):
   ```bash
   npm install && npm run dev
   ```
   Copy `.env.example` to `.env.local` there with the same LiveKit
   credentials, then open `localhost:3000`, connect, and talk.

### Things worth trying once connected

- Ask about order 1002 with `STRESS_TEST_TOOL_DELAY_MS=4000`, then say **"are
  you still there?"** — you get a status answer and the lookup still completes.
- Same setup, then say **"actually, what's your return policy?"** — the lookup
  is killed and its result is never spoken.
- Interrupt a long answer halfway, then ask **"what did you just say?"** — the
  agent only knows the part you actually heard.

## Known limitations

- **No verified end-to-end TTFA number yet.** The pipeline is fully
  instrumented and a live run emits real p50/p95 plus a per-stage breakdown,
  but that run has not been recorded into RIME_EVIDENCE.md. The offline
  numbers there are logic timings and are labelled as such — they are not a
  substitute.
- **Offline tests cannot prove the audio half of any claim.** No audio engine,
  no room, no Rime connection in that process. This is stated in the script's
  output as well as its docstring.
- **Post-interrupt truncation is an estimate.** Client-side buffering is not
  observable from the agent process. It is biased to under-report, and each
  utterance carries a confidence of `exact` / `estimated` / `unknown`.
- **Intent classification is regex-based.** It will misread phrasings the
  patterns don't cover; unrecognised input falls through to "new request", so
  the failure mode is a discarded lookup, not a stale answer.
- **Cancellation is best-effort, not authoritative.** `task.cancel()` is not
  guaranteed to land before a task completes. The design assumes it will
  sometimes fail and relies on the point-of-use check as the real guarantee.
- **Groq model selection is a live tradeoff.** `llama-3.3-70b-versatile` was
  deprecated by Groq. Currently `openai/gpt-oss-20b` for speed;
  `openai/gpt-oss-120b` is the fallback if answer quality needs to improve at
  the cost of latency.
- **History reconciliation depends on a moving API.** LiveKit's chat-context
  API has changed across releases; on failure this degrades to keeping the
  full text and logs a warning rather than breaking the session.
- **No TTS fallback provider.** If Rime is unavailable the agent has no
  automatic fallback.

## Status

Bare voice loop working end-to-end in a real LiveKit room. All three
directions are implemented and integrated: latency instrumentation with a
per-stage breakdown and gap covering; interruption fencing at the LLM node,
the TTS node and both tool paths, plus playback-accurate state recovery; and
intent-routed continuity during tool work. 73 unit tests and a three-suite
acceptance test pass. The outstanding work is recording a live run's real
latency and audio-stop numbers into RIME_EVIDENCE.md.
