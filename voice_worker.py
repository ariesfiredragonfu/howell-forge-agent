#!/usr/bin/env python3
"""
voice_worker.py — ARIA LiveKit Voice Bridge for Howell Forge.

This is the "Ear" that lives in the LiveKit room with Chris.
It connects his microphone to the OpenAI Realtime API, using ARIA's full
personality and the live Forge Context as the system prompt.

Architecture:
  Mic ──▶ LiveKit Room ──▶ OpenAI Realtime API ──▶ Speaker
                    ▲                 │
                    │   ARIA prompt   │  Safety agent runs on
                    │   + live ctx    │  every interim transcript
                    └─────────────────┘

Turn Detection (from Gemini's design):
  Low Priority   → standard server-side VAD silence detection (allow_interruptions=True)
  Safety Override→ SafetyAgent runs on each transcript chunk; on interrupt=True
                   ARIA's current TTS is cancelled and the override fires immediately.

Setup:
  1. Copy .env.example → .env and fill in keys.
  2. Start a LiveKit server (Cloud or local dev):
       npx livekit-cli start-dev-server   (for quick local test)
  3. Run this worker:
       python3 voice_worker.py dev        (dev mode, single job)
       python3 voice_worker.py start      (production, connects to LiveKit Cloud)

Environment variables (.env):
  LIVEKIT_URL         wss://your-project.livekit.cloud
  LIVEKIT_API_KEY     your_livekit_api_key
  LIVEKIT_API_SECRET  your_livekit_api_secret
  OPENAI_API_KEY      sk-...
  FORGE_API_URL       http://localhost:8765   (for live context snapshots)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp
import redis
from dotenv import load_dotenv
from openai.types import realtime as rt

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
    llm,
)
from livekit.plugins.openai import realtime as lk_realtime

from aria_voice_agent import ARIA_SYSTEM_PROMPT, check_safety, build_aria_prompt
from shop_config import ShopConfig, shop_cfg as _default_shop_cfg
from aria_graph import run_aria_graph

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ARIA] %(message)s")
logger = logging.getLogger("aria.voice")

# ── Config ────────────────────────────────────────────────────────────────────

FORGE_API_URL = os.getenv("FORGE_API_URL", "http://localhost:8765")
VOICE         = "ballad"   # OpenAI Realtime voices: alloy, echo, shimmer, ballad, coral, marin
MODEL         = "gpt-4o-realtime-preview"

# Turn detection: end-of-speech sensitivity in seconds.
# Lower = snappier responses but may cut Chris off mid-thought.
# Higher = more patient but sluggish for fast back-and-forth.
ENDPOINTING_DELAY     = 0.6   # seconds of silence → end of turn (normal)
MAX_ENDPOINTING_DELAY = 2.5   # maximum wait even if Chris keeps talking quietly

# Redis channel — all mission control events flow through here
REDIS_CHANNEL = "mission_control_events"

# ── Camera view presets (mm, in ForgeViewer coordinate space) ─────────────────
#
# Machine: Howell_Forge_Main_CNC — 500×400×400mm
# Three.js: Y = up. Machine origin [0,0,0] = home. Table center = [250, 0, 200].
# All positions are Three.js camera positions; targets are orbit focus points.
#
# vise_01 center ≈ X76, Z50 (152.4/2 × 100/2) — used for collision_zoom target.

_TABLE_CENTER = [250.0, 0.0, 200.0]
_VISE_CENTER  = [76.2,  36.5, 50.0]   # vise_01 body center (x/2, h/2, z/2)

CAMERA_VIEWS: dict[str, dict] = {
    "top": {
        # Straight overhead, slight Z offset to avoid gimbal lock
        "position": [250.0, 900.0, 201.0],
        "target":   _TABLE_CENTER,
    },
    "side": {
        # +X side wall, eye-level with the vise
        "position": [1000.0, 120.0, 200.0],
        "target":   _TABLE_CENTER,
    },
    "front": {
        # +Y (operator's side, front of machine)
        "position": [250.0, 120.0, 900.0],
        "target":   _TABLE_CENTER,
    },
    "perspective": {
        # Default isometric — shows full table + vise
        "position": [600.0, 350.0, 600.0],
        "target":   _TABLE_CENTER,
    },
    "collision_zoom": {
        # Tight shot on vise_01 no-fly zone — for explaining collisions
        "position": [200.0, 130.0, 200.0],
        "target":   _VISE_CENTER,
    },
}


# ── Redis publisher (sync — runs in a thread from async context) ──────────────

def _redis_publish(event_type: str, payload: dict) -> None:
    """Fire-and-forget Redis publish. Fails gracefully if Redis is down."""
    try:
        r = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=1)
        r.publish(REDIS_CHANNEL, json.dumps({"type": event_type, "payload": payload}))
        logger.debug("Published %s → %s", event_type, payload)
    except Exception as exc:
        logger.warning("Redis publish failed (%s): %s", event_type, exc)


# ── Live Forge Context ────────────────────────────────────────────────────────

async def _fetch_forge_context(session: aiohttp.ClientSession) -> dict:
    """Pull a live snapshot from the Forge Context Provider API."""
    try:
        async with session.get(f"{FORGE_API_URL}/context", timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as exc:
        logger.warning("Context fetch failed: %s", exc)
    return {}


def _build_system_prompt(ctx: dict, shop: ShopConfig) -> str:
    """
    Inject the live Forge Context + live machine_config fixture layout
    into ARIA's system prompt. Called at session start and refreshed each turn.
    """
    shop.reload()   # hot-reload if machine_config.json changed on disk

    bf      = ctx.get("biofeedback",  {})
    fin     = ctx.get("finances",     {})
    ords    = ctx.get("orders",       {})
    ts      = ctx.get("timestamp",    "")

    score   = bf.get("score",  "?")
    health  = bf.get("health", "?")
    total   = ords.get("total", 0)
    paid    = ords.get("paid_count", 0)
    prod    = ords.get("in_production_count", 0)
    usdc    = fin.get("usdc")
    matic   = fin.get("matic")

    fin_str = f"USDC ${usdc:.2f} | MATIC {matic}" if usdc is not None else "wallet not configured"
    ts_str  = ts[11:19] if len(ts) > 19 else "--:--:--"

    context_block = f"""\
