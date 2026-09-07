"""
agent.py

Live pipeline: Silero VAD -> Deepgram STT -> Groq LLM -> Rime TTS -> playback,
with all three engineering directions wired in:

Direction 1 (perceived response time)
    Every pipeline stage is timestamped in ``latency.LatencyTracker``, from
    VAD end-of-speech to the first Rime audio frame handed to playback, so
    TTFA is a measured number with a per-stage breakdown rather than a
    feeling. Cold-start cost is moved off the first turn via ``prewarm_fnc``
    and a TTS websocket warmup, and ``GapCover`` covers a gap that runs long
    so the user hears the agent engage instead of silence.

Direction 2 (interruption and recovery)
    Staleness is enforced at point of use in BOTH the LLM node and the TTS
    node — audio frames stop being emitted the instant the turn advances, not
    only at the next token boundary — and ``PlaybackLedger`` reconciles the
    conversation history down to what the user actually heard, so later turns
    never reference a sentence that was cut off.

Direction 3 (continuity during tool work)
    A user utterance during a tool call is routed by intent
    (``TurnManager.route_utterance``) before any turn bookkeeping is applied:
    status questions and added constraints keep the call alive and are
    answered immediately, cancellation stops it, and only a genuinely new
    request supersedes and fences it.
"""

import asyncio
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import deepgram, silero, rime
from livekit.plugins.openai import LLM as GroqLLM  # Groq uses OpenAI-compatible API

import latency
from continuity import Intent
from latency import FillerPolicy, GapCover, LatencyTracker
from metrics import MetricsLog
from playback import PlaybackLedger
from tools import register_tools
from turn_manager import TurnManager

load_dotenv()

turn_manager = TurnManager()
latency_tracker = LatencyTracker(budget_ms=float(os.environ.get("TTFA_BUDGET_MS", "1200")))
playback_ledger = PlaybackLedger()
filler_policy = FillerPolicy(threshold_ms=float(os.environ.get("FILLER_THRESHOLD_MS", "700")))

# Set when the first audio frame of the current turn reaches playback; used by
# GapCover to stand down the moment real speech starts.
_audio_started = asyncio.Event()


