"""Full-system acceptance test for the interrupt-during-speech race,
using the REAL Orchestrator + REAL RimeTTSProvider (websocket mocked,
so no RIME_API_KEY / network needed — deterministic and CI-safe).

Why this exists: backend/tests/test_race_conditions.py already proves
the fencing/cancellation logic against MockTTSProvider, but that mock
uses chunk_delay_seconds=0.0 — TTS is effectively instant there, so no
test in the suite actually interrupts mid-audio-stream. That's exactly
the race the whole architecture exists to survive. This closes that gap
using RimeTTSProvider's real streaming/stop() code path, and uses
EvidenceCollector (independent of Orchestrator internals) to produce
the leak-count and tts_stop_latency_ms numbers for RIME_EVIDENCE.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from unittest import mock

from backend.app.adapters.rime import RimeTTSProvider
from backend.app.core.events import EventBus, EventType
from backend.app.core.orchestrator import Orchestrator
from backend.app.llm.mock import MockLLMProvider
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.app.tools.registry import ToolRegistry
from backend.app.utils.evidence import EvidenceCollector

AUDIO_B64 = base64.b64encode(b"FAKEAUDIO").decode()


class SlowFakeWS:
    """Mimics an in-progress Rime stream: many chunks, real delay between
    each, so there's a genuine window in which an interrupt lands mid-speech
    (unlike MockTTSProvider's instant chunk_delay_seconds=0.0)."""

    def __init__(self, num_chunks: int = 200, delay_seconds: float = 0.01) -> None:
        self._remaining = num_chunks
        self._delay = delay_seconds
        self._closed = False
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed or self._remaining <= 0:
            if not self._closed:
                self._closed = True
                return json.dumps({"type": "done"})
            raise StopAsyncIteration
        await asyncio.sleep(self._delay)
        self._remaining -= 1
        return json.dumps({"type": "chunk", "data": AUDIO_B64})


def _install_fake_websockets(ws: SlowFakeWS) -> None:
    class FakeConnect:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return ws

        async def __aexit__(self, *a):
            return False

    fake_module = mock.MagicMock()
    fake_module.connect = lambda *a, **kw: FakeConnect()
    sys.modules["websockets"] = fake_module


async def _run_one_trial() -> EvidenceCollector:
    ws = SlowFakeWS(num_chunks=200, delay_seconds=0.01)
    _install_fake_websockets(ws)

    bus = EventBus()
    evidence = EvidenceCollector(bus)

    registry = ToolRegistry()
    registry.register(FakeSlowSearchTool(delay_seconds=0.01))

    orch = Orchestrator(
        "conv_rime_acceptance",
        llm=MockLLMProvider(),
        tts=RimeTTSProvider(api_key="fake-key-for-test"),
        tools=registry,
        event_bus=bus,
        speak_bridging=False,
        cancel_timeout=0.05,
    )

    first = await orch.handle_user_message("Find hotels in Mumbai")
    assert first is not None

    # Let real audio streaming actually get underway before interrupting —
    # this is the whole point: TTS must be genuinely in-flight, not instant.
    await bus.wait_for(
        lambda e: e.event_type == EventType.TTS_START and e.request_id == first.request_id,
        timeout=2.0,
    )
    await asyncio.sleep(0.03)

    second = await orch.handle_user_message("Actually under 5000")
    assert second is not None

    await bus.wait_for(
        lambda e: e.event_type == EventType.ASSISTANT_RESPONSE_READY
        and e.request_id == second.request_id,
        timeout=2.0,
    )

    evidence.close()
    return evidence


async def test_single_interrupt_during_real_rime_stream_no_leak() -> None:
    evidence = await _run_one_trial()
    summary = evidence.summary()

    assert summary["leak_count"] == 0, summary["leaks"]
    assert summary["trials"] == 1
    # tts_stop_latency should be small and non-negative — RimeTTSProvider.stop()
    # flips its flag before doing any I/O, so this should be well under 100ms.
    assert evidence.tts_stop_latencies_ms[0] >= 0


async def test_acceptance_20_trials_zero_leaks() -> None:
    """The full-system equivalent of Codebase 1's acceptance_test.py:
    N trials of interrupt-during-real-TTS-stream, reporting p50/p95 and
    leak count. This is the number set that goes into RIME_EVIDENCE.md."""

    all_latencies: list[float] = []
    total_leaks = 0

    for _ in range(20):
        evidence = await _run_one_trial()
        summary = evidence.summary()
        total_leaks += summary["leak_count"]
        all_latencies.extend(evidence.tts_stop_latencies_ms)

    assert total_leaks == 0

    combined = EvidenceCollector.__new__(EvidenceCollector)  # reuse summary() math only
    combined.tts_stop_latencies_ms = all_latencies
    combined.leaks = []
    report = combined.summary()

    print(f"\n[RIME_EVIDENCE] trials=20 leaks={total_leaks} "
          f"p50={report['tts_stop_latency_ms']['p50']:.2f}ms "
          f"p95={report['tts_stop_latency_ms']['p95']:.2f}ms "
          f"max={report['tts_stop_latency_ms']['max']:.2f}ms")
