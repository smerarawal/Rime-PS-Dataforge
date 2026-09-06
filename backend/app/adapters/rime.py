"""TTS provider contract. Smera implements Rime behind this interface."""

from __future__ import annotations

import asyncio
import base64
import json
import os
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


# Rime's flagship streaming endpoint. JSON messages, base64-encoded audio
# chunks. See https://docs.rime.ai/docs/websockets for the protocol.
RIME_WS3_URL = "wss://users-ws.rime.ai/ws3"


class RimeTTSProvider(TTSProvider):
    """Real Rime-backed TTS provider using the /ws3 streaming endpoint.

    Mirrors MockTTSProvider's evidence fields (last_text, started_at,
    stopped_at, completed_at) so anything already consuming those keeps
    working unmodified when TTS_PROVIDER=rime is set.

    Staleness handling: every call to speak()/stream() registers its
    request_id/generation_id as "current" before doing any network work.
    Every loop iteration (sending a text chunk, receiving an audio chunk)
    re-checks this — the same point-of-use pattern used elsewhere in this
    project, not a one-time check at the start. stop() flips a flag AND
    closes the websocket directly, so both the passive staleness check and
    an active connection-level cutoff work together as defense-in-depth.

    NOTE on the audio-chunk JSON key: Rime's docs describe /ws3 chunk
    messages as carrying base64 audio but the exact field name wasn't
    confirmed against a live response while writing this. This checks a
    couple of plausible key names (`data`, `audio`) — run one real call
    and print the raw message once to confirm, then simplify if needed.
    """

    name = "rime"

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "mistv2",
        speaker: str = "cove",
        lang: str = "eng",
        sampling_rate: int = 24000,
    ) -> None:
        self.api_key = api_key or os.environ.get("RIME_API_KEY", "")
        if not self.api_key:
            raise ValueError("RIME_API_KEY is required for RimeTTSProvider")
        self.model_id = model_id
        self.speaker = speaker
        self.lang = lang
        self.sampling_rate = sampling_rate

        self._current_request_id: str | None = None
        self._current_generation_id: str | None = None
        self._stop_requested = False
        self._ws: Any = None
        self._speaking = False
        self.last_text: str | None = None
        self.started_at = None
        self.stopped_at = None
        self.completed_at = None
        self.chunks_played: list[str] = []
        self._audio_bytes = bytearray()

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def _is_current(self, request_id: str, generation_id: str) -> bool:
        return (
            self._current_request_id == request_id
            and self._current_generation_id == generation_id
            and not self._stop_requested
        )

    async def speak(
        self,
        text: str,
        *,
        request_id: str,
        generation_id: str,
        conversation_id: str,
    ) -> None:
        async def _chunks() -> AsyncIterator[str]:
            yield text

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
        # Import here rather than at module level: keeps the mock-mode path
        # (MockTTSProvider, used by default and in most tests) free of a
        # hard dependency on `websockets` being installed.
        import websockets

        self._current_request_id = request_id
        self._current_generation_id = generation_id
        self._stop_requested = False
        self._speaking = True
        self.started_at = utcnow()
        self.stopped_at = None
        self.completed_at = None
        self.chunks_played = []
        self.last_text = ""
        self._audio_bytes = bytearray()

        url = (
            f"{RIME_WS3_URL}?modelId={self.model_id}&speaker={self.speaker}"
            f"&lang={self.lang}&samplingRate={self.sampling_rate}"
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                self._ws = ws

                async def _sender() -> None:
                    async for chunk in chunks:
                        if not self._is_current(request_id, generation_id):
                            return
                        self.chunks_played.append(chunk)
                        self.last_text = " ".join(self.chunks_played)
                        await ws.send(json.dumps({"text": chunk}))
                    if self._is_current(request_id, generation_id):
                        await ws.send(json.dumps({"operation": "eos"}))

                sender_task = asyncio.create_task(_sender())
                try:
                    async for raw in ws:
                        if not self._is_current(request_id, generation_id):
                            # Point-of-use staleness check: drop any audio
                            # still arriving once this request/generation is
                            # no longer current, even mid-stream.
                            break
                        message = json.loads(raw)
                        msg_type = message.get("type")
                        if msg_type == "chunk":
                            audio_b64 = message.get("data") or message.get("audio")
                            if audio_b64:
                                self._audio_bytes.extend(base64.b64decode(audio_b64))
                        elif msg_type == "done":
                            break
                        elif msg_type == "error":
                            break
                finally:
                    sender_task.cancel()
                    try:
                        await sender_task
                    except asyncio.CancelledError:
                        pass

            if self._is_current(request_id, generation_id):
                self.completed_at = utcnow()
        finally:
            self._ws = None
            self._speaking = False

    async def stop(self) -> None:
        # Idempotent and fast: this is the interruption-latency evidence
        # hook (orchestrator measures tts_stop_latency_ms from here). Flip
        # the flag first (unblocks any in-flight is_current() check on the
        # very next loop iteration without waiting on network I/O), then
        # close the websocket directly rather than a graceful protocol
        # handshake — speed matters more than a clean close here.
        self._stop_requested = True
        self._speaking = False
        self.stopped_at = utcnow()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass  # already closing/closed — stop() must never raise

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "speaking": self._speaking,
            "last_text": self.last_text,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "audio_bytes_received": len(self._audio_bytes),
        }
