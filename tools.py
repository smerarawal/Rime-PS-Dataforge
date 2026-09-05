"""
tools.py

lookup_order_status simulates a slow backend order-status lookup. Its delay
is controlled by STRESS_TEST_TOOL_DELAY_MS so the interruption/staleness
stress test can reliably force the race condition on demand: interrupt the
agent while this tool is still running, and confirm the stale result never
reaches the user.

The tool is stamped with the turn id active when it was called, and checked
against turn_manager.is_stale() again right before its result is returned
into the chat context — the same point-of-use principle used in
Assistant.llm_node. This means even if the tool finishes after the user has
moved on to a new question, the stale order-status result is discarded
rather than being fed back into the conversation and potentially spoken.
"""

import asyncio
import os

from livekit.agents import function_tool, RunContext

from turn_manager import TurnManager, StaleResultError


# Fake order database for the demo
_FAKE_ORDERS = {
    "1001": {"status": "out for delivery", "eta": "today by 6pm"},
    "1002": {"status": "delayed", "eta": "unknown, carrier issue"},
    "1003": {"status": "delivered", "eta": "delivered yesterday"},
}


async def _lookup_order_status_impl(turn_manager: TurnManager, order_id: str) -> str:
    """The actual lookup logic, undecorated, so it can be called directly
    from acceptance_test.py without depending on @function_tool's internal
    wrapper shape (which may differ across livekit-agents versions)."""
    stamped_id = turn_manager.stamp()

    delay_ms = int(os.environ.get("STRESS_TEST_TOOL_DELAY_MS", "0"))
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)

    if turn_manager.is_stale(stamped_id):
        raise StaleResultError(
            f"Order lookup for turn {stamped_id} is stale; discarding result."
        )

    order = _FAKE_ORDERS.get(order_id) or _FAKE_ORDERS["1001"]
    return f"Order status: {order['status']}. Estimated: {order['eta']}."


def register_tools(turn_manager: TurnManager):
    """Returns the tool function bound to a specific TurnManager instance,
    so agent.py can pass in the same turn_manager used everywhere else."""

    @function_tool(
        description=(
            "Look up the current status of a customer's order by order ID. "
            "Use this whenever the user asks about their order status, "
            "delivery, or shipping."
        )
    )
    async def lookup_order_status(ctx: RunContext, order_id: str) -> str:
        # Acknowledge immediately so the session stays responsive while the
        # slow lookup runs — satisfies conversation continuity during tool
        # work (Direction 3), rather than leaving the user hanging in silence.
        try:
            await ctx.session.say("Let me check on that for you.", allow_interruptions=True)
        except Exception:
            pass

        # Run the actual lookup in its own dedicated task and register it
        # with turn_manager so an interrupt can cancel JUST this tool call
        # (see turn_manager.cancel_active_tool_task), without touching the
        # broader generation task the framework depends on for handling the
        # next turn correctly.
        task = asyncio.create_task(_lookup_order_status_impl(turn_manager, order_id))
        turn_manager.active_tool_task = task
        try:
            return await task
        except asyncio.CancelledError:
            raise StaleResultError(
                f"Order lookup cancelled mid-flight (turn superseded)."
            )

    return lookup_order_status
