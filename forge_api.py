#!/usr/bin/env python3
"""
Forge API — FastAPI backend for the ARIA Mission Control dashboard.

Endpoints
─────────
REST:
  GET  /                          health check
  GET  /orders                    all orders (enriched)
  GET  /orders/{order_id}         single order + forge run info
  POST /orders/{order_id}/approve approve order → In Production
  GET  /biofeedback               current EWMA score + recent events
  GET  /context                   one-shot full ForgeContextProvider snapshot

WebSocket:
  WS   /ws/context                broadcasts ForgeContextProvider.snapshot()
                                  every 2 seconds to all connected clients
  WS   /ws/chat                   streaming ARIA chat via Claude Code CLI

ARIA personality is injected into every chat message via the forge context.

Run:
    uvicorn forge_api:app --host 0.0.0.0 --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import eliza_memory
from forge_context_provider import ForgeContextProvider
from forge_manager_v1 import forge_manager_v1
from aria_voice_agent import build_aria_prompt, check_safety, ARIA_SYSTEM_PROMPT

import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ─── Paths ────────────────────────────────────────────────────────────────────
# FORGE_ORDERS_DIR can be overridden via env so the same image works both
# locally (~/Hardware_Factory/forge_orders) and inside Docker
# (/data/aria_forge or a bind-mounted host path).
_default_orders = Path.home() / "Hardware_Factory" / "forge_orders"
FORGE_ORDERS_DIR = Path(os.getenv("FORGE_ORDERS_DIR", str(_default_orders)))
FORGE_ORDERS_DIR.mkdir(parents=True, exist_ok=True)

# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Howell Forge API",
    description="ARIA Mission Control — ForgeContextProvider + WebSocket streaming",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # lock down to React dev server in production
    allow_methods=["*"],
    allow_headers=["*"],
)

_context_provider = ForgeContextProvider()
CLAUDE_CLI = Path.home() / ".local" / "bin" / "claude"

# ─── Connected WebSocket client sets ─────────────────────────────────────────

_context_clients: set[WebSocket] = set()
_chat_clients:    set[WebSocket] = set()


# ARIA personality is fully defined in aria_voice_agent.py
# build_aria_prompt() and check_safety() imported at top of file


# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {
        "status": "online",
        "agent":  "ARIA",
        "forge":  "Howell Forge Mission Control",
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


@app.get("/context")
def get_context():
    """One-shot full snapshot — same payload as the WebSocket broadcast."""
    return _context_provider.snapshot()


@app.get("/biofeedback")
def get_biofeedback():
    import biofeedback as bf
    return {
        "score":  round(bf.get_score(), 4),
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


@app.get("/orders")
def list_orders():
    orders = eliza_memory.get_all_orders()
    return {"total": len(orders), "orders": orders}


@app.get("/orders/{order_id}/hash")
def get_order_hash(order_id: str):
    """Return SHA-256 hashes + chain receipt for an order's forge outputs."""
    from forge_hash import hash_forge_outputs, push_hash_to_chain
    order_dir = FORGE_ORDERS_DIR / order_id

    # Return stored hashes.json if it exists (fast path)
    hashes_file = order_dir / "hashes.json"
    if hashes_file.exists():
        return json.loads(hashes_file.read_text())

    # Otherwise compute on-the-fly from existing files
    step  = order_dir / "part.step"
    gcode = order_dir / "part.gcode"
    stl   = order_dir / "part.stl"
    if not any(p.exists() for p in [step, gcode, stl]):
        raise HTTPException(404, f"No forge outputs found for {order_id}")

    hashes = hash_forge_outputs(
        step_path  = step  if step.exists()  else None,
        gcode_path = gcode if gcode.exists() else None,
        stl_path   = stl   if stl.exists()   else None,
    )
    return hashes


