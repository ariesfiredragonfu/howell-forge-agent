# ══════════════════════════════════════════════════════════════════════════════
# Howell Forge — ARIA Agent Container
#
# Base: python:3.11-slim-bookworm (Debian 12 + Python 3.11)
#   • Python 3.11 pre-installed — no PPA gymnastics
#   • Better FreeCAD compatibility than ubuntu:22.04
#
# FreeCAD runs headlessly via a persistent Xvfb virtual display (:99).
# The entrypoint starts Xvfb once at boot; every freecadcmd subprocess
# inherits DISPLAY=:99 automatically.
#
# Runtime user: `forge` (non-root, UID 1001)
#   All writable paths are owned by forge before USER is switched.
#   This eliminates every "permission denied" complaint from FreeCAD,
#   ARIA's file writes, and mounted Docker volumes.
#
# Build:   docker build -t aria-agent .
# Run:     docker run --env-file aria.env -p 8765:8765 aria-agent
# Voice:   docker run --env-file aria.env aria-agent python voice_worker.py dev
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim-bookworm

# ── Labels ────────────────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="Howell Forge ARIA Agent"
LABEL org.opencontainers.image.description="LangGraph-powered CNC shop AI with FreeCAD headless"
LABEL org.opencontainers.image.source="https://github.com/ariesfiredragonfu/howell-forge-agent"

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # /app — project root
    # /usr/lib/freecad-python3/lib — FreeCAD Python bindings installed by apt
    #   (Debian's freecad package puts FreeCAD.so here; the python:3.11-slim
    #    Docker base uses /usr/local/lib/python3.11, so we bridge the gap here)
    PYTHONPATH=/app:/usr/lib/freecad-python3/lib \
    # Virtual display — entrypoint sets this up, subprocesses inherit it
    DISPLAY=:99 \
    # FreeCAD / Mesa — software renderer, no GPU required
    LIBGL_ALWAYS_SOFTWARE=1 \
    # FreeCAD writes its config to $HOME/.FreeCAD — point it at the forge user
    HOME=/home/forge \
    # CAD output directory (mount a named volume here in production)
    CAD_OUTPUT_DIR=/data/aria_forge

# ── Dedicated runtime user ────────────────────────────────────────────────────
# All system installs and pip runs happen as root above this line.
# The container switches to `forge` (UID 1001) before running any user code.
# This is standard practice and prevents FreeCAD / Python from ever needing root.
RUN groupadd --system --gid 1001 forge && \
    useradd  --system --uid 1001 --gid forge \
             --home /home/forge --create-home \
             forge

# ── System dependencies ───────────────────────────────────────────────────────
# Layer A: virtual display + Mesa software OpenGL (small, fast layer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libgl1-mesa-dri \
    libglapi-mesa \
    libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*

# Layer B: FreeCAD (large — own layer so it caches across code-only rebuilds)
RUN apt-get update && apt-get install -y --no-install-recommends \
    freecad \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Copied as root so pip can install globally — forge user inherits site-packages.
# Own layer: only rebuilds when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY *.py            ./
COPY machine_config.json ./
COPY eliza-config.json   ./

# ── Entrypoint script ─────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ── Writable directories — owned by forge ────────────────────────────────────
# Create every path ARIA or FreeCAD might write to, then hand them to forge.
# This is the single fix for all "permission denied" issues in Docker.
#
#   /app/exports          — G-code, STEP, STL output (shared volume)
#   /data/aria_forge      — FreeCAD output, mounted as a named volume
#   /data/orders          — SQLite / order state persistence
#   /home/forge/.FreeCAD  — FreeCAD config + recent-files cache
#   /tmp/.X11-unix        — Xvfb socket directory
#     Xvfb tries to create this at runtime. When running as non-root the mkdir
#     fails with "euid != 0". Pre-creating it with sticky+world-write (1777,
#     the same mode the real X server uses) lets forge start Xvfb cleanly.
#
RUN mkdir -p \
        /app/exports \
        /data/aria_forge \
        /data/orders \
        /home/forge/.FreeCAD \
        /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R forge:forge \
        /app \
        /data \
        /home/forge \
    && chmod -R u+rwX,go+rX \
        /app \
        /data \
        /home/forge

# ── Switch to non-root user ───────────────────────────────────────────────────
# Every instruction below here — and every process at runtime — runs as forge.
USER forge

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8765/context')" \
        || exit 1

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8765

# ── Entry point ───────────────────────────────────────────────────────────────
# Starts Xvfb :99, then exec's CMD (keeping forge as PID 1).
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "forge_api.py"]
