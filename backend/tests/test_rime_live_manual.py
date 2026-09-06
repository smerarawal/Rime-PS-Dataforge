"""
test_rime_live_manual.py

NOT a pytest test — a standalone script that makes REAL calls to Rime's
live API. Requires RIME_API_KEY to be set in your environment (or in
.env, loaded via python-dotenv below). This will consume a small amount
of real Rime API usage — that's expected and fine.

Run:
    python backend/tests/test_rime_live_manual.py

Two parts:
  1. Raw probe — connects directly to Rime's /ws3 endpoint (bypassing
     RimeTTSProvider entirely) and prints every raw message it gets back,
     unparsed. This is what actually answers the `data` vs `audio` key
     question — the mocked test can't, because it only ever echoes back
     whatever key the mock's author chose.
  2. Full adapter test — runs RimeTTSProvider.speak() for real and
     confirms real audio bytes come back through the actual code path,
     not just the raw connection.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
if not os.path.isdir(os.path.join(REPO_ROOT, "backend")):
    print(f"WARNING: computed repo root {REPO_ROOT!r} has no 'backend' folder in it.")
    print("Run this script from the repo root instead: python backend/tests/test_rime_live_manual.py")
sys.path.insert(0, REPO_ROOT)

RIME_API_KEY = os.environ.get("RIME_API_KEY", "")
RIME_MODEL_ID = os.environ.get("RIME_MODEL_ID", "mistv2")
RIME_SPEAKER = os.environ.get("RIME_SPEAKER", "cove")
RIME_SAMPLING_RATE = os.environ.get("RIME_SAMPLING_RATE", "24000")

RIME_WS3_URL = "wss://users-ws.rime.ai/ws3"


async def raw_probe():
    import websockets

    print("\n=== PART 1: raw probe against real Rime /ws3 ===")

    url = (
        f"{RIME_WS3_URL}?modelId={RIME_MODEL_ID}&speaker={RIME_SPEAKER}"
        f"&lang=eng&samplingRate={RIME_SAMPLING_RATE}"
    )
    headers = {"Authorization": f"Bearer {RIME_API_KEY}"}

    async with websockets.connect(url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"text": "Hello, this is a live test."}))
        await ws.send(json.dumps({"operation": "eos"}))

        message_count = 0
        async for raw in ws:
            message_count += 1
            try:
                parsed = json.loads(raw)
                keys = list(parsed.keys())
                msg_type = parsed.get("type")
                print(f"[msg {message_count}] type={msg_type!r} keys={keys}")
                if msg_type == "chunk":
                    # Show which key actually holds the audio, without
                    # dumping the full base64 blob.
                    for candidate in ("data", "audio", "audio_content", "chunk"):
                        if candidate in parsed:
                            val = parsed[candidate]
                            print(f"    -> audio key confirmed: '{candidate}' "
                                  f"(base64 string, length={len(val)})")
                if msg_type == "done":
                    print("    -> stream signaled done")
                    break
                if msg_type == "error":
                    print(f"    -> ERROR message: {parsed}")
                    break
            except json.JSONDecodeError:
                print(f"[msg {message_count}] non-JSON / binary frame, "
                      f"type={type(raw)}, len={len(raw)}")

            if message_count > 50:
                print("Safety cutoff at 50 messages — stopping.")
                break

    print(f"=== Raw probe complete: {message_count} messages received ===\n")


async def full_adapter_test():
    print("=== PART 2: full RimeTTSProvider.speak() against real API ===")

    import backend.app.adapters.rime as rime_mod

    provider = rime_mod.RimeTTSProvider(
        api_key=RIME_API_KEY,
        model_id=RIME_MODEL_ID,
        speaker=RIME_SPEAKER,
        sampling_rate=int(RIME_SAMPLING_RATE),
    )

    await provider.speak(
        "This is a real end to end test of the Rime adapter.",
        request_id="live-req-1",
        generation_id="live-gen-1",
        conversation_id="live-conv-1",
    )

    snap = provider.snapshot()
    print(f"Snapshot after speak(): {snap}")

    assert provider.completed_at is not None, "expected completed_at to be set"
    assert snap["audio_bytes_received"] > 0, "expected real audio bytes, got 0"

    print(f"PASS — received {snap['audio_bytes_received']} real audio bytes from Rime.")


async def main():
    if not RIME_API_KEY:
        print("RIME_API_KEY is not set. Add it to .env and try again.")
        return

    await raw_probe()
    await full_adapter_test()


if __name__ == "__main__":
    asyncio.run(main())
