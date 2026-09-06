# Testing

Tests do not call Gemini, LiveKit, Rime, or real hotel APIs.

## Run

From the repository root, with the virtualenv activated:

```bash
pytest -q
```

`asyncio_mode = auto` is configured in `pyproject.toml`.

## What the suite proves

| File | Coverage |
|---|---|
| `test_state.py` | Serializable conversation state, snapshot isolation |
| `test_request_manager.py` | IDs, invalidation, cancel/complete, concurrent creates |
| `test_cancellation.py` | Task cancel, request marked cancelled/obsolete |
| `test_fencing.py` | Older result discarded when a newer request finished first |
| `test_context_preservation.py` | Mumbai + “under 5000” keeps the city |
| `test_race_conditions.py` | Cancel failure, rapid interrupts, out-of-order tools |
| `test_orchestrator.py` | TTS stop, full interrupt flow, duplicate interrupts |
| `test_websocket_events.py` | `/health`, WS user/interrupt protocol |

## Race cases

1. **Basic fencing** — A starts, B starts, B finishes first, A discarded.
2. **Cancellation** — B supersedes A; A is marked cancelled/obsolete.
3. **Cancellation failure** — A ignores cancel, still returns, fence discards A.
4. **Context** — city survives a budget-only follow-up.
5. **Rapid interruptions** — only the last generation delivers a final answer.
6. **Out-of-order tools** — only the current generation is accepted.
7. **TTS interrupt** — `tts_stop` fires; no stale final speech for the old request.
8. **Full flow** — Mumbai search interrupted by “under 5000”.

## Demo

```bash
python -m backend.demo.interruption_demo
```
