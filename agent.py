import os
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import deepgram, silero, rime
from livekit.plugins.openai import LLM as GroqLLM  # Groq uses OpenAI-compatible API

from turn_manager import TurnManager

load_dotenv()

turn_manager = TurnManager()


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="You are a voice assistant. Respond in one short sentence, no more than 15 words."
        )

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Overrides the default llm_node so every streamed chunk is checked
        against the current turn id right before being yielded onward to TTS.
        This is the point-of-use staleness check — if a new turn has started
        (because the user interrupted), generation stops immediately instead
        of finishing the stale reply."""
        stamped_id = turn_manager.stamp()
        async for chunk in super().llm_node(chat_ctx, tools, model_settings):
            if turn_manager.is_stale(stamped_id):
                return
            yield chunk


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    session = AgentSession(
        vad=silero.VAD.load(),
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
        ),
    )

    @session.on("user_input_transcribed")
    def on_user_turn(event):
        # Fires when a user utterance is finalized/committed — this is what
        # marks the previous turn obsolete. Check LiveKit's current docs if
        # this event name has changed; the concept (start_new_turn on every
        # committed user utterance) is what matters.
        if getattr(event, "is_final", True):
            turn_manager.start_new_turn()

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
