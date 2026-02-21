#!/usr/bin/env sh
# ══════════════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh — Howell Forge container startup
#
# 1. Starts a persistent Xvfb virtual display on :99
#    FreeCAD's Qt layer needs *some* display server even in headless mode.
#    Starting it once here is more reliable than wrapping every freecadcmd
#    call with xvfb-run (which allocates a new display per invocation).
#
# 2. Waits briefly for the display to be ready.
#
# 3. exec's the CMD — PID 1 stays the main process, signals pass through
#    correctly, and Docker stop/restart works cleanly.
#
# Usage (via Dockerfile):
#   ENTRYPOINT ["docker-entrypoint.sh"]
#   CMD ["python", "forge_api.py"]
#
#   Override CMD to run a different service:
#   docker run --env-file aria.env aria-agent python voice_worker.py dev
# ══════════════════════════════════════════════════════════════════════════════

set -e

# ── Start virtual framebuffer ─────────────────────────────────────────────────
# :99 matches the DISPLAY=:99 env var set in the Dockerfile.
# -screen 0 1024x768x24 — 24-bit colour depth satisfies Qt's OpenGL checks.
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Give Xvfb a moment to initialise before FreeCAD tries to connect
sleep 0.5

# Verify it's actually running (fail fast if something is wrong)
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[entrypoint] ERROR: Xvfb failed to start. FreeCAD will not be able to run." >&2
    # Don't exit — the rest of the agent (API, LangGraph, Redis) still works.
    # Only CAD generation will fail gracefully with an error message.
fi

echo "[entrypoint] Xvfb :99 running (PID $XVFB_PID)"
echo "[entrypoint] Starting: $*"

# ── Hand off to main process (exec keeps it as PID 1) ────────────────────────
exec "$@"
