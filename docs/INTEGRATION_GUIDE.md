# Integration Guide

The orchestration engine is ready in mock mode. Each teammate plugs in behind a contract.

## FOR NIKUNJ

How to connect LiveKit / VAD / STT / interruption events.

1. Keep WebRTC, room objects, audio frames, and microphone code out of `backend/app/core`.
2. Implement `RealtimeInputAdapter` in `backend/app/adapters/livekit.py` (replace or subclass `MockRealtimeInputAdapter`).
3. Map LiveKit events to adapter methods:

   | LiveKit / VAD / STT signal | Adapter method | Orchestrator effect |
   |---|---|---|
   | user started speaking | `on_user_speech_started()` | optional `user_turn` phase event |
   | user stopped speaking | `on_user_speech_stopped()` | local only |
   | final transcript | `on_user_transcript_final(text)` | `handle_user_message(text)` |
   | barge-in / interruption | `on_user_interrupted()` | `handle_interrupt()` |

4. If the user is speaking while the agent is speaking, call `on_user_interrupted()` **immediately**, then send the final transcript when STT completes.
5. Do not store current request IDs or conversation parameters in the LiveKit layer.
6. Set `REALTIME_PROVIDER=livekit` when the real adapter is wired. Until then, mock mode is the default.

The adapter already documents a `translate()` helper for raw LiveKit event names:

- `USER_SPEECH_STARTED`
- `USER_SPEECH_STOPPED`
- `USER_TRANSCRIPT_FINAL`
- `USER_INTERRUPTED`

## FOR SMERA

How to implement Rime behind the TTS interface.

1. Implement `TTSProvider` in `backend/app/adapters/rime.py`.
2. Required methods: `speak`, `stop`, `stream`.
3. The orchestrator already emits:

   - `assistant_response_ready` with the speakable `text`
   - `tts_start` immediately before `speak()`
   - `tts_stop` on interruption, **before** obsolete content may continue

4. `stop()` must be idempotent and fast. This is the interruption evidence hook.
5. Tag every utterance with `request_id` and `generation_id`. If a stream is in flight and those IDs are no longer current, drop remaining audio.
6. Do not ask the orchestrator to own Rime credentials or audio buffers.
7. Evaluation / Rime evidence should subscribe to `EventBus` (`tts_start`, `tts_stop`, `interruption`) and measure real timestamps. `LatencyTracker` only records actual event times.
8. Set `TTS_PROVIDER=rime` when the real adapter is ready. `MockTTSProvider` remains for tests.

## FOR PRISHA

How to consume WebSocket events and display state.

1. Connect to `ws://<host>/ws/conversation/{conversation_id}`.
2. Send:

   ```json
   {"type": "user_message", "text": "Find hotels in Mumbai"}
   {"type": "interrupt"}
   ```

3. Render at least:

   - current status (`state_updated.state.status`)
   - current parameters (city, budget)
   - current `request_id` / `generation_id`
   - live event stream: `task_started`, `request_invalidated`, `stale_result_discarded`, `tts_stop`
   - latest `assistant_response_ready.text`

4. `stale_result_discarded` should show as a discarded/obsolete chip, not as an error toast.
5. On user barge-in, send `interrupt` immediately; do not wait for the next assistant sentence.
6. Use `GET /health` for backend readiness. Use `GET /conversations/{id}/state` only after a socket session exists.
7. Never display or request API keys. The backend will not send secrets over the socket.

See `docs/EVENT_PROTOCOL.md` for the full payload list.
