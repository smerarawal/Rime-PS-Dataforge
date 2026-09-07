"""
test_continuity.py — Direction 3 (conversation continuity during tool work).

Run with: pytest test_continuity.py -v

Two properties are under test, and they pull in opposite directions:

* work the user asked to KEEP (status question, added constraint, backchannel)
  must survive the turn boundary and still pass the point-of-use fence, and
* work the user MOVED PAST must be cancelled and must never pass that fence,
  even if cancellation fails to land in time.

The safety property is the more important of the two, so the fallback for an
unclassifiable utterance is asserted to be "supersede", not "keep".
"""

import asyncio

import pytest

from continuity import Intent, UtteranceRouter, status_sentence
from turn_manager import TurnManager


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "are you still there",
    "any luck?",
    "how's that going",
    "still checking?",
    "how much longer",
    "hello?",
    "did you find it",
])
def test_status_questions_are_recognised_while_work_is_pending(text):
    assert UtteranceRouter().classify(text, work_pending=True).intent is Intent.STATUS_REQUEST


@pytest.mark.parametrize("text", [
    "never mind",
    "forget it",
    "stop",
    "cancel that",
    "don't bother",
])
def test_cancellations_are_recognised(text):
    assert UtteranceRouter().classify(text, work_pending=True).intent is Intent.CANCEL


@pytest.mark.parametrize("text", [
    "actually make it the express one",
    "use the other one instead",
    "the other order",
    "while you're at it check the refund",
    "order number 1003",
])
def test_constraints_are_recognised(text):
    assert UtteranceRouter().classify(text, work_pending=True).intent is Intent.CONSTRAINT


@pytest.mark.parametrize("text", ["ok", "sure", "mhm", "got it", "thanks"])
def test_backchannels_are_recognised(text):
    assert UtteranceRouter().classify(text, work_pending=True).intent is Intent.AFFIRMATION


def test_status_question_with_nothing_running_is_just_a_new_request():
    assert UtteranceRouter().classify("any luck?", work_pending=False).intent is Intent.NEW_REQUEST


def test_unrecognised_utterance_falls_through_to_new_request():
    """The safe default: discard the work rather than risk narrating a stale
    result into an unrelated question."""
    c = UtteranceRouter().classify("what's your return policy", work_pending=True)
    assert c.intent is Intent.NEW_REQUEST
    assert c.keeps_work is False


def test_cancel_wins_over_constraint_when_both_could_match():
    """'stop, actually check the other one' contains a constraint phrase, but
    the user said stop first — keeping the work would be the wrong call."""
    c = UtteranceRouter().classify("stop, actually use the other one", work_pending=True)
    assert c.intent is Intent.CANCEL


# --- routing + fencing -----------------------------------------------------

def _tm_with_work(description="checking order 1002"):
    tm = TurnManager()
    tm.start_new_turn()             # turn 1: the user's original request
    work = tm.register_work(description)
    return tm, work


def test_new_request_supersedes_and_fences_the_work():
    tm, work = _tm_with_work()
    decision = tm.route_utterance("what's your return policy")
    assert decision.intent is Intent.NEW_REQUEST
    assert decision.superseded is True
    assert decision.work_kept is False
    assert tm.is_work_stale(work) is True   # result can never reach the user


def test_status_question_keeps_the_work_alive_across_the_turn_boundary():
    tm, work = _tm_with_work()
    decision = tm.route_utterance("are you still there")
    assert decision.intent is Intent.STATUS_REQUEST
    assert decision.work_kept is True
    assert decision.reply and "checking order 1002" in decision.reply
    # The turn advanced, but the work was carried forward onto it — so the
    # point-of-use fence still passes and the lookup is not wasted.
    assert tm.current_turn_id == 2
    assert work.turn_id == 2
    assert tm.is_work_stale(work) is False


def test_status_reply_needs_no_llm_round_trip():
    """The whole point is answering during tool work without paying another
    model round-trip on the interrupt path."""
    tm, _ = _tm_with_work()
    decision = tm.route_utterance("any luck?")
    assert decision.needs_llm is False
    assert decision.reply is not None


