"""
02 - IVR (DTMF) Agent
=====================
Demonstrates a multi-level IVR menu that accepts both DTMF key presses
and spoken responses.

Flow:
  1. Agent answers the call and plays the main menu.
  2. Caller presses a DTMF digit or says their choice.
  3. Agent routes to the correct sub-menu or action.
  4. Agent can also send DTMF tones to upstream systems (e.g., to navigate
     a carrier IVR while on behalf of the caller).

LiveKit DTMF docs:
  https://docs.livekit.io/telephony/features/dtmf/

Key APIs used:
  - AgentSession(ivr_detection=True)        – auto-detects upstream IVRs
  - room.on("sip_dtmf_received", handler)   – listen for caller key presses
  - local_participant.publish_dtmf()        – send DTMF upstream

Menu layout:
  Main menu
  ├── 1 → Sales
  ├── 2 → Billing
  ├── 3 → Support
  │       ├── 1 → Hardware support
  │       └── 2 → Software support
  └── 0 → Repeat menu
"""

import asyncio
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions, llm
from livekit.plugins import deepgram, noise_cancellation, openai, silero

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ivr-agent")

OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")

# DTMF digit → code mapping (RFC 4733)
DTMF_MAP: dict[str, int] = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "*": 10, "#": 11,
}

# ---------------------------------------------------------------------------
# IVR Menu text
# ---------------------------------------------------------------------------

MAIN_MENU = (
    "Welcome to Vobiz. "
    "Press or say 1 for Sales. "
    "Press or say 2 for Billing. "
    "Press or say 3 for Technical Support. "
    "Press or say 0 to repeat this menu."
)

SUPPORT_MENU = (
    "For hardware support press or say 1. "
    "For software support press or say 2. "
    "To go back to the main menu press or say 0."
)


# ---------------------------------------------------------------------------
# IVR state — stored in AgentSession.userdata
# ---------------------------------------------------------------------------

class IVRState:
    def __init__(self) -> None:
        self.menu_level: str = "main"   # "main" | "support"
        self.digits_collected: list[str] = []


# ---------------------------------------------------------------------------
# IVR Agent
# ---------------------------------------------------------------------------

class IVRAgent(Agent):
    """
    Stateful IVR agent driven by both DTMF and voice input.

    It exposes an LLM tool so GPT-4o-mini can route spoken choices the same
    way DTMF events route key presses.
    """

    def __init__(self) -> None:
        super().__init__(
            instructions="""
            You are an IVR voice assistant for Vobiz.

            You present a menu and route callers based on what they say or press.
            Available choices depend on the current menu level stored in context.

            Main menu options:
              1 → Sales
              2 → Billing
              3 → Technical Support
              0 → Repeat menu

            Support sub-menu options (when menu_level is "support"):
              1 → Hardware support
              2 → Software support
              0 → Back to main menu

            When the caller states a choice, call the route_choice tool with
            the matching digit as a string (e.g., "1", "2", "3").

            Rules:
            - Keep responses short.
            - Always re-read the menu after routing if you cannot determine intent.
            - Never guess; ask for clarification if unclear.
            """
        )

    async def on_enter(self) -> None:
        """Read the main menu on session start."""
        await self.session.generate_reply(instructions=MAIN_MENU)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class IVRTools(llm.ToolContext):
    def __init__(self, ctx: agents.JobContext, session_ref: list) -> None:
        """
        session_ref is a mutable list so we can inject the AgentSession
        after it's created (circular dependency workaround).
        """
        super().__init__(tools=[])
        self._ctx = ctx
        self._session_ref = session_ref  # [AgentSession] once available

    @property
    def _session(self) -> Optional[AgentSession]:
        return self._session_ref[0] if self._session_ref else None

    @llm.function_tool(
        description=(
            "Route the caller to a department or repeat the menu based on "
            "their spoken choice. Pass the digit that corresponds to the option "
            "they selected (e.g. '1' for Sales, '2' for Billing, '3' for Support, '0' to repeat)."
        )
    )
    async def route_choice(self, digit: str) -> str:
        """
        Args:
            digit: Single character — '0'–'9', '*', or '#'.
        """
        if not self._session:
            return "Session not ready."

        state: IVRState = self._session.userdata
        return await _handle_digit(digit, state, self._session, self._ctx)

    @llm.function_tool(
        description=(
            "Send DTMF tones to an upstream system on behalf of the caller. "
            "Use this to navigate a carrier IVR or enter an extension."
        )
    )
    async def send_dtmf(self, digits: str) -> str:
        """
        Args:
            digits: String of digits/symbols to send (e.g., '1234#').
        """
        local = self._ctx.room.local_participant
        for ch in digits:
            code = DTMF_MAP.get(ch)
            if code is not None:
                await local.publish_dtmf(code=code, digit=ch)
                await asyncio.sleep(0.15)  # 150 ms gap between tones
        logger.info("Sent DTMF: %s", digits)
        return f"Sent DTMF: {digits}"


