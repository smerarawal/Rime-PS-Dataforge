# Architecture

Rime is a realtime voice agent whose core innovation is **not** the LLM. It is the orchestration engine that keeps conversation state correct when the user interrupts, tools run long, cancellation fails, and results arrive out of order.

## Source of truth

Conversation state has one owner: the **Orchestrator**.

Gemini, LiveKit, Rime, tools, and the frontend may emit events. They must not keep a competing current-request or current-answer.

```
Frontend
   ↓
WebSocket
   ↓
Realtime Adapter (LiveKit later)
   ↓
Orchestrator
   ↓
Gemini (via LLMProvider)
   ↓
Tools
   ↓
ResultValidator  ← correctness fence
   ↓
TTS Adapter (Rime later)
   ↓
User
```

Cancellation is an optimization. **Fencing is the correctness guarantee.** An old tool result can never become the current answer, even if `asyncio.Task.cancel()` fails.

## Responsibility map

| Module | Responsibility |
|---|---|
| `ConversationStore` / `ConversationState` | Serializable conversation snapshot only |
| `RequestManager` | Request IDs, generation IDs, current/obsolete lifecycle |
| `TaskManager` | asyncio task create / cancel / status |
| `ContextManager` | Parameter merge; independent of Gemini |
| `ResultValidator` | Central stale-result fence |
| `LLMProvider` | Structured `IntentAnalysis` only |
| `ToolRegistry` / `BaseTool` | Tool lookup and execution |
| `TTSProvider` | Speech I/O behind an interface |
| `RealtimeInputAdapter` | LiveKit/VAD/STT → application events |
| `EventBus` | Fan-out of typed `AppEvent`s |
| `Orchestrator` | Coordinates the above; does not implement them |

## Identity model

Every unit of work carries `(request_id, generation_id)`.

1. A new user instruction creates a new request and a new generation.
2. The previous request is marked obsolete immediately.
3. Background tasks for the old generation are asked to cancel.
4. When any result returns, `ResultValidator.validate(request_id, generation_id)` decides whether it may touch state, the LLM, TTS, or the frontend.

## Context merge

If the user says “Find hotels in Mumbai” and later “Actually under 5000”, `ContextManager` overlays the new budget on the existing city. Gemini may propose parameters; it does not write conversation state.

## Mock mode

Default providers are mocks. The engine, tests, and demo run without Gemini, LiveKit, Rime, or real hotel APIs.
