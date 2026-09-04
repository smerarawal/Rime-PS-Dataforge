# Rime PS — DataForge

A voice-native order-status / customer support agent built for the Rime hackathon challenge. Rime provides all spoken output; the core engineering problem solved is **turn correctness under interruption** — the agent must never speak a result that belongs to a conversational turn the user has already moved past.

## The problem this solves

When a user interrupts an agent mid-response (including while a backend tool call is still running), most voice agents either:
- keep talking over the user, or
- stop the audio but still let the in-flight LLM/tool result "leak" back in and get spoken a moment later, out of context.

This project treats every user utterance as a **turn** with a monotonically increasing id. Any async work (LLM generation, tool calls, queued TTS) is stamped with the turn id active when it started. Before that work is allowed to reach the user, it is checked **again, right at the point of use** — not just once when the work began — against the *current* turn id. If the turn has moved on, the result is discarded.

This was validated against a real bug encountered during development: without this fencing, an interrupted agent would pause briefly, then resume speaking a stale answer to a question the user had already abandoned.

## Architecture

```
User speaks
  → Silero VAD (speech boundary detection)
  → Deepgram STT, streaming (nova-3)
  → TurnManager.start_new_turn() on each committed user utterance
  → Groq LLM (streaming), wrapped by TurnManager point-of-use staleness check
  → Rime TTS (streaming via websocket)
  → Playback

Interrupt path (can fire at any point):
  → VAD detects user talking over the agent
  → start_new_turn() invalidates the previous turn id
  → In-flight LLM generation stops yielding chunks as soon as staleness is
    detected (checked per-chunk in Assistant.llm_node)
  → Queued/playing Rime audio stops (LiveKit default barge-in behavior)
```

Core mechanism lives in `turn_manager.py`:
- `start_new_turn()` — increments the current turn id
- `stamp()` — captures the turn id active when a piece of work starts
- `is_stale(stamped_id)` — the critical check, called again right before a result is used, not just at start
- `guard(coro, stamped_id)` — async wrapper, raises `StaleResultError` on stale results
- `cancel_and_fence(task, stamped_id)` — best-effort `task.cancel()` with a staleness fallback, since cancellation alone cannot be trusted to land in time
- `audit_log()` — records every turn/discard event, used as reproducibility evidence

## Tech stack

| Layer | Choice |
|---|---|
| Transport / orchestration | LiveKit Agents (Python) |
| TTS (primary spoken output) | Rime, via `livekit-plugins-rime`, `use_websocket=True` |
| STT | Deepgram, streaming (`nova-3`) |
| VAD | Silero (auto-provisioned by LiveKit) |
| LLM | Groq — `openai/gpt-oss-20b` (see Known Limitations re: model selection) |

**Exact Rime configuration used in testing:** model `mistv2`, speaker `cove`, websocket transport, default audio format. *(Verify against Rime's live catalog before final submission/demo — this may change.)*

## Setup

1. Install dependencies:
   ```bash
   pip install livekit-agents livekit-plugins-rime livekit-plugins-deepgram livekit-plugins-silero livekit-plugins-openai python-dotenv pytest pytest-asyncio
   ```
2. Copy `.env.example` to `.env` and fill in real credentials (LiveKit, Rime, Deepgram, Groq). Never commit `.env`.
3. Run the turn manager tests:
   ```bash
   pytest test_turn_manager.py -v
   ```
4. Run the agent:
   ```bash
   python agent.py dev
   ```
5. Run the frontend (in `agent-starter-react/`):
   ```bash
   npm install
   npm run dev
   ```
   Copy `.env.example` to `.env.local` in that folder with the same LiveKit credentials.
6. Open `localhost:3000`, connect, and talk.

## Known limitations

- **Groq model selection is a live tradeoff.** `llama-3.3-70b-versatile` was deprecated by Groq and is no longer available. Currently using `openai/gpt-oss-20b` for speed; `openai/gpt-oss-120b` is a fallback if answer quality needs to improve at the cost of latency. This choice is not yet finalized.
- **LiveKit Agents issue [`livekit/agents#3702`](https://github.com/livekit/agents/issues/3702)**: tool call results can be lost or mishandled during interruption in some versions of the framework. The custom `turn_manager.py` fencing layer exists as defense-in-depth for exactly this class of bug, rather than relying solely on LiveKit's built-in interruption handling.
- **Cancellation is best-effort, not authoritative.** `task.cancel()` is not guaranteed to land before a task completes. The design assumes cancellation will sometimes fail and relies on the point-of-use staleness check (`is_stale()`, called immediately before a result is spoken or used) as the actual correctness guarantee.
- **Tool-call cancellation path is still being hardened.** An intermittent case was identified where fencing correctly marks a result stale, but the result still surfaced due to a staleness check that ran too early rather than immediately before use — this is being fixed by moving all staleness checks to point-of-use across every code path (main and fallback/error-handling branches).
- **Latency has not yet been formally measured against an acceptance threshold.** Informal testing in a real LiveKit room shows perceptible-but-workable latency; formal p50/p95 measurement across N trials is planned but not yet implemented (see `RIME_EVIDENCE.md`, not yet written).

## Failure behavior

- If Rime TTS is unavailable, the agent currently has no automatic fallback provider — this should be added and disclosed before submission if implemented.
- If a tool call fails or times out, the failure path currently does not yet run through the same staleness-fencing as the success path — this is a known gap, not a solved case.

## Status

Bare voice loop (VAD → STT → LLM → TTS → playback) is working end-to-end in a real LiveKit room. Turn-based staleness fencing is implemented and unit-tested but not yet stress-tested against a deliberately slow tool call. Stress-test script and `RIME_EVIDENCE.md` are not yet built.