def _on(session, event_name: str, handler):
    """Register an event handler tolerantly.

    LiveKit has renamed session events across releases. A handler that fails
    to attach must not take the whole agent down — but it must be loud, since
    a silently unattached handler is exactly how instrumentation rots into
    lying about what it measured.
    """
    try:
        session.on(event_name, handler)
        return True
    except Exception as e:
        print(f"[WARN] could not attach handler for {event_name!r}: {e} — "
              f"instrumentation depending on it will be missing, not wrong")
        return False


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a helpful, knowledgeable customer support voice "
                "assistant for order status inquiries. Give real, substantive "
                "answers — don't pad with filler, but don't strip out useful "
                "content either. Aim for 1-3 sentences: enough to actually "
                "answer the question well, phrased naturally for speech (no "
                "lists, no headers, no markdown). Be direct and specific "
                "rather than vague. Use the lookup_order_status tool whenever "
                "the user asks about an order.\n\n"
                "If a previous reply of yours is marked as interrupted, the "
                "user did not hear the part after the marker — do not refer "
                "back to it as if they did.\n\n"
                "Company policy facts you can use if asked:\n"
                "- Returns are accepted within 30 days of delivery, unused, "
                "with original packaging.\n"
                "- Refunds are issued to the original payment method within "
                "5-7 business days.\n"
                "- Standard shipping takes 3-5 business days; express is "
                "1-2 days.\n\n"
                "If asked about anything outside order status and these "
                "listed policies, be upfront that you don't have that "
                "information rather than guessing or inventing an answer."
            ),
            tools=[register_tools(turn_manager)],
        )

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Overrides the default llm_node so every streamed chunk is checked
        against the current turn id right before being yielded onward to TTS.
        This is the point-of-use staleness check — if a new turn has started
        (because the user interrupted), generation stops immediately instead
        of finishing the stale reply."""
        stamped_id = turn_manager.stamp()
        latency_tracker.mark(stamped_id, latency.LLM_REQUEST)
        print(f"[DEBUG] llm_node started, stamped_id={stamped_id}")
        chunk_count = 0
        async for chunk in super().llm_node(chat_ctx, tools, model_settings):
            chunk_count += 1
            if chunk_count == 1:
                ttft = latency_tracker.mark(stamped_id, latency.LLM_FIRST_TOKEN)
                if ttft is not None:
                    MetricsLog.record("llm_first_token", turn_id=stamped_id, ms_since_speech_end=ttft)
            if turn_manager.is_stale(stamped_id):
                print(f"[DEBUG] STALE after {chunk_count} chunks — stamped={stamped_id}, current={turn_manager.current_turn_id}")
                MetricsLog.record(
                    "stale_result_discarded",
                    turn_id=stamped_id,
                    current_turn_id=turn_manager.current_turn_id,
                    chunks_yielded_before_discard=chunk_count,
                    source="llm_node",
                )
                return
            yield chunk
        print(f"[DEBUG] llm_node finished normally, {chunk_count} chunks yielded")

    async def tts_node(self, text, model_settings):
        """Fences and instruments the audio path itself.

        Two things the LLM-level fence cannot do:

        * Stop audio that is ALREADY synthesized. By the time the turn
          advances, several sentences may be sitting in the TTS stream; this
          stops yielding frames immediately rather than draining them.
        * Know what the user actually heard. Every frame that is yielded is
          recorded in the playback ledger, and every piece of text sent to
          Rime is recorded alongside it, which is what makes the truncated
          history reconstruction possible on interrupt.
        """
        stamped_id = turn_manager.stamp()
        playback_ledger.begin_utterance(stamped_id)
        latency_tracker.mark(stamped_id, latency.TTS_REQUEST)

        async def _tracked_text():
            async for segment in text:
                playback_ledger.note_synthesized(stamped_id, segment)
                yield segment

        frame_count = 0
        async for frame in super().tts_node(_tracked_text(), model_settings):
            if turn_manager.is_stale(stamped_id):
                playback_ledger.note_interrupted(stamped_id)
                MetricsLog.record(
                    "stale_audio_discarded",
                    turn_id=stamped_id,
                    current_turn_id=turn_manager.current_turn_id,
                    frames_emitted_before_discard=frame_count,
                    source="tts_node",
                )
                return

            frame_count += 1
            if frame_count == 1:
                latency_tracker.mark(stamped_id, latency.TTS_FIRST_BYTE)
                ttfa = latency_tracker.mark(stamped_id, latency.FIRST_AUDIO_FRAME)
                _audio_started.set()
                if ttfa is not None:
                    MetricsLog.record("first_audio_frame", turn_id=stamped_id, ttfa_ms=ttfa)
                    print(f"[LATENCY] turn {stamped_id}: first audio at {ttfa}ms after speech end")

            samples = getattr(frame, "samples_per_channel", None)
            rate = getattr(frame, "sample_rate", None)
            if samples and rate:
                playback_ledger.note_frame(stamped_id, samples, rate)

            yield frame

        # Reached the end without going stale: everything synthesized was
        # handed to playback.
        if not turn_manager.is_stale(stamped_id):
            playback_ledger.note_finished(stamped_id)


async def _reconcile_history(session, agent, interrupted_turn_id: int):
    """Rewrite the last assistant message down to what the user actually
    heard, so the next turn reasons against the real conversation.

    Best-effort by nature — the ledger's cut point is an estimate, and the
    chat-context API has moved between LiveKit releases. A failure here
    degrades to the old behaviour (history keeps the full text) rather than
    breaking the session, and says so out loud.
    """
    report = playback_ledger.report(interrupted_turn_id)
    if not report or not report["interrupted"]:
        return
    heard = report["heard_text"]
    if heard is None:
        return

    MetricsLog.record(
        "history_reconciled",
        turn_id=interrupted_turn_id,
        played_fraction=report["played_fraction"],
        confidence=report["confidence"],
        heard_chars=len(heard),
        synthesized_chars=report["synthesized_chars"],
    )

    try:
        chat_ctx = agent.chat_ctx.copy()
        items = getattr(chat_ctx, "items", None)
        if not items:
            return
        for item in reversed(items):
            if getattr(item, "role", None) != "assistant":
                continue
            if hasattr(item, "content") and isinstance(item.content, list):
                item.content = [heard] if heard else []
            elif hasattr(item, "text_content"):
                item.text_content = heard
            break
        await agent.update_chat_ctx(chat_ctx)
        print(f"[RECOVERY] history truncated to what was heard "
              f"({report['played_fraction']:.0%} of turn {interrupted_turn_id}, "
              f"confidence={report['confidence']})")
    except Exception as e:
        print(f"[WARN] history reconciliation skipped ({type(e).__name__}: {e}); "
              f"history still holds the full un-heard text for turn {interrupted_turn_id}")


def prewarm(proc: agents.JobProcess):
    """Cold start is latency the FIRST user turn pays and no later turn does,
    which makes it invisible in a p50 and very visible to a real caller.
    Loading the VAD model here moves it into process startup, before anyone
    is on the line."""
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=0.5,  # give slightly more room before declaring end-of-speech
    )


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    stress_delay = os.environ.get("STRESS_TEST_TOOL_DELAY_MS", "0")
    print(f"[CONFIG] LLM=openai/gpt-oss-20b (Groq) | TTS=Rime mistv2/cove (websocket) | "
          f"STT=Deepgram nova-3 | STRESS_TEST_TOOL_DELAY_MS={stress_delay} | "
          f"TTFA_BUDGET_MS={latency_tracker.budget_ms} | "
          f"FILLER_THRESHOLD_MS={filler_policy.threshold_ms}")

    vad = ctx.proc.userdata.get("vad") if hasattr(ctx, "proc") else None
    if vad is None:
        vad = silero.VAD.load(min_silence_duration=0.5)

    session = AgentSession(
        vad=vad,
        stt=deepgram.STT(model="nova-3"),
        llm=GroqLLM(
            model="openai/gpt-oss-20b",
            base_url="https://api.groq.com/openai/v1",
            api_key=os.environ["GROQ_API_KEY"],
        ),
        tts=rime.TTS(
            model="mistv2",
            speaker="cove",
            use_websocket=True,
            sample_rate=48000,  # match LiveKit's typical room audio rate explicitly,
                                 # to reduce how often the native resampler has to run —
                                 # a likely trigger for the soxr FFT-cache race crash
        ),
        # Force plain VAD-based turn detection instead of LiveKit Cloud's
        # adaptive/inference-based detector, which was observed timing out
        # mid-session ("interruption inference timed out after 0.7s") and
        # silently degrading — that degradation was the likely cause of
        # words being cut off. Skipping straight to VAD avoids depending
        # on that network round-trip at all.
        turn_detection="vad",
        # Endpointing delay is pure additive latency on every single turn: the
        # user has stopped speaking and nothing is happening yet. 0.5s is the
        # floor we found that does not start clipping people mid-sentence;
        # dropping it further trades barge-in correctness for TTFA, which is a
        # bad trade for a support agent. Measured as the first row of the
        # latency breakdown, so the tradeoff stays visible.
        min_endpointing_delay=0.5,
    )

    assistant = Assistant()

    gap_cover = GapCover(
        filler_policy,
        speak=lambda phrase: session.say(phrase, allow_interruptions=True),
        tracker=latency_tracker,
    )

    def _begin_turn_timing(_event=None):
        """VAD end-of-speech: the clock the user perceives starts HERE, not
        when the transcript lands. The turn id is not allocated until the
        transcript commits, so it is predicted — route_utterance() always
        advances by exactly one."""
        next_turn_id = turn_manager.current_turn_id + 1
        latency_tracker.begin_turn(next_turn_id)
        _audio_started.clear()

    def on_user_state_changed(event):
        state = getattr(event, "new_state", None)
        if state == "listening":  # user stopped speaking
            _begin_turn_timing(event)

    # Preferred: explicit user-stopped-speaking signal. Falls back to the
    # state-change event on releases that only expose that one.
    if not _on(session, "user_stopped_speaking", _begin_turn_timing):
        _on(session, "user_state_changed", on_user_state_changed)

    def on_user_turn(event):
        # Fires when a user utterance is finalized/committed — this is what
        # marks the previous turn obsolete.
        if not getattr(event, "is_final", True):
            return

        transcript = getattr(event, "transcript", "") or ""
        previous_turn = turn_manager.current_turn_id

        # Route BEFORE any turn bookkeeping: whether in-flight tool work
        # survives depends on what the user actually said, and that decision
        # has to be made while the work is still registered.
        decision = turn_manager.route_utterance(transcript)
        new_id = decision.turn_id
        latency_tracker.mark(new_id, latency.STT_FINAL)
        print(f"[DEBUG] route_utterance -> {decision}, transcript={transcript!r}")

        MetricsLog.record(
            "utterance_routed",
            turn_id=previous_turn,
            new_turn_id=new_id,
            intent=decision.intent.value,
            work_kept=decision.work_kept,
        )

        if previous_turn > 0:
            # Anything that was already speaking gets stopped regardless of
            # intent — the user is talking, so the agent stops. What differs
            # by intent is what happens to the in-flight TOOL WORK, which
            # route_utterance has already decided.
            MetricsLog.record("interrupt_detected", turn_id=previous_turn, new_turn_id=new_id)
            playback_ledger.note_interrupted(previous_turn)
            latency_tracker.mark_aborted(previous_turn)

            try:
                session.interrupt()
            except Exception as e:
                print(f"[DEBUG] session.interrupt() raised (likely nothing was playing): {e}")
            MetricsLog.record("audio_stopped", turn_id=previous_turn, new_turn_id=new_id)

            # Bring the conversation record in line with what was actually
            # heard before the cut.
            asyncio.create_task(_reconcile_history(session, assistant, previous_turn))
        else:
            session.interrupt()  # still safe/harmless to call, just not logged as evidence

        # An immediate spoken answer for status / cancel / constraint. This is
        # what keeps the session responsive during a slow tool call: the user
        # gets a real reply about the work in flight without the work being
        # thrown away and without waiting on an LLM round-trip.
        if decision.reply:
            MetricsLog.record("continuity_reply", turn_id=new_id, intent=decision.intent.value,
                              reply=decision.reply)
            asyncio.create_task(session.say(decision.reply, allow_interruptions=True))

        if decision.needs_llm:
            # Cover the gap if the real answer runs long. Cancels itself the
            # moment audio starts or the turn goes stale.
            asyncio.create_task(
                gap_cover.run(
                    new_id,
                    _audio_started,
                    is_stale=lambda t=new_id: turn_manager.is_stale(t),
                )
            )

    _on(session, "user_input_transcribed", on_user_turn)

    def on_item_added(event):
        # Track whether an assistant item was ever added for a turn that
        # had already been superseded — this is the direct evidence check
        # for "stale tool/LLM results are never spoken."
        role = getattr(event.item, "role", None)
        if role == "assistant":
            MetricsLog.record(
                "assistant_item_added",
                current_turn_id=turn_manager.current_turn_id,
                text=getattr(event.item, "text_content", None),
            )

    _on(session, "conversation_item_added", on_item_added)

    async def _dump_evidence():
        """Write both logs on shutdown so a live run leaves the same shape of
        evidence the offline acceptance test does."""
        try:
            MetricsLog.export_json("live_session_metrics.json")
            latency_tracker.export_json("live_session_latency.json")
            summary = latency_tracker.summary()
            print("\n[LATENCY SUMMARY]")
            print(f"  turns measured: {summary['turns_measured']}  "
                  f"budget: {summary['budget_ms']}ms  "
                  f"over budget: {len(summary['over_budget_turns'])}")
            if summary["ttfa"]:
                print(f"  TTFA p50={summary['ttfa']['p50_ms']}ms  p95={summary['ttfa']['p95_ms']}ms")
            for stage, s in summary["stages"].items():
                print(f"    {stage:<18} p50={s['p50_ms']:>7}ms  p95={s['p95_ms']:>7}ms  ({s['what']})")
            slowest = latency_tracker.slowest_stage()
            if slowest:
                print(f"  slowest stage: {slowest}")
        except Exception as e:
            print(f"[WARN] evidence dump failed: {e}")

    try:
        ctx.add_shutdown_callback(_dump_evidence)
    except Exception:
        pass

    await session.start(
        room=ctx.room,
        agent=assistant,
        room_input_options=RoomInputOptions(),
    )

    await session.generate_reply(
        instructions="Greet the user briefly and ask how you can help."
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm)
    )
