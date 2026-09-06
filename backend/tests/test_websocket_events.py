from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from backend.app.api.sessions import SessionRegistry
from backend.app.main import create_app
from backend.tests.conftest import make_orchestrator


def _app() -> TestClient:
    registry = SessionRegistry(factory=lambda conversation_id: make_orchestrator(conversation_id))
    return TestClient(create_app(sessions=registry))


def _receive_until(
    socket,
    predicate: Callable[[dict], bool],
    limit: int = 40,
) -> list[dict]:
    events: list[dict] = []
    for _ in range(limit):
        message = socket.receive_json()
        events.append(message)
        if predicate(message):
            return events
    types = [event.get("type") for event in events]
    raise AssertionError(f"predicate not met in events: {types}")


def test_health_endpoint() -> None:
    client = _app()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_websocket_user_message_and_interrupt() -> None:
    client = _app()
    with client.websocket_connect("/ws/conversation/conv_ws") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "state_updated"

        socket.send_json({"type": "user_message", "text": "Find hotels in Mumbai"})
        started = _receive_until(socket, lambda message: message["type"] == "task_started")
        assert any(message["type"] == "request_created" for message in started)

        socket.send_json({"type": "interrupt"})
        interrupted = _receive_until(
            socket,
            lambda message: message["type"] in {"tts_stop", "interruption"},
        )
        assert any(message["type"] in {"interruption", "tts_stop"} for message in interrupted)

        socket.send_json({"type": "user_message", "text": "Actually under 5000"})
        _receive_until(
            socket,
            lambda message: message["type"] == "assistant_response_ready"
            and "under 5000" in message.get("text", ""),
        )

        socket.send_json({"type": "user_message", "text": ""})
        error = _receive_until(socket, lambda message: message["type"] == "error")
        assert error[-1]["type"] == "error"


def test_duplicate_client_event_id_is_ignored() -> None:
    client = _app()
    with client.websocket_connect("/ws/conversation/conv_dup") as socket:
        socket.receive_json()
        socket.send_json({"type": "interrupt", "event_id": "dup-1"})
        first = _receive_until(socket, lambda message: message["type"] == "interruption")
        socket.send_json({"type": "interrupt", "event_id": "dup-1"})
        assert sum(1 for message in first if message["type"] == "interruption") == 1
