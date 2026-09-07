"""
Continuity during tool work, in the orchestrator runtime.

The orchestrator originally superseded the current request on ANY user
utterance that arrived while it was busy, and paid an analyze_intent
round-trip to work out what to do. That is wrong for the utterances below:
"are you still there?", a backchannel, and an explicit cancel are not new
requests, and two of them should leave the in-flight search completely alone.

These tests pin the fast path added in
``Orchestrator._handle_continuity_utterance``, which shares its classifier
with the LiveKit agent (``continuity.py`` at the repo root) so both runtimes
read a mid-tool utterance the same way.

Every test here gates on the tool's ``started`` event and holds it open with
``release``, so the second utterance provably lands while the search is still
running. Sleeping and hoping would make these tests pass for the wrong
reason.
"""

from __future__ import annotations

import asyncio

from backend.app.core.events import EventType
from backend.app.models.requests import RequestStatus
from backend.app.tools.fake_search import FakeSlowSearchTool
from backend.tests.conftest import make_orchestrator


class RunningSearch:
    """An orchestrator whose search tool is held mid-flight until released."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.tool = FakeSlowSearchTool(started=self.started, release=self.release)
        self.orchestrator = make_orchestrator(tool=self.tool)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(
            self.orchestrator.handle_user_message("find hotels in mumbai")
        )
        await asyncio.wait_for(self.started.wait(), timeout=2)
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        try:
            await asyncio.wait_for(self.task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return False

    @property
    def o(self):
        return self.orchestrator


async def test_status_question_does_not_supersede_the_running_request() -> None:
    async with RunningSearch() as ctx:
        before = await ctx.o.requests.get_current_request()
        assert before is not None and before.status == RequestStatus.ACTIVE

        result = await ctx.o.handle_user_message("are you still there?")

        # Handled entirely on the fast path: no new request, and the original
        # is still current and still active.
        assert result is None
        after = await ctx.o.requests.get_current_request()
        assert after is not None
        assert after.request_id == before.request_id
        assert after.status == RequestStatus.ACTIVE
        assert ctx.tool.calls == 1          # the search was never restarted


async def test_status_answer_names_the_work_and_its_age() -> None:
    async with RunningSearch() as ctx:
        await ctx.o.handle_user_message("any luck?")
        spoken = ctx.o.tts.last_text or ""

    assert spoken.startswith("Still ")
    assert "second" in spoken or "just started" in spoken


async def test_backchannel_is_not_answered_and_keeps_the_work() -> None:
    async with RunningSearch() as ctx:
        before = await ctx.o.requests.get_current_request()
        spoken_before = list(ctx.o.tts.chunks_played)

        result = await ctx.o.handle_user_message("mhm")

        assert result is None
        after = await ctx.o.requests.get_current_request()
        assert after.request_id == before.request_id
        # Nothing new was said — talking over a backchannel is worse than
        # silence.
        assert ctx.o.tts.chunks_played == spoken_before


async def test_cancel_stops_the_request_and_confirms() -> None:
    async with RunningSearch() as ctx:
        before = await ctx.o.requests.get_current_request()

        result = await ctx.o.handle_user_message("never mind")

        assert result is None
        request = await ctx.o.requests.get_request(before.request_id)
        assert request.status in {RequestStatus.CANCELLED, RequestStatus.OBSOLETE}
        assert "stopped" in (ctx.o.tts.last_text or "")


async def test_genuinely_new_request_still_supersedes() -> None:
    """The safety property: anything the router does not recognise as a
    continuation must go down the original supersede path."""
    async with RunningSearch() as ctx:
        before = await ctx.o.requests.get_current_request()

        result = await ctx.o.handle_user_message("what is your refund policy")

        assert result is not None                       # a new request exists
        assert result.request_id != before.request_id
        superseded = await ctx.o.requests.get_request(before.request_id)
        assert superseded.status in {RequestStatus.OBSOLETE, RequestStatus.CANCELLED}


async def test_superseded_request_never_produces_an_accepted_result() -> None:
    """Fencing must still hold across the fast path: the abandoned search's
    result may never be accepted, even though it was already running."""
    async with RunningSearch() as ctx:
        events: list = []
        ctx.o.events.subscribe(events.append)
        first = await ctx.o.requests.get_current_request()

        await ctx.o.handle_user_message("what is your refund policy")
        ctx.release.set()                                # let the old search finish
        await asyncio.sleep(0.05)

        accepted_for_first = [
            e for e in events
            if e.event_type == EventType.RESULT_ACCEPTED and e.request_id == first.request_id
        ]
        assert accepted_for_first == []


async def test_status_question_does_not_call_the_llm() -> None:
    """The fast path exists partly for latency: it must not add a model
    round-trip to the interrupt path."""
    async with RunningSearch() as ctx:
        calls: list[str] = []
        original = ctx.o.llm.analyze_intent

        async def _counting(*args, **kwargs):
            calls.append("analyze_intent")
            return await original(*args, **kwargs)

        ctx.o.llm.analyze_intent = _counting  # type: ignore[method-assign]
        await ctx.o.handle_user_message("are you still there?")
        assert calls == []

        # ...and the fallthrough path still does call it, so this test cannot
        # pass just because the counter was wired up wrong.
        await ctx.o.handle_user_message("what is your refund policy")
        assert calls == ["analyze_intent"]


async def test_status_question_leaves_the_conversation_status_intact() -> None:
    """Speaking a status line must not tell the state machine the agent has
    moved on from executing the tool."""
    async with RunningSearch() as ctx:
        status_before = ctx.o.get_state().status
        await ctx.o.handle_user_message("how's that going")
        assert ctx.o.get_state().status == status_before


async def test_status_line_is_speakable_not_a_variable_name() -> None:
    """The status sentence is spoken aloud by Rime. "Still search_hotels" —
    or its de-underscored cousin "Still search hotels" — is worse than saying
    nothing, so the intent slug is mapped to a real phrase."""
    async with RunningSearch() as ctx:
        await ctx.o.handle_user_message("are you still there?")
        spoken = ctx.o.tts.last_text or ""

    assert "searching for hotels" in spoken
    assert "search_hotels" not in spoken
    assert "Still search hotels" not in spoken


async def test_unknown_intent_still_produces_a_sentence() -> None:
    from backend.app.core.orchestrator import _describe_intent

    assert _describe_intent(None) == "working on that"
    assert _describe_intent("check_refund_status") == "working on your check refund status request"
