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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

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


# ─── ARIA system prompt ───────────────────────────────────────────────────────

_ARIA_SYSTEM = """\
You are ARIA, the floor presence at Howell Forge — a precision CNC machine shop.
You talk directly with the owner, Christopher.

Personality:
- Direct. No filler sentences. Say the thing.
- Technically sharp. You know G-code, EWMA biofeedback, FreeCAD, Polygon/Kaito payments.
- Dry wit. If something is obviously wrong, say so plainly.
- Loyal. This is Christopher's shop. You're on his side, not performing neutrality.
- Occasionally opinionated. You will flag bad toolpaths, risky G-code, or suspicious orders.

You have live access to the forge context (orders, biofeedback score, recent events,
forge run results) injected at the start of each message. Use it.

When the shop is healthy → be brief and confident.
When something needs attention → say it first, explain second.
When Christopher asks you to do something technical → do it, don't ask permission.
"""


def _build_aria_prompt(user_message: str, context: dict) -> str:
    """Inject live forge context into every ARIA message."""
    bf    = context.get("biofeedback", {})
    ords  = context.get("orders", {})
    score = bf.get("score", "?")
    health = bf.get("health", "?")
    total  = ords.get("total", 0)
    paid   = ords.get("paid_count", 0)
    prod   = ords.get("in_production_count", 0)
    pending = ords.get("pending_count", 0)

    # Recent biofeedback events summary
    recent = bf.get("recent_events", [])[:5]
    events_str = ", ".join(
        f"{e.get('type','?')}({e.get('agent','?')})" for e in recent
    ) or "none"

    ctx_block = f"""\
[LIVE FORGE CONTEXT — {context.get('timestamp', '')}]
Biofeedback EWMA: {score} | Health: {health}
Orders — Total: {total} | PAID: {paid} | In Production: {prod} | Pending: {pending}
Recent events: {events_str}
"""

    return f"{_ARIA_SYSTEM}\n\n{ctx_block}\n\nChristopher: {user_message}"


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


@app.get("/forge-file/{order_id}/{filename}")
def serve_forge_file(order_id: str, filename: str):
    """Serve STL, STEP, G-code, or PNG renders for an order."""
    allowed = {".stl", ".step", ".gcode", ".png"}
    p = Path(FORGE_ORDERS_DIR) / order_id / filename
    if not p.exists() or p.suffix.lower() not in allowed:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(p))


FORGE_ORDERS_DIR = Path.home() / "Hardware_Factory" / "forge_orders"


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

            # Inject live forge context
            ctx    = await asyncio.to_thread(_context_provider.snapshot)
            prompt = _build_aria_prompt(user_msg, ctx)

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


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("forge_api:app", host="0.0.0.0", port=8765, reload=True)
