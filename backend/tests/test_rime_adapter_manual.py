import asyncio, base64, json, sys
sys.path.insert(0, "/home/claude/repo_atharva")
from unittest import mock

async def main():
    import backend.app.adapters.rime as rime_mod

    class FakeWS:
        def __init__(self, incoming_messages, on_send=None):
            self._incoming = list(incoming_messages)
            self._closed = False
            self.sent = []
            self.on_send = on_send

        async def send(self, msg):
            self.sent.append(msg)
            if self.on_send:
                await self.on_send(msg)

        async def close(self):
            self._closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._closed or not self._incoming:
                raise StopAsyncIteration
            await asyncio.sleep(0.01)
            return self._incoming.pop(0)

    audio = base64.b64encode(b"FAKEAUDIO").decode()
    messages = [
        json.dumps({"type": "chunk", "data": audio}),
        json.dumps({"type": "chunk", "data": audio}),
        json.dumps({"type": "done"}),
    ]

    fake_ws = FakeWS(messages)

    class FakeConnect:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return fake_ws
        async def __aexit__(self, *a):
            return False

    fake_websockets_module = mock.MagicMock()
    fake_websockets_module.connect = lambda *a, **kw: FakeConnect()

    with mock.patch.dict(sys.modules, {"websockets": fake_websockets_module}):
        provider = rime_mod.RimeTTSProvider(api_key="fake-key-for-test")

        async def one_chunk():
            yield "hello world"

        await provider.speak("hello world", request_id="req1", generation_id="gen1", conversation_id="conv1")

        assert provider.completed_at is not None, "should have completed normally"
        assert len(provider._audio_bytes) > 0, "should have received audio bytes"
        print("Test 1 (normal completion): PASS")

        # Test 2: interrupt mid-stream via stop() -> should not mark completed, should be stale
        fake_ws2 = FakeWS([json.dumps({"type": "chunk", "data": audio})] * 10)
        class FakeConnect2:
            async def __aenter__(self): return fake_ws2
            async def __aexit__(self, *a): return False
        fake_websockets_module.connect = lambda *a, **kw: FakeConnect2()

        provider2 = rime_mod.RimeTTSProvider(api_key="fake-key-for-test")
        task = asyncio.create_task(provider2.speak("long text here", request_id="req2", generation_id="gen2", conversation_id="conv1"))
        await asyncio.sleep(0.02)
        await provider2.stop()
        await task
        assert provider2.completed_at is None, "should NOT have completed after stop()"
        assert provider2.stopped_at is not None
        print("Test 2 (interrupt via stop()): PASS")

        # Test 3: stop() idempotency - calling twice should not raise
        await provider2.stop()
        await provider2.stop()
        print("Test 3 (idempotent stop): PASS")

asyncio.run(main())
