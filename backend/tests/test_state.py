from __future__ import annotations

import json

from backend.app.core.state import ConversationStore
from backend.app.models.conversation import ConversationStatus


async def test_state_is_json_serializable() -> None:
    store = ConversationStore("conv_1")
    await store.bind_request("req_1", "gen_1", intent="search_hotels", parameters={"city": "Mumbai"})
    await store.set_status(ConversationStatus.EXECUTING)
    snapshot = await store.snapshot()
    encoded = json.dumps(snapshot.to_json_dict())
    assert "conv_1" in encoded
    assert snapshot.current_parameters["city"] == "Mumbai"


async def test_snapshot_is_isolated() -> None:
    store = ConversationStore("conv_2")
    snap = await store.snapshot()
    snap.current_parameters["city"] = "Delhi"
    latest = await store.snapshot()
    assert "city" not in latest.current_parameters


async def test_status_values_exist() -> None:
    assert ConversationStatus.IDLE.value == "IDLE"
    assert ConversationStatus.INTERRUPTED.value == "INTERRUPTED"
    assert ConversationStatus.CANCELLING.value == "CANCELLING"