@app.get("/forge-file/{order_id}/{filename}")
def serve_forge_file(order_id: str, filename: str):
    """Serve STL, STEP, G-code, or PNG renders for an order."""
    allowed = {".stl", ".step", ".gcode", ".png"}
    p = Path(FORGE_ORDERS_DIR) / order_id / filename
    if not p.exists() or p.suffix.lower() not in allowed:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(p))


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = eliza_memory.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id!r} not found")
    from forge_context_provider import _forge_run_info
    return {**order, "forge_run": _forge_run_info(order_id)}


@app.post("/orders/{order_id}/approve")
def approve_order(order_id: str, body: dict = {}):
    """
    Trigger forge_manager_v1 for a PAID order — auto-approves (no human prompt).
    Intended for dashboard one-click approval after the operator has seen the renders.
    """
    order = eliza_memory.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id!r} not found")

    description = body.get("description") or order.get("raw_data", {}) or "custom part"
    if isinstance(description, dict):
        description = description.get("description", "custom part")

    # Run forge_manager with auto-approval flag
    import forge_manager_v1 as fm
    original_review = fm._human_review
    fm._human_review = lambda stl, gcode, val: True   # operator already approved via dashboard

    try:
        result = forge_manager_v1(order_id, str(description))
    finally:
        fm._human_review = original_review   # always restore

    return result


# ─── WebSocket: live context broadcast ───────────────────────────────────────

@app.websocket("/ws/context")
async def ws_context(websocket: WebSocket):
    """
    Broadcasts ForgeContextProvider.snapshot() to the client every 2 seconds.
    The React dashboard subscribes here for live order/biofeedback updates.
    """
    await websocket.accept()
    _context_clients.add(websocket)
    try:
        while True:
            snapshot = await asyncio.to_thread(_context_provider.snapshot)
            payload  = json.dumps(snapshot, default=str)
            await websocket.send_text(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    finally:
        _context_clients.discard(websocket)


# ─── WebSocket: ARIA streaming chat ──────────────────────────────────────────

async def _stream_claude(prompt: str) -> AsyncGenerator[str, None]:
    """
    Call Claude Code CLI with streaming output, yield chunks as they arrive.
    Falls back to a plain non-streaming call if streaming is unsupported.
    """
    cli = str(CLAUDE_CLI) if CLAUDE_CLI.exists() else "claude"
    proc = await asyncio.create_subprocess_exec(
        cli, "-p", prompt, "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(64)
        if not chunk:
            break
        yield chunk.decode("utf-8", errors="replace")
    await proc.wait()


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """
    ARIA streaming chat.

    Client sends: {"message": "your question"}
    Server sends: {"type": "chunk", "text": "..."} repeatedly,
                  then {"type": "done"} when response is complete,
                  or {"type": "error", "text": "..."} on failure.

    Every message gets the live ForgeContextProvider snapshot injected
    so ARIA always has current shop state.
    """
    await websocket.accept()
    _chat_clients.add(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"message": raw}

            user_msg = data.get("message", "").strip()
            if not user_msg:
                continue

            # Inject live forge context + ARIA full personality
            ctx    = await asyncio.to_thread(_context_provider.snapshot)
            prompt = build_aria_prompt(user_msg, ctx)

            # Stream response chunks to client
            try:
                async for chunk in _stream_claude(prompt):
                    await websocket.send_text(
                        json.dumps({"type": "chunk", "text": chunk})
                    )
                await websocket.send_text(json.dumps({"type": "done"}))
            except Exception as exc:
                await websocket.send_text(
                    json.dumps({"type": "error", "text": str(exc)[:200]})
                )

    except WebSocketDisconnect:
        pass
    finally:
        _chat_clients.discard(websocket)


# ─── Machine config ───────────────────────────────────────────────────────────

_MACHINE_CONFIG_PATH = Path(__file__).parent / "machine_config.json"

@app.get("/machine-config")
async def get_machine_config():
    """
    Serve machine_config.json to the dashboard and any agent that needs it.
    ForgeViewer.jsx can fetch this at startup to dynamically render fixtures
    from the active_layout without hardcoding them.
    """
    if not _MACHINE_CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="machine_config.json not found")
    return JSONResponse(json.loads(_MACHINE_CONFIG_PATH.read_text()))


@app.post("/machine-config/fixtures")
async def update_active_fixtures(body: dict):
    """
    Update the active_layout in machine_config.json.
    Called when a fixture is added, removed, or repositioned on the table.
    Also publishes a FIXTURE_UPDATE event so the dashboard refreshes live.

    Body: { "current_fixtures": [ { "id": ..., "type": ..., "position": [...], ... } ] }
    """
    if not _MACHINE_CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="machine_config.json not found")
    cfg = json.loads(_MACHINE_CONFIG_PATH.read_text())
    cfg["active_layout"] = body
    _MACHINE_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

    # Notify dashboard live
    try:
        import redis as _r
        rc = _r.Redis(host="localhost", port=6379, db=0, socket_timeout=1)
        rc.publish("mission_control_events", json.dumps({
            "type": "FIXTURE_UPDATE",
            "payload": body,
        }))
    except Exception:
        pass

    return {"ok": True, "fixtures": body.get("current_fixtures", [])}


