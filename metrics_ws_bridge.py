"""
WebSocket bridge for the real MetricsLog.

IMPORTANT:
This must run inside the SAME Python process as agent.py because
MetricsLog stores events in memory.

The frontend connects to:
    ws://localhost:8765

The bridge polls MetricsLog.all_events() and forwards new events
to connected browser clients.
"""

import asyncio
import json
import websockets

from metrics import MetricsLog


CLIENTS = set()


async def handler(websocket):
    CLIENTS.add(websocket)

    print("[WS] Frontend connected")

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

    message = json.dumps(event)

    dead_clients = set()

    for client in CLIENTS:
        try:
            await client.send(message)
        except Exception:
            dead_clients.add(client)

    for client in dead_clients:
        CLIENTS.discard(client)


async def metrics_poller():
    """
    Watch MetricsLog for new events and broadcast them.

    MetricsLog itself has no subscribe() method, so we periodically
    compare the number of events already sent with all_events().
    """

    sent_count = 0

    while True:
        try:
            events = MetricsLog.all_events()

            if len(events) > sent_count:
                new_events = events[sent_count:]

                for event in new_events:
                    await broadcast(event)

                sent_count = len(events)

        except Exception as e:
            print(f"[WS] Poller error: {e}")

        await asyncio.sleep(0.05)


async def run_ws_bridge(host="localhost", port=8765):
    print(f"[WS] Metrics bridge starting on ws://{host}:{port}")

    async with websockets.serve(handler, host, port):
        print("[WS] Metrics bridge ready")

        await metrics_poller()