def test_constraint_is_attached_to_the_running_work_not_dropped():
    tm, work = _tm_with_work()
    decision = tm.route_utterance("actually make it the express shipping one")
    assert decision.intent is Intent.CONSTRAINT
    assert work.constraints == ["actually make it the express shipping one"]
    assert tm.is_work_stale(work) is False


def test_cancel_stops_the_work_and_fences_it():
    tm, work = _tm_with_work()
    decision = tm.route_utterance("never mind")
    assert decision.intent is Intent.CANCEL
    assert decision.work_kept is False
    assert work.cancelled is True
    assert tm.is_work_stale(work) is True
    assert "stopped" in decision.reply


def test_backchannel_keeps_work_and_says_nothing():
    tm, work = _tm_with_work()
    decision = tm.route_utterance("mhm")
    assert decision.intent is Intent.AFFIRMATION
    assert decision.reply is None
    assert tm.is_work_stale(work) is False


def test_carry_forward_is_recorded_in_the_audit_log():
    """Carry-forward is the one exception to 'an old stamp is stale', so
    every use of it has to be attributable after the fact."""
    tm, _ = _tm_with_work()
    tm.route_utterance("are you still there")
    events = [e["event"] for e in tm.audit_log()]
    assert "work_carried_forward" in events
    assert "utterance_routed" in events


def test_status_sentence_says_what_is_running_and_roughly_how_long():
    tm, work = _tm_with_work("checking order 1002")
    work.started_at -= 4  # pretend it has been running a while
    sentence = status_sentence(work)
    assert "checking order 1002" in sentence
    assert "seconds" in sentence


# --- the async path: does a kept lookup actually still deliver? -------------

@pytest.mark.asyncio
async def test_kept_work_delivers_its_result_after_a_status_question():
    from tools import _lookup_order_status_impl
    import os
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = "300"

    tm = TurnManager()
    tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))
    work.task = task

    await asyncio.sleep(0.05)
    decision = tm.route_utterance("are you still there")   # mid-flight
    assert decision.work_kept is True

    result = await task
    assert "delayed" in result


@pytest.mark.asyncio
async def test_superseded_work_never_returns_a_result():
    from tools import _lookup_order_status_impl
    from turn_manager import StaleResultError
    import os
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = "300"

    tm = TurnManager()
    tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))
    work.task = task

    await asyncio.sleep(0.05)
    tm.route_utterance("what's your return policy")        # user moved on

    with pytest.raises((StaleResultError, asyncio.CancelledError)):
        await task


@pytest.mark.asyncio
async def test_constraint_added_mid_flight_reaches_the_result():
    from tools import _lookup_order_status_impl
    import os
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = "300"

    tm = TurnManager()
    tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))
    work.task = task

    await asyncio.sleep(0.05)
    tm.route_utterance("also check the refund while you're at it")

    result = await task
    assert "refund" in result  # the constraint was applied, not discarded


@pytest.mark.asyncio
async def test_fence_holds_even_when_cancellation_lands_too_late():
    """The race the whole design exists for: the task completes despite
    cancel(). The point-of-use check must still block the result."""
    from tools import _lookup_order_status_impl
    from turn_manager import StaleResultError
    import os
    os.environ["STRESS_TEST_TOOL_DELAY_MS"] = "200"

    tm = TurnManager()
    tm.start_new_turn()
    work = tm.register_work("checking order 1002")
    task = asyncio.create_task(_lookup_order_status_impl(tm, "1002", work))

    # Deliberately do NOT attach the task to the work, so route_utterance has
    # nothing to cancel — this simulates cancellation failing to land, leaving
    # the point-of-use check as the only thing standing between a stale result
    # and the user.
    await asyncio.sleep(0.05)
    tm.route_utterance("what's your return policy")

    with pytest.raises(StaleResultError):
        await task
