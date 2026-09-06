# Rime

Realtime voice-agent **orchestration** for a hackathon build: conversation continuity, user interruption, request invalidation, cancellation, and stale-result fencing.

The LLM is not the product. The product is a state machine that stays correct when the user talks over the agent, tools run long, cancel fails, and old results arrive late.

## Why interruption is hard

If the user says “Find hotels in Mumbai” and two seconds later “Actually under 5000”:

- a slow search may still be running
- the agent may already be speaking
- cancel may not stop the first tool
- the first result must never become the answer
- Mumbai must not be forgotten

Request IDs + generation IDs + cancellation + fencing + context merge are what make that safe.

## Architecture

```
Frontend → WebSocket → Realtime adapter → Orchestrator
                                             ↓
                              LLM provider (Gemini or mock)
                                             ↓
                                           Tools
                                             ↓
                                      ResultValidator
                                             ↓
                                    TTS adapter (Rime or mock)
```

The Orchestrator is the only owner of conversation state. LiveKit, Rime, Gemini, tools, and the UI emit events; they do not keep a second “current answer”.

Details: `docs/ARCHITECTURE.md`.

## Installation

Python 3.11+.

```bash
cd /path/to/RIME
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment setup

```bash
cp .env.example .env
```

Do not put real secrets in git. `.env` is gitignored.

```
LLM_PROVIDER=mock
TTS_PROVIDER=mock
REALTIME_PROVIDER=mock
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
```

## Mock mode

Default. The backend, tests, and demo run without Gemini, LiveKit, Rime, or real search APIs.

If `LLM_PROVIDER=gemini` but `GEMINI_API_KEY` is missing, the process falls back to the mock LLM.

## Running tests

```bash
pytest -q
```

## Running the demo

```bash
python -m backend.demo.interruption_demo
```

This prints a deterministic timeline: request A, interrupt, request B, B accepted, A discarded.

## Running the API server

```bash
uvicorn backend.app.main:app --reload
```

Check:

```bash
curl http://127.0.0.1:8000/health
```

WebSocket:

```
ws://127.0.0.1:8000/ws/conversation/{conversation_id}
```

## Team integration points

| Person | Surface |
|---|---|
| Atharva | Orchestrator, state, fencing, tools, contracts |
| Nikunj | LiveKit adapter (`RealtimeInputAdapter`) |
| Smera | Rime adapter (`TTSProvider`) |
| Prisha | WebSocket client + status UI |

See `docs/INTEGRATION_GUIDE.md`.

## How to plug in LiveKit

Implement `RealtimeInputAdapter` in `backend/app/adapters/livekit.py`. Convert `USER_SPEECH_STARTED` / `USER_TRANSCRIPT_FINAL` / `USER_INTERRUPTED` into `handle_user_message` and `handle_interrupt`. Do not pass room or audio objects into the orchestrator.

## How to plug in Rime

Implement `TTSProvider` in `backend/app/adapters/rime.py`. Honor `stop()` immediately on interruption. Subscribe to `tts_start` / `tts_stop` for evidence.

## How the frontend connects

Open `/ws/conversation/{conversation_id}` and send `user_message` / `interrupt`. Display `state_updated`, `stale_result_discarded`, and `tts_stop`. Protocol: `docs/EVENT_PROTOCOL.md`.