# ─── WebSocket: Redis pub/sub event bridge ───────────────────────────────────

@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """
    Subscribes to the Redis 'mission_control_events' pub/sub channel and
    forwards every message to the connected React dashboard.

    Events published here:
      { "type": "CAMERA_MOVE",   "payload": { "position": [...], "target": [...] } }
      { "type": "TOGGLE_GROUP",  "payload": { "group": "workholding", "visible": false } }

    Published by:
      • voice_worker.py tools (set_dashboard_view, toggle_fixture_visibility)
      • Any future Python agent that calls _redis_publish()

    React listens on this socket via useForgeEvents.js and applies the payloads
    to the ForgeViewer camera and group visibility state.
    """
    await websocket.accept()
    channel = "mission_control_events"

    # Use a thread-based Redis pubsub subscriber to avoid blocking the event loop
    import redis as _redis_sync
    import threading

    r  = _redis_sync.Redis(host="localhost", port=6379, db=0)
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(channel)

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _reader():
        """Blocking reader thread — puts messages into the asyncio queue."""
        try:
            for msg in ps.listen():
                if msg and msg.get("type") == "message":
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    loop.call_soon_threadsafe(queue.put_nowait, data)
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        while True:
            raw = await queue.get()
            await websocket.send_text(raw)
    except WebSocketDisconnect:
        pass
    finally:
        ps.unsubscribe(channel)
        ps.close()


# ─── WebSocket: real-time safety check ───────────────────────────────────────

@app.websocket("/ws/safety")
async def ws_safety(websocket: WebSocket):
    """
    Safety interrupt channel — called by React on every interim transcript chunk.

    Client sends:  {"transcript": "let's shift the clamp to the left—"}
    Server sends:
      {"interrupt": false}
      — or —
      {"interrupt": true, "priority": 1, "category": "COLLISION",
       "message": "Interrupting, Chris. Moving left puts the bolt in the toolpath..."}

    The React voice hook stops TTS immediately on interrupt=true and
    speaks ARIA's message through SpeechSynthesis before resuming listening.
    """
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"transcript": raw}

            transcript = data.get("transcript", "").strip()
            if not transcript:
                await websocket.send_text(json.dumps({"interrupt": False}))
                continue

            # Get live context for financial checks + ShopConfig for fixture dims
            ctx    = await asyncio.to_thread(_context_provider.snapshot)
            from shop_config import shop_cfg as _shop_cfg
            result = await asyncio.to_thread(check_safety, transcript, ctx, _shop_cfg)
            await websocket.send_text(json.dumps(result))

    except WebSocketDisconnect:
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("forge_api:app", host="0.0.0.0", port=8765, reload=True)
