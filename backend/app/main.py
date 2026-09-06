"""FastAPI entrypoint. uvicorn backend.app.main:app --reload"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.routes import router as http_router
from backend.app.api.sessions import SessionRegistry
from backend.app.api.websocket import router as ws_router
from backend.app.config import get_settings
from backend.app.utils.logging import configure_logging, get_logger, log_operation

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log_operation(
        logger,
        "server_starting",
        llm_provider=settings.llm_provider,
        tts_provider=settings.tts_provider,
        realtime_provider=settings.realtime_provider,
    )
    yield
    log_operation(logger, "server_stopping")


def create_app(sessions: SessionRegistry | None = None) -> FastAPI:
    app = FastAPI(title="Rime Orchestrator", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.sessions = sessions or SessionRegistry()
    app.include_router(http_router)
    app.include_router(ws_router)
    return app


app = create_app()
