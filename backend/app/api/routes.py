"""HTTP routes. No conversation state lives here."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.app.config import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "tts_provider": settings.tts_provider,
        "realtime_provider": settings.realtime_provider,
    }


@router.get("/conversations/{conversation_id}/state")
async def conversation_state(conversation_id: str, request: Request) -> dict:
    orchestrator = request.app.state.sessions.get(conversation_id)
    if orchestrator is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return orchestrator.get_state().to_json_dict()