# ---------------------------------------------------------------------------
# Routing logic (shared between DTMF event and LLM tool)
# ---------------------------------------------------------------------------

async def _handle_digit(
    digit: str,
    state: IVRState,
    session: AgentSession,
    ctx: agents.JobContext,
) -> str:
    digit = digit.strip()

    if state.menu_level == "main":
        if digit == "1":
            state.menu_level = "main"
            return await _reply(session, "Connecting you to Sales. Please hold.")
        elif digit == "2":
            state.menu_level = "main"
            return await _reply(session, "Connecting you to Billing. Please hold.")
        elif digit == "3":
            state.menu_level = "support"
            return await _reply(session, SUPPORT_MENU)
        elif digit == "0":
            return await _reply(session, MAIN_MENU)
        else:
            return await _reply(session, f"Sorry, {digit} is not a valid option. " + MAIN_MENU)

    elif state.menu_level == "support":
        if digit == "1":
            state.menu_level = "main"
            return await _reply(session, "Routing you to Hardware Support.")
        elif digit == "2":
            state.menu_level = "main"
            return await _reply(session, "Routing you to Software Support.")
        elif digit == "0":
            state.menu_level = "main"
            return await _reply(session, MAIN_MENU)
        else:
            return await _reply(session, f"Sorry, {digit} is not valid. " + SUPPORT_MENU)

    return "Unknown menu state."


async def _reply(session: AgentSession, text: str) -> str:
    await session.generate_reply(instructions=text)
    return text


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: agents.JobContext):
    logger.info("Room: %s", ctx.room.name)

    phone_number: Optional[str] = None
    try:
        if ctx.job.metadata:
            phone_number = json.loads(ctx.job.metadata).get("phone_number")
    except Exception:
        pass

    # Mutable reference so IVRTools can access session after construction
    session_ref: list = []

    tools = IVRTools(ctx, session_ref)

    session = AgentSession[IVRState](
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(model="tts-1", voice="alloy"),
        vad=silero.VAD.load(),
        tools=tools.all_tools,
        userdata=IVRState(),
        # ivr_detection=True,  # Uncomment to auto-detect upstream IVR systems
    )
    session_ref.append(session)

    # Register DTMF event listener — fires when the caller presses a key
    @ctx.room.on("sip_dtmf_received")
    def on_dtmf(dtmf: rtc.SipDTMF):
        logger.info("DTMF from %s: digit=%s code=%s", dtmf.participant.identity, dtmf.digit, dtmf.code)
        state: IVRState = session.userdata
        asyncio.ensure_future(
            _handle_digit(dtmf.digit, state, session, ctx)
        )

    await session.start(
        room=ctx.room,
        agent=IVRAgent(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    if phone_number:
        logger.info("Dialing %s …", phone_number)
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=phone_number,
                participant_identity=f"sip_{phone_number}",
                wait_until_answered=True,
            )
        )
        logger.info("Call answered — IVR menu will play via on_enter().")
    else:
        logger.info("Inbound call — IVR menu fires via on_enter().")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="ivr-agent",
        )
    )
