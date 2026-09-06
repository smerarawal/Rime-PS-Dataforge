"""WebSocket protocol endpoint for the frontend."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.app.adapters.frontend import FrontendEventAdapter
from backend.app.core.errors import InvalidRequestError
from backend.app.utils.logging import get_logger, log_operation

logger = get_logger(__name__)
router = APIRouter()
frontend = FrontendEventAdapter()


@router.websocket("/ws/conversation/{conversation_id}")
async def conversation_socket(websocket: WebSocket, conversation_id: str) -> None:
    await websocket.accept()
    orchestrator = websocket.app.state.sessions.get_or_create(conversation_id)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _forward(event: Any) -> None:
        await queue.put(frontend.to_message(event))

    unsubscribe = orchestrator.events.subscribe(_forward)
    log_operation(
        logger,
        "websocket_connected",
        conversation_id=conversation_id,
    )

    async def _pump() -> None:
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    pump = asyncio.create_task(_pump())
    try:
        await websocket.send_json(
            {
                "type": "state_updated",
                "conversation_id": conversation_id,
                "state": orchestrator.get_state().to_json_dict(),
            }
        )
        while True:
            raw = await websocket.receive_json()
            if not isinstance(raw, dict):
                await websocket.send_json({"type": "error", "error": "invalid_request"})
                continue
            event_id = raw.get("event_id")
            if event_id and not orchestrator.remember_event(str(event_id)):
                continue
            parsed = frontend.parse_client_message(raw)
            try:
                if parsed["action"] == "user_message":
                    await orchestrator.handle_user_message(parsed["text"], source="frontend")
                elif parsed["action"] == "interrupt":
                    await orchestrator.handle_interrupt(reason=parsed.get("reason") or "user")
                else:
                    await websocket.send_json({"type": "error", "error": "unknown_message_type"})
            except InvalidRequestError as exc:
                await websocket.send_json({"type": "error", "error": "invalid_request", "detail": str(exc)})
            except Exception as exc:
                log_operation(
                    logger,
                    "websocket_handler_error",
                    conversation_id=conversation_id,
                    error=str(exc),
                )
                await websocket.send_json({"type": "error", "error": "internal_error"})
    except WebSocketDisconnect:
        log_operation(logger, "websocket_disconnected", conversation_id=conversation_id)
    finally:
        pump.cancel()
        unsubscribe()
        try:
            await pump
        except asyncio.CancelledError:
            pass
