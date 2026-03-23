# 02 — IVR (DTMF) Agent

Demonstrates a **multi-level IVR menu** that accepts both **DTMF key presses** and **spoken responses**. The agent also supports **sending DTMF tones upstream** to navigate carrier IVR systems on behalf of the caller.

---

## What It Does

```
Caller picks up
      ↓
Agent plays main menu:
  "Press 1 for Sales, 2 for Billing, 3 for Support, 0 to repeat"
      ↓
Caller presses 3 (or says "support")
      ↓
Agent plays support sub-menu:
  "Press 1 for Hardware, 2 for Software, 0 to go back"
      ↓
Caller presses 2
      ↓
Agent: "Routing you to Software Support"
```

---

## Menu Structure

```
Main Menu
├── 1 → Sales
├── 2 → Billing
├── 3 → Technical Support
│       ├── 1 → Hardware support
│       └── 2 → Software support
└── 0 → Repeat menu
```

---

## Key Concepts

| Concept | API |
|---------|-----|
| Receive DTMF from caller | `room.on("sip_dtmf_received", handler)` |
| Send DTMF upstream | `local_participant.publish_dtmf(code, digit)` |
| LLM routes spoken input | `@llm.function_tool route_choice(digit)` |
| IVR state across turns | `AgentSession[IVRState](userdata=IVRState())` |

---

## Environment Variables

```bash
# LiveKit Cloud
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxx

# OpenAI
OPENAI_API_KEY=sk-xxxx

# Deepgram
DEEPGRAM_API_KEY=xxxx

# Vobiz SIP
VOBIZ_SIP_DOMAIN=xxxx.sip.vobiz.ai
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxx
```

---

## Setup

```bash
source ".venv/bin/activate"   # from repo root
cd 02-ivr-dtmf-agent
```

Or install manually:
```bash
pip install livekit-agents livekit-plugins-openai livekit-plugins-deepgram \
            livekit-plugins-silero livekit-plugins-noise-cancellation python-dotenv
```

---

## Running

### Step 1 — Start the agent worker

```bash
python agent.py start
```

```
INFO  registered worker  agent_name=ivr-agent
```

### Step 2 — Place a call

```bash
python ../make_call.py --to +91XXXXXXXXXX --agent ivr-agent
```

### Step 3 — Test it

**DTMF (key press):** press buttons on your dialpad  
**Voice:** say "sales", "billing", "support", "one", "two", "go back"

---

## How DTMF Reception Works

```python
# Registers on the LiveKit room — fires for every key press
@ctx.room.on("sip_dtmf_received")
def on_dtmf(dtmf: rtc.SipDTMF):
    # dtmf.digit → "1", "2", "#", "*"
    # dtmf.code  → RFC 4733 code (0–11)
    asyncio.ensure_future(
        _handle_digit(dtmf.digit, state, session, ctx)
    )
```

Both DTMF and spoken input route through the same `_handle_digit()` function — the LLM parses spoken choices ("press two" → `"2"`) and calls `route_choice("2")`.

## How Sending DTMF Upstream Works

```python
# Send tones to a carrier IVR on behalf of the caller
@llm.function_tool(description="Send DTMF tones to an upstream IVR")
async def send_dtmf(self, context, digits: str) -> str:
    local = self._ctx.room.local_participant
    for ch in digits:
        code = DTMF_MAP.get(ch)          # "1" → 1, "#" → 11
        await local.publish_dtmf(code=code, digit=ch)
        await asyncio.sleep(0.15)        # 150ms gap between tones
```

Use case: caller asks agent to "navigate the Airtel menu and get to billing" — agent dials Airtel, detects IVR, sends `1`, `3`, `#` automatically.

---

## IVR State

```python
@dataclass
class IVRState:
    menu_level: str = "main"        # "main" | "support"
    digits_collected: list[str] = field(default_factory=list)

# Passed to AgentSession as typed userdata
session = AgentSession[IVRState](
    ...
    userdata=IVRState(),
)
```

State is accessible in tools via `RunContext[IVRState]` and persists across the entire call.

---

## Enabling Auto-IVR Detection

Uncomment this line in `agent.py` to enable LiveKit's automatic upstream IVR detection:

```python
session = AgentSession[IVRState](
    ...
    # ivr_detection=True,   ← uncomment this
)
```

When enabled, LiveKit detects if the called number plays an automated IVR system and signals your agent.

---

## Customising

**Add a new menu option:**
```python
# In _handle_digit(), main menu section:
elif digit == "4":
    return await _reply(session, "Connecting you to Finance.")
```

**Change menu text:**
```python
MAIN_MENU = (
    "Welcome to ACME Corp. "
    "Press 1 for Sales. "
    "Press 2 for Support. "
)
```

---

## Docs

- [LiveKit DTMF](https://docs.livekit.io/telephony/features/dtmf/)
- [SipDTMF reference](https://docs.livekit.io/reference/python/livekit/rtc/)
- [publish_dtmf](https://docs.livekit.io/reference/python/livekit/rtc/#livekit.rtc.LocalParticipant.publish_dtmf)
