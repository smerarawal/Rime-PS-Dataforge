"""
tools.py

lookup_order_status simulates a slow backend order-status lookup. Its delay
is controlled by STRESS_TEST_TOOL_DELAY_MS so the interruption/staleness
stress test can reliably force the race condition on demand: interrupt the
agent while this tool is still running, and confirm the stale result never
reaches the user.

The tool registers itself with the TurnManager as PendingWork before it
starts. That registration does three jobs at once:

1. it lets an interrupt cancel exactly this call and nothing else,
2. it lets a mid-flight status question be answered ("still checking order
   1002, about 2 seconds in") without touching the call, and
3. it carries the turn stamp, which is re-read at point of use — so a call
   the user chose to keep (via a status question or an added constraint)
   survives the turn boundary, while a call the user moved past does not.

The staleness check is deliberately taken from ``work.turn_id`` at the moment
the result is about to be returned, never from a local variable captured at
call time. Capturing it would defeat carry-forward, and — more importantly —
re-checking at point of use is what makes the guarantee hold when
cancellation fails to land in time.

Both the success path AND the failure/timeout path run through the same
fence: a backend error that arrives after the user has moved on is just as
wrong to narrate as a successful result would be.
"""

import asyncio
import os

try:
    from livekit.agents import function_tool, RunContext
    _LIVEKIT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in the no-framework env
    # The fencing logic below is plain asyncio and is worth testing without a
    # LiveKit install (CI, and the offline acceptance test). Only the decorated
    # tool registration actually needs the framework, and that path fails loudly
    # if it is reached without it.
    _LIVEKIT_AVAILABLE = False
    RunContext = object

    def function_tool(*_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator

from turn_manager import TurnManager, StaleResultError


# Fake order database for the demo
_FAKE_ORDERS = {
    "1001": {"status": "out for delivery", "eta": "today by 6pm"},
    "1002": {"status": "delayed", "eta": "unknown, carrier issue"},
    "1003": {"status": "delivered", "eta": "delivered yesterday"},
}

# Upper bound on the simulated backend. A real backend needs one too — an
# unbounded tool call is indistinguishable from a hung session to the user.
TOOL_TIMEOUT_S = float(os.environ.get("TOOL_TIMEOUT_S", "15"))


async def _lookup_order_status_impl(turn_manager: TurnManager, order_id: str, work=None) -> str:
    """The actual lookup logic, undecorated, so it can be called directly
    from acceptance_test.py without depending on @function_tool's internal
    wrapper shape (which may differ across livekit-agents versions).

    ``work`` is the PendingWork registration, when there is one. Without it
    the function falls back to a stamp captured at call time, which is the
    original behaviour and is still correct for the plain
    interrupt-supersedes-work case.
    """
    stamped_id = turn_manager.stamp()

    delay_ms = int(os.environ.get("STRESS_TEST_TOOL_DELAY_MS", "0"))

    try:
        if delay_ms > 0:
            await asyncio.wait_for(asyncio.sleep(delay_ms / 1000), timeout=TOOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        # The failure path is fenced exactly like the success path: if the
        # user has moved on, they must not hear about a backend that timed
        # out on a question they abandoned.
        if _stale(turn_manager, work, stamped_id):
            raise StaleResultError(
                f"Order lookup timed out for turn {stamped_id}, and the turn is stale; discarding."
            )
        return "The order system is not responding right now. I can try again, or take a message."

    # Point-of-use check: read the turn stamp NOW, not the value captured
    # when this coroutine started.
    if _stale(turn_manager, work, stamped_id):
        raise StaleResultError(
            f"Order lookup for turn {stamped_id} is stale; discarding result."
        )

    order = _FAKE_ORDERS.get(order_id) or _FAKE_ORDERS["1001"]
    result = f"Order status: {order['status']}. Estimated: {order['eta']}."

    # Constraints the user added while the lookup was running are applied to
    # the result rather than being dropped — that is the whole point of
    # keeping the work alive instead of restarting it.
    if work is not None and work.constraints:
        notes = "; ".join(work.constraints)
        result += f" (User added while this was running: {notes})"
    return result


def _stale(turn_manager: TurnManager, work, stamped_id: int) -> bool:
    """Single staleness entry point, so the success and failure paths cannot
    drift apart."""
    if work is not None:
        return turn_manager.is_work_stale(work)
    return turn_manager.is_stale(stamped_id)


def register_tools(turn_manager: TurnManager):
    """Returns the tool function bound to a specific TurnManager instance,
    so agent.py can pass in the same turn_manager used everywhere else."""
    if not _LIVEKIT_AVAILABLE:
        raise RuntimeError(
            "register_tools() requires livekit-agents. Install it with "
            "`pip install livekit-agents` — the fencing logic in "
            "_lookup_order_status_impl runs without it, but tool registration "
            "does not."
        )

    @function_tool(
        description=(
            "Look up the current status of a customer's order by order ID. "
            "Use this whenever the user asks about their order status, "
            "delivery, or shipping."
        )
    )
    async def lookup_order_status(ctx: RunContext, order_id: str) -> str:
        # Register BEFORE the acknowledgement, so an interrupt landing during
        # the acknowledgement already has something to route against.
        work = turn_manager.register_work(f"checking order {order_id}")

        # Acknowledge immediately so the session stays responsive while the
        # slow lookup runs — the user hears the request land instead of
        # silence, which is what stops them repeating themselves and
        # accidentally superseding their own turn.
        try:
            await ctx.session.say("Let me check on that for you.", allow_interruptions=True)
        except Exception:
            pass

        # Run the actual lookup in its own dedicated task and register it
        # with turn_manager so an interrupt can cancel JUST this tool call
        # (see turn_manager.cancel_active_tool_task), without touching the
        # broader generation task the framework depends on for handling the
        # next turn correctly.
        task = asyncio.create_task(_lookup_order_status_impl(turn_manager, order_id, work))
        work.task = task
        turn_manager.active_tool_task = task
        try:
            return await task
        except asyncio.CancelledError:
            raise StaleResultError(
                "Order lookup cancelled mid-flight (turn superseded)."
            )
        finally:
            turn_manager.complete_work(work.work_id)

    return lookup_order_status
