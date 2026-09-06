"""TTS provider contract. Smera implements Rime behind this interface."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from backend.app.utils.timing import utcnow


class TTSProvider(ABC):
    name: str

    @abstractmethod
    async def speak(
        self,
        text: str,
        *,
        request_id: str,
        generation_id: str,
        conversation_id: str,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stream(
        self,
        chunks: AsyncIterator[str],
        *,
        request_id: str,
        generation_id: str,
        conversation_id: str,
    ) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name}


class MockTTSProvider(TTSProvider):
    """In-process TTS used for development and interruption tests."""

    name = "mock"

    def __init__(self, chunk_delay_seconds: float = 0.02) -> None:
        self.chunk_delay_seconds = chunk_delay_seconds
        self._speaking = False
        self._stop_requested = False
        self._lock = asyncio.Lock()
        self.last_text: str | None = None
        self.started_at = None
        self.stopped_at = None
        self.completed_at = None
        self.chunks_played: list[str] = []

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    async def speak(
        self,
        text: str,
        *,
        request_id: str,
        generation_id: str,
        conversation_id: str,
    ) -> None:
        async def _chunks() -> AsyncIterator[str]:
            for word in text.split():
                yield word

        await self.stream(
            _chunks(),
            request_id=request_id,
            generation_id=generation_id,
            conversation_id=conversation_id,
        )

    async def stream(
        self,
        chunks: AsyncIterator[str],
        *,
        request_id: str,
        generation_id: str,
        conversation_id: str,
    ) -> None:
        async with self._lock:
            self._speaking = True
            self._stop_requested = False
            self.started_at = utcnow()
            self.stopped_at = None
            self.completed_at = None
            self.chunks_played = []
            self.last_text = ""

        try:
            async for chunk in chunks:
                if self._stop_requested:
                    break
                self.chunks_played.append(chunk)
                self.last_text = " ".join(self.chunks_played)
                if self.chunk_delay_seconds:
                    await asyncio.sleep(self.chunk_delay_seconds)
            if not self._stop_requested:
                self.completed_at = utcnow()
        finally:
            self._speaking = False

    async def stop(self) -> None:
        self._stop_requested = True
        self._speaking = False
        self.stopped_at = utcnow()

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "speaking": self._speaking,
            "last_text": self.last_text,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
