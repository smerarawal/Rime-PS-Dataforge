"""
WebSocket bridge for the real MetricsLog.

IMPORTANT:
This must run inside the SAME Python process as agent.py because
MetricsLog stores events in memory.

The frontend connects to:
    ws://localhost:8765

Events are pushed as they are recorded, via MetricsLog.subscribe(), rather
than polled. The subscriber callback runs synchronously inside the agent's
event loop — on the interrupt path, among other places — so it does the
smallest possible amount of work: put the event on a queue. A separate task
drains the queue and does the actual sending. Blocking or awaiting inside
the callback would add latency to the very pipeline this dashboard exists to
measure.

A newly connected client is sent the backlog first, so a dashboard opened
mid-session shows the turns that already happened instead of starting blank.
"""

import asyncio
import json

import websockets

from metrics import MetricsLog


CLIENTS = set()

# Bounded: if a client stalls, drop the oldest observability events rather
# than growing without limit inside a live voice session.
_QUEUE_MAX = 2000

# Sent to a client on connect. Enough to reconstruct recent turns without
# replaying an entire long session.
_BACKLOG_LIMIT = 200


async def handler(websocket):
    CLIENTS.add(websocket)
    print("[WS] Frontend connected")

    try:
        backlog = MetricsLog.all_events()[-_BACKLOG_LIMIT:]
        for event in backlog:
            await websocket.send(json.dumps(event))
    except Exception as e:
        print(f"[WS] backlog send failed: {e}")

    try:
        await websocket.wait_closed()
    except Exception:
        pass
    finally:
        CLIENTS.discard(websocket)
        print("[WS] Frontend disconnected")


async def broadcast(event):
    if not CLIENTS:
        return

    message = json.dumps(event, default=str)
    dead_clients = set()

    for client in CLIENTS:
        try:
            await client.send(message)
        except Exception:
            dead_clients.add(client)

    for client in dead_clients:
        CLIENTS.discard(client)


async def _drain(queue: asyncio.Queue):
    """Owns all the actual sending, off the recording path."""
    while True:
        event = await queue.get()
        try:
            await broadcast(event)
        except Exception as e:
            print(f"[WS] broadcast error: {e}")


async def run_ws_bridge(host="localhost", port=8765):
    print(f"[WS] Metrics bridge starting on ws://{host}:{port}")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)

    def _on_metric(entry):
        # Runs synchronously inside MetricsLog.record(). Must not block and
        # must not raise — a dashboard problem must never break the agent.
        try:
            loop.call_soon_threadsafe(_enqueue, entry)
        except RuntimeError:
            pass  # loop is shutting down

    def _enqueue(entry):
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()      # drop oldest, keep the live view current
                queue.put_nowait(entry)
            except Exception:
                pass

    unsubscribe = MetricsLog.subscribe(_on_metric)
    drainer = asyncio.create_task(_drain(queue))

    try:
        async with websockets.serve(handler, host, port):
            print("[WS] Metrics bridge ready (push-based via MetricsLog.subscribe)")
            await asyncio.Event().wait()
    except OSError as e:
        # Port already in use, most likely a second agent process. The voice
        # agent must still run — observability is not worth failing a session.
        print(f"[WS] bridge could not start ({e}); the agent continues without it")
    finally:
        unsubscribe()
        drainer.cancel()