━━━ LIVE FORGE CONTEXT [{ts_str} UTC] ━━━
Machine:      {shop.machine_name} | {shop.envelope_summary()}
Active setup:
{shop.describe_active_layout()}
Biofeedback:  {score} EWMA ({health})
Orders:       {total} total | {paid} PAID | {prod} in production
Finances:     {fin_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return f"{ARIA_SYSTEM_PROMPT}\n{context_block}"


# ── ARIA Tool Definitions ─────────────────────────────────────────────────────

@llm.function_tool(
    name="lookup_fixture",
    description=(
        "Look up a workholding fixture by name and return its exact dimensions, "
        "no-fly zone boundaries, and current position on the table. "
        "Call this whenever Chris mentions a vise, clamp, or fixture by name "
        "so you can validate whether a part or tool move is safe. "
        "Examples: 'the vise', 'kurt vise', 'toe clamp', 'vise_01'."
    ),
)
async def lookup_fixture(name: str) -> str:
    """
    Resolve a fixture name from machine_config.json and return its specs.

    Args:
        name: Natural-language fixture name (e.g. 'the vise', 'toe clamp', 'vise_01').
    """
    _default_shop_cfg.reload()

    # Try active fixture ID first (e.g. "vise_01")
    by_id = _default_shop_cfg.get_active_fixture_by_id(name)
    if by_id and by_id.definition:
        d   = by_id.definition
        nfz = by_id.no_fly_zone
        return (
            f"{by_id.id} ({d.key.replace('_',' ')}) — "
            f"body {d.length:.1f}×{d.width:.1f}×{d.height:.1f}mm, "
            f"no-fly X{nfz['x_min']:.1f}→{nfz['x_max']:.1f}mm, "
            f"Z{nfz['z_min']:.1f}→{nfz['z_max']:.1f}mm, "
            f"height limit {nfz['height']:.1f}mm. "
            f"First safe tool X: {nfz['x_max']:.1f}mm."
        )

    # Try library name resolver
    description = _default_shop_cfg.describe_fixture(name)
    return description


@llm.function_tool(
    name="list_active_fixtures",
    description=(
        "List all fixtures currently active on the machine table "
        "with their positions and no-fly zone boundaries. "
        "Use this when Chris asks 'what's on the table?' or before "
        "planning a new setup."
    ),
)
async def list_active_fixtures() -> str:
    """Return all active fixtures from the current machine_config.json layout."""
    _default_shop_cfg.reload()
    layout = _default_shop_cfg.describe_active_layout()
    envelope = _default_shop_cfg.envelope_summary()
    count = len([f for f in _default_shop_cfg.active if f.status == "active"])
    return (
        f"{_default_shop_cfg.machine_name} — {envelope}\n"
        f"{count} active fixture(s):\n{layout}"
    )


@llm.function_tool(
    name="set_dashboard_view",
    description=(
        "Switch the Mission Control 3D viewport to a named camera angle. "
        "Use this whenever you want Chris to see the scene from a specific perspective "
        "— especially before explaining a collision or geometry issue. "
        "Supported views: top, side, front, perspective, collision_zoom."
    ),
)
async def set_dashboard_view(view_name: str) -> str:
    """
    Push a CAMERA_MOVE event to the React dashboard via Redis pub/sub.
    The ForgeViewer.jsx camera smoothly animates to the new position.

    Args:
        view_name: One of 'top', 'side', 'front', 'perspective', 'collision_zoom'.
    """
    key = view_name.strip().lower()
    view = CAMERA_VIEWS.get(key)
    if not view:
        available = ", ".join(CAMERA_VIEWS.keys())
        return (
            f"I don't have a preset for '{view_name}'. "
            f"Available views: {available}. Which one do you want?"
        )

    await asyncio.to_thread(_redis_publish, "CAMERA_MOVE", view)
    phrases = {
        "top":            "Switching to top-down — you can see the full table layout.",
        "side":           "Side view — good for checking Z-height and stock thickness.",
        "front":          "Front view — straight-on look at the Y-axis face.",
        "perspective":    "Back to isometric perspective.",
        "collision_zoom": "Zooming in on the work zone — watch the clamp clearance.",
    }
    return phrases.get(key, f"Switching to {view_name} view.")


@llm.function_tool(
    name="toggle_fixture_visibility",
    description=(
        "Show or hide a group of objects in the 3D viewport: "
        "'workholding' (clamps/vises), 'safezones' (collision boundaries), "
        "'environment' (table/grid), or 'part' (the active STL model). "
        "Use this to help Chris visualize where a collision would occur."
    ),
)
async def toggle_fixture_visibility(group: str, visible: bool) -> str:
    """
    Push a TOGGLE_GROUP event to the React dashboard.

    Args:
        group:   One of 'workholding', 'safezones', 'environment', 'part'.
        visible: True to show, False to hide.
    """
    valid = {"workholding", "safezones", "environment", "part"}
    key   = group.strip().lower()
    if key not in valid:
        return f"'{group}' isn't a valid group. Try: {', '.join(sorted(valid))}."

    await asyncio.to_thread(
        _redis_publish,
        "TOGGLE_GROUP",
        {"group": key, "visible": visible},
    )
    action = "showing" if visible else "hiding"
    labels = {
        "workholding": "clamps and vises",
        "safezones":   "collision boundary zones",
        "environment": "the machine table",
        "part":        "the part model",
    }
    return f"{action.capitalize()} {labels.get(key, key)} on the dashboard."


@llm.function_tool(
    name="get_forge_status",
    description="Get the current machine status, active order, and shop health snapshot.",
)
async def get_forge_status_tool() -> str:
    """Returns a human-readable forge status summary for ARIA to speak aloud."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{FORGE_API_URL}/context", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    ctx     = await r.json()
                    machine = ctx.get("forge_status", {}).get("status", "IDLE")
                    score   = ctx.get("biofeedback",  {}).get("score",  "?")
                    health  = ctx.get("biofeedback",  {}).get("health", "?")
                    paid    = ctx.get("orders",        {}).get("paid_count", 0)
                    prod    = ctx.get("orders",        {}).get("in_production_count", 0)
                    usdc    = ctx.get("finances",      {}).get("usdc")
                    fin_str = f"${usdc:.2f} USDC" if usdc is not None else "wallet offline"
                    return (
                        f"Machine is {machine}. "
                        f"Biofeedback {score} ({health}). "
                        f"{paid} paid orders waiting, {prod} in production. "
                        f"Wallet: {fin_str}."
                    )
    except Exception as exc:
        logger.warning("get_forge_status_tool failed: %s", exc)
    return "Forge API is offline — I can't pull live status right now."


# ── Safety Interrupt Handler ──────────────────────────────────────────────────

class _SafetyInterruptAgent(Agent):
    """
    ARIA agent with a built-in safety interrupt loop.

    On every transcribed user utterance the SafetyAgent runs.
    If a Priority 1/2/3 issue is detected, ARIA interrupts mid-thought
    and speaks the override message before responding normally.
    """

    def __init__(
        self,
        forge_ctx: dict,
        http_session: aiohttp.ClientSession,
        shop: ShopConfig,
    ):
        super().__init__(
            instructions=_build_system_prompt(forge_ctx, shop),
            llm=lk_realtime.RealtimeModel(
                model=MODEL,
                voice=VOICE,
                modalities=["text", "audio"],
                input_audio_transcription=rt.AudioTranscription(model="whisper-1"),
                turn_detection=rt.TurnDetection(
                    type="server_vad",
                    threshold=0.5,
                    silence_duration_ms=int(ENDPOINTING_DELAY * 1000),
                    create_response=True,
                ),
            ),
            tools=[
                lookup_fixture,
                list_active_fixtures,
                set_dashboard_view,
                toggle_fixture_visibility,
                get_forge_status_tool,
            ],
        )
        self._forge_ctx    = forge_ctx
        self._http_session = http_session
        self._shop         = shop

    async def on_user_turn_completed(
        self,
        turn_ctx,
        new_message,
    ) -> None:
        """
        Fires after the user finishes speaking and before ARIA responds.

        Runs the full LangGraph reasoning loop:
          Design → Safety Inspector → (Revise → Design)* → Approve

        If the graph produces a revision or abort, the result is prepended
        to the response context so ARIA speaks it before her normal reply.
        If the path was safe on the first pass, the graph is a no-op and
        ARIA responds naturally via the Realtime model.
        """
        transcript = ""
        for item in new_message.items:
            if hasattr(item, "text"):
                transcript += item.text or ""

        if transcript:
            # Refresh forge context and hot-reload shop config
            try:
                self._forge_ctx = await _fetch_forge_context(self._http_session)
            except Exception:
                pass
            self._shop.reload()

            try:
                # Run the stateful LangGraph reasoning loop.
                # session_id = room name → per-room checkpoint memory.
                graph_result = await run_aria_graph(
                    user_message = transcript,
                    forge_ctx    = self._forge_ctx,
                    shop         = self._shop,
                    session_id   = getattr(self, "_room_name", "default"),
                )

                # ForgeState field names (5 sections)
                iters     = graph_result.get("iteration",          0)
                action    = graph_result.get("action_type",        "none")
                response  = graph_result.get("response",           "")
                is_valid  = graph_result.get("is_geometry_valid",  True)
                notes     = graph_result.get("revision_notes",     [])
                dims      = graph_result.get("current_dimensions", {})
                fixtures  = graph_result.get("active_fixtures",    [])
                kaito     = graph_result.get("kaito_status",       "UNKNOWN")
                cost      = graph_result.get("estimated_cost",     0.0)
                cad       = graph_result.get("active_cad_path",    "")
                category  = graph_result.get("safety_category",    "SAFE")

                logger.info(
                    "[GRAPH] valid=%s iters=%d action=%s kaito=%s cost=$%.2f fixtures=%s",
                    is_valid, iters, action, kaito, cost, fixtures,
                )
                if cad:
                    logger.info("[GRAPH] CAD: %s", cad)

                # ── Revised or aborted → inject ARIA's graph response ─────────
                # On a safe first pass (iters=0), let the Realtime model respond
                # naturally — the graph was just a safety check, no injection needed.
                if iters > 0 or action == "abort":
                    if response:
                        logger.info("[GRAPH] Injecting: %s", response[:80])
                        turn_ctx.add_item(role="assistant", content=response)

                # ── Camera move side-effect ───────────────────────────────────
                if action == "camera_move":
                    move_x = dims.get("move_x")
                    move_y = dims.get("move_y")
                    move_z = dims.get("move_z")
                    if move_x is not None:
                        target = {
                            "position": [move_x, 80.0, move_z or 200.0],
                            "target":   [move_x, 0.0,  move_z or 200.0],
                        }
                        await asyncio.to_thread(
                            _redis_publish, "CAMERA_MOVE", target
                        )
                    elif move_y is not None:
                        target = {
                            "position": [250.0,  80.0, move_y],
                            "target":   [250.0,   0.0, move_y],
                        }
                        await asyncio.to_thread(
                            _redis_publish, "CAMERA_MOVE", target
                        )

            except Exception as exc:
                logger.warning("LangGraph run failed, falling back to direct safety check: %s", exc)
                # Fallback to single-shot safety check if graph errors
                result = check_safety(transcript, self._forge_ctx, shop_config=self._shop)
                if result.get("interrupt"):
                    message = result.get("message", "Interrupting, Chris.")
                    turn_ctx.add_item(role="assistant", content=message)

        # Refresh system prompt with latest context + fixture layout
        try:
            await self.update_instructions(
                _build_system_prompt(self._forge_ctx, self._shop)
            )
        except Exception as exc:
            logger.debug("Prompt refresh skipped: %s", exc)


# ── Job Entry Point ───────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    """
    Called by the LiveKit worker for each new job (room joining).
    One job = one conversation session with Chris.
    """
    logger.info("ARIA voice agent starting for room: %s", ctx.room.name)

    await ctx.connect()

    async with aiohttp.ClientSession() as http_session:
        # ── Load shop config (read-only from machine_config.json) ────────────
        shop = _default_shop_cfg
        shop.reload()
        logger.info(
            "ShopConfig: machine=%s envelope=%s fixtures=%d",
            shop.machine_name,
            shop.envelope_summary(),
            len([f for f in shop.active if f.status == "active"]),
        )
        for f in shop.active:
            if f.status == "active":
                nfz = f.no_fly_zone
                logger.info(
                    "  Fixture %-12s  no-fly X%.0f→%.0f  Z%.0f→%.0f",
                    f.id, nfz["x_min"], nfz["x_max"], nfz["z_min"], nfz["z_max"],
                )

        # ── Pull initial forge context ────────────────────────────────────────
        forge_ctx = await _fetch_forge_context(http_session)
        logger.info(
            "Forge context: machine=%s | biofeedback=%s | orders=%s",
            forge_ctx.get("forge_status", {}).get("status", "?"),
            forge_ctx.get("biofeedback",  {}).get("score",  "?"),
            forge_ctx.get("orders",       {}).get("total",  "?"),
        )

        agent = _SafetyInterruptAgent(forge_ctx, http_session, shop)
        agent._room_name = ctx.room.name   # used as LangGraph session_id
        session = AgentSession(
            # Let ARIA interrupt if someone speaks over her
            allow_interruptions=True,
            # Short interrupt — a single word "wait" is enough
            min_interruption_duration=0.4,
            min_interruption_words=1,
            # Endpointing — tuned for a loud shop
            min_endpointing_delay=ENDPOINTING_DELAY,
            max_endpointing_delay=MAX_ENDPOINTING_DELAY,
        )

        await session.start(
            agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # Subscribe to the first audio track published in the room
                # (Chris's microphone via the dashboard or a phone)
                noise_cancellation=True,
            ),
        )

        logger.info("ARIA is live in room '%s'. Listening.", ctx.room.name)

        # Keep running until the room empties or process is killed
        await session.wait()

    logger.info("ARIA session ended for room: %s", ctx.room.name)


# ── Worker bootstrap ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # Number of parallel sessions this process can host.
            # 1 is fine for a single-operator shop.
            num_idle_processes=1,
        )
    )
