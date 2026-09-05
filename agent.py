import os
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import deepgram, silero, rime
from livekit.plugins.openai import LLM as GroqLLM  # Groq uses OpenAI-compatible API

from turn_manager import TurnManager
from tools import register_tools
from metrics import MetricsLog

load_dotenv()

turn_manager = TurnManager()


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
        print(f"[DEBUG] llm_node started, stamped_id={stamped_id}")
        chunk_count = 0
        async for chunk in super().llm_node(chat_ctx, tools, model_settings):
            chunk_count += 1
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


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    stress_delay = os.environ.get("STRESS_TEST_TOOL_DELAY_MS", "0")
    print(f"[CONFIG] LLM=openai/gpt-oss-20b (Groq) | TTS=Rime mistv2/cove (websocket) | "
          f"STT=Deepgram nova-3 | STRESS_TEST_TOOL_DELAY_MS={stress_delay}")

    session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=0.5,  # give slightly more room before declaring end-of-speech
        ),
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
        min_endpointing_delay=0.5,
    )

    @session.on("user_input_transcribed")
    def on_user_turn(event):
        # Fires when a user utterance is finalized/committed — this is what
        # marks the previous turn obsolete. Check LiveKit's current docs if
        # this event name has changed; the concept (start_new_turn on every
        # committed user utterance) is what matters.
        if getattr(event, "is_final", True):
            previous_turn = turn_manager.current_turn_id
            new_id = turn_manager.start_new_turn()
            print(f"[DEBUG] start_new_turn() -> {new_id}, transcript={event.transcript!r}")

            # Explicit audio cancellation: this project's assigned
            # audio-cancellation piece. Rather than relying only on
            # LiveKit's implicit VAD-triggered barge-in, we explicitly
            # stop any current agent speech the moment a new turn is
            # committed — this is the call that satisfies "queued Rime
            # audio stops immediately" from the acceptance test, tied
            # directly to our own turn logic rather than an implicit
            # side effect.
            #
            # Skip logging on turn 0->1 (the very first user utterance,
            # right after the greeting) — there's nothing genuinely being
            # interrupted yet at that point, so counting it would pollute
            # the audio-stop-latency evidence with a meaningless near-zero
            # measurement.
            if previous_turn > 0:
                MetricsLog.record("interrupt_detected", turn_id=previous_turn, new_turn_id=new_id)

                # This is the fix for the bug where an interrupted tool-call
                # continuation could still get narrated: cancelling only the
                # tool's own task stops its delay dead immediately, while
                # leaving the broader generation task alone so the
                # framework's own handling of the NEW turn isn't disrupted.
                turn_manager.cancel_active_tool_task()

                try:
                    session.interrupt()
                except Exception as e:
                    print(f"[DEBUG] session.interrupt() raised (likely nothing was playing): {e}")
                MetricsLog.record("audio_stopped", turn_id=previous_turn, new_turn_id=new_id)
            else:
                session.interrupt()  # still safe/harmless to call, just not logged as evidence

    @session.on("conversation_item_added")
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

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(),
    )

    await session.generate_reply(
        instructions="Greet the user briefly and ask how you can help."
    )


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
