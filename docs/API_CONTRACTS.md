# API Contracts

These interfaces are the only integration surface. Do not import orchestrator internals from LiveKit, Rime, or the frontend.

## LLM provider

```python
class LLMProvider(ABC):
    async def analyze_intent(
        self,
        user_input: str,
        *,
        current_parameters: dict,
        current_intent: str | None,
        conversation_status: str,
        current_request_id: str | None = None,
    ) -> IntentAnalysis
```

`IntentAnalysis` is validated Pydantic:

- `intent`
- `parameters`
- `is_follow_up`
- `is_interruption`
- `target_request`
- `requested_action`: `execute_tool` | `respond` | `cancel`
- `tool_name`
- `confidence`

Free-form model text must never drive control flow. Implement Gemini in `backend/app/llm/gemini.py` only.

Environment:

- `LLM_PROVIDER=mock|gemini`
- `GEMINI_API_KEY=`
- `GEMINI_MODEL=gemini-2.5-flash`

Missing Gemini credentials fall back to mock mode.

## Tool provider

```python
class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict

    async def execute(
        self,
        parameters: dict,
        token: CancellationToken | None = None,
        *,
        request_id: str,
        generation_id: str,
        task_id: str | None = None,
    ) -> ToolResult
```

Tools return data. They do not decide whether that data is current. Fencing happens in `ResultValidator`.

## TTS provider

```python
class TTSProvider(ABC):
    async def speak(self, text: str, *, request_id: str, generation_id: str, conversation_id: str) -> None
    async def stop(self) -> None
    async def stream(self, chunks: AsyncIterator[str], *, request_id: str, generation_id: str, conversation_id: str) -> None
```

The orchestrator emits `tts_start`, `tts_stop`, and `assistant_response_ready`. On interruption, `tts_stop` is emitted before obsolete speech may continue.

## Realtime provider

```python
class RealtimeInputAdapter(ABC):
    async def on_user_speech_started(self) -> None
    async def on_user_speech_stopped(self) -> None
    async def on_user_transcript_final(self, text: str) -> None
    async def on_user_interrupted(self) -> None
```

LiveKit room objects, audio frames, and WebRTC stay inside this adapter. The orchestrator sees only typed application events and `handle_user_message` / `handle_interrupt`.

## Frontend protocol

WebSocket: `/ws/conversation/{conversation_id}`

Client → server:

```json
{"type": "user_message", "text": "Find hotels in Mumbai"}
{"type": "interrupt"}
```

Server → client: see `docs/EVENT_PROTOCOL.md`.

HTTP:

- `GET /health`
- `GET /conversations/{conversation_id}/state` (after a session exists)

Secrets are never included in WebSocket payloads.
