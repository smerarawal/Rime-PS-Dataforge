# Event Protocol

All internal events are `AppEvent` models. The WebSocket protocol uses the same `type` strings in snake_case.

## Envelope

```json
{
  "type": "request_created",
  "event_id": "evt_...",
  "timestamp": "2026-09-06T06:30:00+00:00",
  "conversation_id": "conv_...",
  "request_id": "req_...",
  "generation_id": "gen_...",
  "...payload": {}
}
```

## Client → server

| type | fields | orchestrator call |
|---|---|---|
| `user_message` | `text` | `handle_user_message` |
| `interrupt` | optional `reason` | `handle_interrupt` |

Optional `event_id` on client messages is treated as an idempotency key.

## Server → client

| type | meaning | notable payload |
|---|---|---|
| `user_turn` | user text accepted | `text`, `source` |
| `interruption` | barge-in / supersede | `reason` |
| `request_created` | new request + generation | `intent`, `parameters`, `sequence_number` |
| `request_invalidated` | previous request obsolete | `reason` |
| `task_started` | tool task launched | `task_id`, `tool_name` |
| `task_cancelled` | cancel requested | `task_id` |
| `task_completed` | tool finished (may still be stale) | `task_id`, `ok` |
| `stale_result_discarded` | fence rejected a result | `reason` |
| `result_accepted` | fence accepted a result | `task_id` |
| `assistant_thinking` | model/tool planning | |
| `assistant_response_ready` | speakable text | `text` |
| `tts_start` | speech starting | `text`, `kind` |
| `tts_stop` | speech stopped | `reason` |
| `state_updated` | full conversation snapshot | `state` |
| `error` | recoverable failure | `error`, `detail` |

`stale_result_discarded` is a normal lifecycle event, not an application crash.

## State object

`state_updated.state` matches `ConversationState`:

- `conversation_id`
- `current_request_id`
- `generation_id`
- `current_intent`
- `current_parameters`
- `previous_parameters`
- `active_task_ids`
- `status`
- `last_user_message`
- `last_assistant_message`
- `interruption_count`
- `timestamps`

Statuses: `IDLE`, `LISTENING`, `THINKING`, `EXECUTING`, `SPEAKING`, `INTERRUPTED`, `CANCELLING`, `COMPLETED`, `ERROR`.
