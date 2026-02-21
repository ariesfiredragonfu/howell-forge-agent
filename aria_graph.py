#!/usr/bin/env python3
"""
aria_graph.py — ARIA's Forge Nervous System (5 nodes, Gemini wiring)

Graph topology — the "Nervous System":

  [parse_intent] ──► [generate_cad] ──► [validate_safety]
                           ▲                    │
                           │    unsafe + loop   │ unsafe
                           └────────────────────┘
                                                │ safe (or abort)
                                                ▼
                                       [check_finances]
                                                │
                                                ▼
                                           [respond] ──► END

Key design decisions (from Gemini's wiring):

  1. `set_entry_point("parse_intent")` — explicit, no START import needed.
  2. Linear edges: parse_intent → generate_cad → validate_safety.
     ALL intents flow through this pipeline; non-design nodes are smart no-ops.
  3. The "Self-Healing" loop uses the exact Gemini conditional:
       lambda x: "generate_cad" if not x["is_geometry_valid"] else "check_finances"
     `validate_safety` owns the abort decision: when MAX_ITERATIONS is hit it
     sets is_geometry_valid=True so the lambda routes forward, not backward.
  4. No separate `revise` node — revision logic lives inside `generate_cad`.
     On a loop-back the node reads collision_report + iteration, applies one fix
     to current_dimensions, then regenerates the FreeCAD script.
  5. check_finances → respond → END is always linear.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import List, Literal, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from aria_voice_agent import SafetyAgent, MIN_WALL, _CHRIS_NAME, _SHOP_NAME
from shop_config import ShopConfig, shop_cfg as _default_shop_cfg

logger = logging.getLogger("aria.graph")

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_ITERATIONS  = 3
FREECAD_TIMEOUT = 45        # seconds
CAD_OUTPUT_DIR  = "/tmp/aria_forge"


# ═══════════════════════════════════════════════════════════════════════════════
# ForgeState
# ═══════════════════════════════════════════════════════════════════════════════

class ForgeState(TypedDict):
    """
    Single source of truth flowing through every node.

    The Human Element    — user_input, current_user
    The Digital Twin     — current_dimensions, active_cad_path, is_geometry_valid,
                           cad_script, cad_error
    The Physical Shop    — active_fixtures, collision_report
    The Business         — kaito_status, estimated_cost, ledger_ok, ledger_message
    The Conversation     — intent, next_node

    ShopConfig is never stored here — nodes call _get_shop() instead.
    """

    # The Human Element
    user_input:         str
    current_user:       str            # always "Chris"

    # The Digital Twin
    current_dimensions: dict           # x_mm/y_mm/z_mm, wall_mm, material,
                                       # move_x/y/z, spend_usd, fixture, description
    active_cad_path:    str            # path to STEP or STL ("" if none)
    is_geometry_valid:  bool
    cad_script:         str            # FreeCAD Python generated this turn
    cad_error:          Optional[str]  # None on success

    # The Physical Shop
    active_fixtures:    List[str]      # e.g. ["vise_01"]
    collision_report:   Optional[str]  # None if safe

    # The Business
    kaito_status:       str            # "PAID" | "PENDING" | "UNKNOWN"
    estimated_cost:     float
    ledger_ok:          bool
    ledger_message:     str

    # The Conversation
    intent:             str            # "design" | "camera" | "ledger" | "status"
    next_node:          str            # hint — read by conditional edges

    # Internal tracking
    forge_ctx:          dict
    revision_notes:     List[str]
    iteration:          int
    response:           str
    needs_confirm:      bool
    action_type:        str            # "none"|"forge_run"|"camera_move"|"abort"
    safety_category:    str


# ── ShopConfig accessor ───────────────────────────────────────────────────────

def _get_shop(override: Optional[ShopConfig] = None) -> ShopConfig:
    cfg = override or _default_shop_cfg
    cfg.reload()
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

# ── Dimension / intent parsing ────────────────────────────────────────────────

_DIM_MM   = re.compile(r'(\d+\.?\d*)\s*mm', re.IGNORECASE)
_DIM_INCH = re.compile(r'(\d+\.?\d*)\s*(?:inch|in\b|")', re.IGNORECASE)
_X_RE     = re.compile(r'[Xx]\s*(\d+\.?\d*)')
_Y_RE     = re.compile(r'[Yy]\s*(\d+\.?\d*)')
_Z_RE     = re.compile(r'[Zz]\s*(\d+\.?\d*)')
_WALL_RE  = re.compile(r'wall\D{0,6}(\d+\.?\d*)\s*mm|(\d+\.?\d*)\s*mm\D{0,6}wall', re.IGNORECASE)
_DOLLAR   = re.compile(r'\$(\d+\.?\d*)|(\d+\.?\d*)\s*(?:dollars?|usd|usdc)', re.IGNORECASE)
_MATERIAL = re.compile(r'\b(steel|aluminum|aluminium|brass|titanium|plastic|hdpe|delrin)\b', re.IGNORECASE)


def _parse_dimensions(text: str, shop: ShopConfig) -> dict:
    dims: dict = {"description": text}

    mm_vals = [float(m.group(1)) for m in _DIM_MM.finditer(text)]
    mm_vals += [float(m.group(1)) * 25.4 for m in _DIM_INCH.finditer(text)]
    if mm_vals:
        sv = sorted(set(mm_vals), reverse=True)
        if len(sv) >= 1: dims["x_mm"] = sv[0]
        if len(sv) >= 2: dims["y_mm"] = sv[1]
        if len(sv) >= 3: dims["z_mm"] = sv[2]

    wm = _WALL_RE.search(text)
    if wm:
        dims["wall_mm"] = float(wm.group(1) or wm.group(2))
    elif mm_vals and len(mm_vals) == 1 and re.search(r'\b(wall|slot|pocket|rib|fin|web)\b', text, re.I):
        dims["wall_mm"] = mm_vals[0]

    mat = _MATERIAL.search(text)
    dims["material"] = (
        "aluminum" if mat and mat.group(1).lower() in ("aluminum", "aluminium")
        else mat.group(1).lower() if mat else "unknown"
    )

    xm = _X_RE.search(text); ym = _Y_RE.search(text); zm = _Z_RE.search(text)
    if xm: dims["move_x"] = float(xm.group(1))
    if ym: dims["move_y"] = float(ym.group(1))
    if zm: dims["move_z"] = float(zm.group(1))

    dm = _DOLLAR.search(text)
    if dm: dims["spend_usd"] = float(dm.group(1) or dm.group(2))

    mentions = shop.scan_mentions(text)
    if mentions:
        dims["fixture"] = mentions[0][1].key

    return dims


# ── Cost estimator ────────────────────────────────────────────────────────────

_MAT_COST    = {"steel": 4.50, "aluminum": 6.00, "brass": 8.00, "titanium": 35.0, "default": 5.00}
_MAT_DENSITY = {"steel": 0.00785, "aluminum": 0.00270, "brass": 0.00850, "titanium": 0.00450, "default": 0.00500}
_MACHINE_RATE = 85.0   # USD/hr


def _estimate_cost(dims: dict) -> float:
    mat = dims.get("material", "default")
    x   = dims.get("x_mm", 50.0) / 10
    y   = dims.get("y_mm", 50.0) / 10
    z   = dims.get("z_mm", 10.0) / 10
    vol = x * y * z
    mat_cost  = vol * _MAT_DENSITY.get(mat, _MAT_DENSITY["default"]) * _MAT_COST.get(mat, _MAT_COST["default"])
    surface   = 2 * (x * y + y * z + x * z)
    mach_cost = ((15 + surface) / 60) * _MACHINE_RATE
    return round(mat_cost + mach_cost, 2)


# ── Revision engine ───────────────────────────────────────────────────────────

def _apply_revision(dims: dict, issue: str, shop: ShopConfig) -> tuple[dict, str]:
    """Apply one deterministic fix to current_dimensions. Returns (revised, note)."""
    revised = dict(dims)
    note    = ""

    if ("no-fly zone" in issue or "overlaps" in issue) and "X" in issue:
        zones = shop.get_active_no_fly_zones()
        if zones:
            safe_x = max(z["x_max"] for z in zones) + 1.0
            revised["move_x"] = safe_x
            note = f"Shifted X to {safe_x:.0f}mm — first clear point past {zones[0]['label']}."

    elif ("no-fly zone" in issue or "overlaps" in issue) and "Z" in issue:
        zones = shop.get_active_no_fly_zones()
        if zones:
            safe_z = max(z["z_max"] for z in zones) + 1.0
            revised["move_z"] = safe_z
            note = f"Shifted Z to {safe_z:.0f}mm — clears fixture no-fly zone."

    elif "X envelope" in issue or ("exceeds" in issue and "X" in issue):
        revised["move_x"] = shop.envelope.x - 1.0
        note = f"Clamped X to {shop.envelope.x - 1:.0f}mm — machine hard limit."

    elif "Z envelope" in issue or ("exceeds" in issue and "Z" in issue):
        revised["move_z"] = shop.envelope.z - 1.0
        note = f"Clamped Z to {shop.envelope.z - 1:.0f}mm — machine hard limit."

    elif "minimum machinable wall" in issue or "chatter" in issue:
        mat   = revised.get("material", "default")
        min_w = MIN_WALL.get(mat, MIN_WALL["default"])
        revised["wall_mm"] = min_w
        note = f"Wall increased to {min_w}mm — minimum machinable for {mat}."

    elif "won't fit" in issue or "jaw opening" in issue:
        note = "Part exceeds vise jaw opening. Soft jaws or a larger vise required."

    elif "wallet" in issue.lower() or "usdc" in issue.lower():
        revised["spend_usd"] = None
        note = "Purchase flagged — over wallet balance. Reducing order scope."

    else:
        note = f"Constraint: {issue[:80]}. Manual review needed."

    return revised, note


# ── FreeCAD script builder + runner ──────────────────────────────────────────

def _build_freecad_script(dims: dict, out_dir: str) -> str:
    x = max(dims.get("x_mm", 50.0), 0.1)
    y = max(dims.get("y_mm", 30.0), 0.1)
    z = max(dims.get("z_mm", 10.0), 0.1)

    desc      = dims.get("description", "").lower()
    has_holes = bool(re.search(r'\b(hole|holes|mounting|bolt|screw)\b', desc))
    hole_r    = min(x, y) * 0.08

    hole_block = ""
    if has_holes and hole_r >= 0.5:
        m = hole_r * 3
        corners = [(m, m), (x - m, m), (x - m, y - m), (m, y - m)]
        lines = [f"    cyl = Part.makeCylinder({hole_r:.2f}, {z:.2f})",
                 f"    cyl.translate(FreeCAD.Vector(cx, cy, 0))",
                  "    body = body.cut(cyl)"]
        corners_str = ", ".join(f"({cx:.2f},{cy:.2f})" for cx, cy in corners)
        hole_block = (
            f"for cx, cy in [{corners_str}]:\n" + "\n".join(lines)
        )

    return "\n".join([
        "import FreeCAD, Part, os",
        f'out_dir = "{out_dir}"',
        "os.makedirs(out_dir, exist_ok=True)",
        "doc = FreeCAD.newDocument('AriaPart')",
        f"body = Part.makeBox({x:.3f}, {y:.3f}, {z:.3f})",
        hole_block,
        "feat = doc.addObject('Part::Feature', 'Part')",
        "feat.Shape = body",
        "doc.recompute()",
        "step_path = os.path.join(out_dir, 'part.step')",
        "stl_path  = os.path.join(out_dir, 'part.stl')",
        "feat.Shape.exportStep(step_path)",
        "feat.Shape.exportStl(stl_path)",
        "print(f'STEP:{step_path}')",
        "print(f'STL:{stl_path}')",
        "FreeCAD.closeDocument('AriaPart')",
    ])


def _run_freecad(script: str) -> tuple[str, str, Optional[str]]:
    """
    Run a FreeCAD script headlessly.

    Display strategy (checked in order):
      1. DISPLAY already set (e.g. :99 from docker-entrypoint.sh)
         → call freecadcmd directly; it inherits the persistent Xvfb display.
      2. DISPLAY not set, xvfb-run available
         → wrap freecadcmd with xvfb-run -a (local dev without Xvfb running).
      3. freecadcmd not found
         → return error; cad_engine_node falls back to the last order's STL.

    The persistent-display path (case 1) is preferred because xvfb-run
    allocates a new server per call — under load that can race or exhaust
    display slots. The entrypoint starts Xvfb once and we reuse it.
    """
    import shutil
    os.makedirs(CAD_OUTPUT_DIR, exist_ok=True)

    if not shutil.which("freecadcmd"):
        return "", "", "freecadcmd not found — install FreeCAD or build the Docker image."

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        path = f.name

    # DISPLAY=:99 set by docker-entrypoint.sh → use freecadcmd directly
    if os.environ.get("DISPLAY"):
        cmd = ["freecadcmd", path]
    elif shutil.which("xvfb-run"):
        # Local dev: spin up a throwaway Xvfb just for this call
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
               "freecadcmd", path]
    else:
        cmd = ["freecadcmd", path]   # last resort — may error if no display

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=FREECAD_TIMEOUT,
        )
        step = stl = ""
        for line in proc.stdout.splitlines():
            if line.startswith("STEP:"): step = line[5:].strip()
            elif line.startswith("STL:"): stl  = line[4:].strip()
        if proc.returncode != 0 or not stl:
            err = (proc.stderr or "").strip()
            return "", "", err[:200] or "FreeCAD produced no output."
        return step, stl, None
    except subprocess.TimeoutExpired:
        return "", "", f"FreeCAD timed out after {FREECAD_TIMEOUT}s."
    finally:
        try: os.unlink(path)
        except OSError: pass


# ── STL bounding box parser ───────────────────────────────────────────────────

def _stl_bbox(stl_path: str) -> Optional[dict]:
    """Parse binary STL → AABB. Returns None on error."""
    try:
        with open(stl_path, "rb") as f:
            f.read(80)
            n = struct.unpack("<I", f.read(4))[0]
            if not (0 < n <= 5_000_000):
                return None
            xs, ys, zs = [], [], []
            for _ in range(n):
                f.read(12)
                for _ in range(3):
                    x, y, z = struct.unpack("<fff", f.read(12))
                    xs.append(x); ys.append(y); zs.append(z)
                f.read(2)
        return {"x_min": min(xs), "x_max": max(xs),
                "y_min": min(ys), "y_max": max(ys),
                "z_min": min(zs), "z_max": max(zs)}
    except Exception:
        return None


def _bbox_vs_nfz(bbox: dict, zones: list[dict]) -> Optional[str]:
    for zone in zones:
        if (bbox["x_min"] < zone["x_max"] and bbox["x_max"] > zone["x_min"] and
                bbox["z_min"] < zone["z_max"] and bbox["z_max"] > zone["z_min"]):
            return (
                f"Part geometry (X{bbox['x_min']:.0f}–{bbox['x_max']:.0f}mm, "
                f"Z{bbox['z_min']:.0f}–{bbox['z_max']:.0f}mm) overlaps "
                f"{zone['label']}'s no-fly zone "
                f"(X{zone['x_min']:.0f}–{zone['x_max']:.0f}mm). "
                f"Collision will occur."
            )
    return None


def _safety_transcript(dims: dict, iteration: int) -> str:
    base  = dims.get("description", "") if iteration == 0 else f"revised move iteration {iteration}"
    parts = [base]
    if dims.get("move_x") is not None: parts.append(f"move to X {dims['move_x']}mm")
    if dims.get("move_y") is not None: parts.append(f"Y {dims['move_y']}mm")
    if dims.get("move_z") is not None: parts.append(f"Z {dims['move_z']}mm")
    if dims.get("spend_usd"):          parts.append(f"${dims['spend_usd']}")
    if dims.get("wall_mm"):            parts.append(f"{dims['wall_mm']}mm wall")
    for k in ("x_mm", "y_mm", "z_mm"):
        v = dims.get(k)
        if v and not dims.get("wall_mm"): parts.append(f"{v}mm")
    return " ".join(str(p) for p in parts if p)


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 1 — parse_intent
# Classifies Chris's utterance. Runs once per conversation turn.
# ═══════════════════════════════════════════════════════════════════════════════

_DESIGN_KW = re.compile(
    r'\b(make|machine|cut|forge|build|fabricate|create|design|produce|mill|drill|bore|ream|'
    r'turn|face|engrave|chamfer|fillet|pocket|slot|tap|thread|contour|profile|'
    r'part|bracket|plate|block|shaft|bushing)\b',
    re.IGNORECASE,
)
_CAMERA_KW = re.compile(
    r'\b(show|view|look|zoom|switch|top\s*view|side\s*view|front\s*view|perspective|'
    r'camera|angle|rotate|isometric|overhead|close\s*up|collision\s*zoom)\b',
    re.IGNORECASE,
)
_LEDGER_KW = re.compile(
    r'\b(balance|budget|cost|afford|wallet|usdc|funds|money|how\s*much|can\s*we|'
    r'purchase|buy|order|spend|price|quote|kaito|invoice|pay)\b',
    re.IGNORECASE,
)


def parse_intent_node(state: ForgeState) -> dict:
    """
    NODE 1: parse_intent — classify the utterance.

    Priority: design > camera > ledger > status.
    All intents then go to generate_cad; that node decides whether to actually
    run FreeCAD or treat it as a pass-through.
    """
    text = state["user_input"]

    if _DESIGN_KW.search(text):
        intent = "design"
    elif _CAMERA_KW.search(text):
        intent = "camera"
    elif _LEDGER_KW.search(text):
        intent = "ledger"
    else:
        intent = "status"

    logger.info("[parse_intent] '%s…' → %s", text[:45], intent)
    return {"intent": intent}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 2 — generate_cad
# Builds/rebuilds the CAD geometry.
# On a loop-back from validate_safety it reads collision_report + iteration,
# applies one fix to current_dimensions, then regenerates.
# Non-design intents pass straight through.
# ═══════════════════════════════════════════════════════════════════════════════

def cad_engine_node(state: ForgeState) -> dict:
    """
    NODE 2: generate_cad — the Digital Twin builder.

    First pass  (iteration == 0, no collision_report):
      • Parse user_input → current_dimensions
      • Generate FreeCAD script → STEP + STL

    Loop-back pass (iteration > 0, collision_report is set):
      • Apply _apply_revision to current_dimensions
      • Regenerate with fixed dimensions
      • Append revision note

    Non-design intents (camera / ledger / status):
      • Pass through — no CAD work, just populate fixtures / kaito / cost.
    """
    shop   = _get_shop()
    ctx    = state["forge_ctx"]
    intent = state.get("intent", "status")
    iter_n = state.get("iteration", 0)

    # ── Non-design: fast pass-through ────────────────────────────────────────
    if intent != "design":
        fixtures = [f.id for f in shop.active if f.status == "active"]
        return {
            "active_fixtures": fixtures,
            "cad_script":      "",
            "cad_error":       None,
            "active_cad_path": state.get("active_cad_path", ""),
        }

    # ── Carry forward or parse dimensions ────────────────────────────────────
    dims         = dict(state.get("current_dimensions") or {})
    notes        = list(state.get("revision_notes") or [])
    issue        = state.get("collision_report") or ""

    if iter_n == 0 or not dims:
        # First pass: parse fresh from user input
        dims = _parse_dimensions(state["user_input"], shop)
    elif issue:
        # Loop-back: apply revision to existing dims
        dims, note = _apply_revision(dims, issue, shop)
        notes.append(note)
        logger.info("[generate_cad] rev %d: %s", iter_n, note[:70])

    # ── Run FreeCAD ───────────────────────────────────────────────────────────
    script                  = _build_freecad_script(dims, CAD_OUTPUT_DIR)
    step_path, stl_path, err = _run_freecad(script)

    # Fallback to last known order STL if FreeCAD isn't installed
    if err and not stl_path:
        for order in ctx.get("orders", {}).get("items", []):
            run = order.get("forge_run") or {}
            for key in ("stl_path", "step_path"):
                val = run.get(key)
                if val and Path(val).exists():
                    stl_path = str(val)
                    err = f"FreeCAD unavailable ({err[:50]}…). Using last order CAD."
                    break

    cad_path = stl_path or step_path or ""
    cost     = _estimate_cost(dims)
    fixtures = [f.id for f in shop.active if f.status == "active"]

    orders = ctx.get("orders", {}).get("items", [])
    kaito  = (orders[0].get("status", "UNKNOWN").upper() if orders
              else ("PAID" if ctx.get("orders", {}).get("paid_count", 0) > 0 else "PENDING"))

    logger.info(
        "[generate_cad] iter=%d  cad=%s  cost=$%.2f  err=%s",
        iter_n, cad_path or "(none)", cost, err or "OK",
    )

    return {
        "current_dimensions": dims,
        "revision_notes":     notes,
        "active_cad_path":    cad_path,
        "cad_script":         script,
        "cad_error":          err,
        "estimated_cost":     cost,
        "active_fixtures":    fixtures,
        "kaito_status":       kaito,
        # Reset safety fields so validate_safety runs fresh
        "collision_report":   None,
        "is_geometry_valid":  False,
        "safety_category":    "SAFE",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 3 — validate_safety
# Compares active_cad_path bounding boxes against shop_config.json.
# Owns the MAX_ITERATIONS abort decision.
# ═══════════════════════════════════════════════════════════════════════════════

def safety_inspector_node(state: ForgeState) -> dict:
    """
    NODE 3: validate_safety — the Physical Shop gatekeeper.

    Non-design intents: pass through (is_geometry_valid = True).

    Design intents — two-stage check:
      Stage A — Geometry: parse STL bounding box, AABB vs. no-fly zones.
      Stage B — Motion:   SafetyAgent (axis over-travel, physics, financial).
      Stage C — Fit:      Fixture jaw opening vs. part dimensions.

    Abort logic (MAX_ITERATIONS):
      When iteration >= MAX_ITERATIONS AND geometry is still invalid,
      force is_geometry_valid = True so the Gemini conditional lambda routes
      forward to check_finances → respond instead of looping back to generate_cad.
      Sets action_type = "abort" to signal respond node.
    """
    intent = state.get("intent", "status")

    # ── Non-design: pass through ──────────────────────────────────────────────
    if intent != "design":
        return {"is_geometry_valid": True, "collision_report": None, "safety_category": "SAFE"}

    shop   = _get_shop()
    ctx    = state["forge_ctx"]
    dims   = state.get("current_dimensions") or {}
    iter_n = state.get("iteration", 0)
    zones  = shop.get_active_no_fly_zones()

    collision_report: Optional[str] = None
    safety_category  = "SAFE"

    # Stage A: bounding box vs. no-fly zones (only when real STL exists)
    cad_path = state.get("active_cad_path", "")
    if cad_path and Path(cad_path).exists() and cad_path.endswith(".stl"):
        bbox = _stl_bbox(cad_path)
        if bbox:
            offset_x = dims.get("move_x", 0.0)
            offset_z = dims.get("move_z", 0.0)
            sbbox = {
                "x_min": bbox["x_min"] + offset_x,
                "x_max": bbox["x_max"] + offset_x,
                "y_min": bbox["y_min"],
                "y_max": bbox["y_max"],
                "z_min": bbox["z_min"] + offset_z,
                "z_max": bbox["z_max"] + offset_z,
            }
            hit = _bbox_vs_nfz(sbbox, zones)
            if hit:
                collision_report = hit
                safety_category  = "COLLISION"
            else:
                env = shop.envelope
                if sbbox["x_max"] > env.x:
                    collision_report = f"Part extends to X{sbbox['x_max']:.0f}mm — exceeds X envelope ({env.x:.0f}mm)."
                    safety_category  = "COLLISION"
                elif sbbox["z_max"] > env.z:
                    collision_report = f"Part extends to Z{sbbox['z_max']:.0f}mm — exceeds Z envelope ({env.z:.0f}mm)."
                    safety_category  = "COLLISION"

    # Stage B: SafetyAgent (motion, physics, financial)
    if not collision_report:
        agent  = SafetyAgent(ctx, shop_config=shop)
        result = agent.check(_safety_transcript(dims, iter_n))
        if result.get("interrupt"):
            collision_report = result["message"].replace("Interrupting, Chris. ", "")
            safety_category  = result.get("category", "COLLISION")

    # Stage C: fixture jaw fit
    if not collision_report:
        fx_key = dims.get("fixture")
        if fx_key:
            defn = shop.library.get(fx_key)
            if defn:
                for dk in ("x_mm", "y_mm"):
                    val = dims.get(dk)
                    if val and val > defn.width * 0.95:
                        collision_report = (
                            f"Part dimension {val:.0f}mm won't fit in the "
                            f"{defn.key.replace('_',' ')} — jaw opening is {defn.width:.0f}mm."
                        )
                        safety_category = "COLLISION"
                        break

    is_valid = collision_report is None

    # ── MAX_ITERATIONS abort: force forward, flag abort ───────────────────────
    # The Gemini lambda is:
    #   "generate_cad" if not is_geometry_valid else "check_finances"
    # When we've exhausted retries, set is_geometry_valid = True so the lambda
    # routes forward, and set action_type = "abort" for the respond node.
    action_type = state.get("action_type", "none")
    if not is_valid and iter_n >= MAX_ITERATIONS:
        is_valid    = True          # breaks the loop
        action_type = "abort"
        logger.info("[validate_safety] MAX_ITERATIONS hit — forcing forward with abort")

    logger.info(
        "[validate_safety] iter=%d  valid=%s  cat=%s  action=%s",
        iter_n, is_valid, safety_category, action_type,
    )

    return {
        "is_geometry_valid": is_valid,
        "collision_report":  collision_report if not is_valid or action_type == "abort" else None,
        "safety_category":   safety_category,
        "action_type":       action_type,
        # Increment iteration counter here so generate_cad sees the right value
        # on the next loop-back pass.
        "iteration":         iter_n + (0 if is_valid else 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 4 — check_finances
# Pings the Kaito engine (forge_ctx finances) to ensure shop health supports
# the proposed move.
# ═══════════════════════════════════════════════════════════════════════════════

_USDC_FLOOR = 10.0    # below this → hard block production
_USDC_WARN  = 50.0    # below this → warn but allow


def kaito_node(state: ForgeState) -> dict:
    """
    NODE 4: check_finances — the Business gate.

    For ledger intent: always ok, just reports balance.
    For design intent: blocks if cost > wallet or wallet < floor.
    Biofeedback health is appended as a non-blocking advisory.
    """
    ctx    = state["forge_ctx"]
    intent = state.get("intent", "design")
    cost   = state.get("estimated_cost", 0.0)

    fin    = ctx.get("finances",    {})
    bf     = ctx.get("biofeedback", {})
    usdc   = fin.get("usdc")
    matic  = fin.get("matic", 0.0)
    score  = bf.get("score",  0.0)
    health = bf.get("health", "UNKNOWN")

    ledger_ok  = True
    msg        = ""

    if intent == "ledger":
        if usdc is not None:
            msg = (
                f"Wallet: ${usdc:.2f} USDC | {matic:.4f} MATIC. "
                f"Biofeedback: {score:.2f} ({health}). "
                f"{'Healthy.' if usdc > _USDC_WARN else 'Running low — watch it.'}"
            )
        else:
            msg = "Wallet data unavailable — Polygon RPC may be offline."

    else:
        if usdc is None:
            msg = "Wallet offline. Confirm balance manually before cutting."
        elif usdc < _USDC_FLOOR:
            ledger_ok = False
            msg = (
                f"Shop wallet is at ${usdc:.2f} USDC — below the ${_USDC_FLOOR:.0f} "
                f"operating floor. Production paused."
            )
        elif cost > 0 and cost > usdc:
            ledger_ok = False
            msg = (
                f"Job estimated at ${cost:.2f} but wallet has ${usdc:.2f} USDC. "
                f"Short by ${cost - usdc:.2f}."
            )
        elif usdc < _USDC_WARN:
            msg = f"Wallet low (${usdc:.2f} USDC). Job at ${cost:.2f} — proceed with caution."
        else:
            msg = f"Wallet clear: ${usdc:.2f} USDC | job ~${cost:.2f}. Kaito: {state.get('kaito_status','?')}."

        if score < -1.0:
            msg += f" Biofeedback at {score:.2f} ({health}) — something's been off. Watch it."

    logger.info("[check_finances] intent=%s usdc=%s cost=$%.2f ok=%s", intent, usdc, cost, ledger_ok)

    return {"ledger_ok": ledger_ok, "ledger_message": msg}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 5 — respond
# ARIA's final spoken line — humble, down-to-earth, no fluff.
# ═══════════════════════════════════════════════════════════════════════════════

_TABLE_CENTER = [250.0, 0.0, 200.0]
_VISE_CENTER  = [76.2,  36.5, 50.0]
_CAMERA_VIEWS = {
    "top":            {"position": [250.0, 900.0, 201.0], "target": _TABLE_CENTER},
    "side":           {"position": [1000.0, 120.0, 200.0],"target": _TABLE_CENTER},
    "front":          {"position": [250.0, 120.0, 900.0], "target": _TABLE_CENTER},
    "perspective":    {"position": [600.0,  350.0, 600.0],"target": _TABLE_CENTER},
    "collision_zoom": {"position": [200.0,  130.0, 200.0],"target": _VISE_CENTER},
    "isometric":      {"position": [600.0,  350.0, 600.0],"target": _TABLE_CENTER},
    "overhead":       {"position": [250.0,  900.0, 201.0],"target": _TABLE_CENTER},
}
_VIEW_RE = re.compile(
    r'\b(top|side|front|perspective|isometric|overhead|collision\s*zoom)\b', re.IGNORECASE
)


def aria_voice_node(state: ForgeState) -> dict:
    """
    NODE 5: respond — ARIA speaks.

    Intent routing:
      design  → confirms job, reports revisions, asks for go/no-go
      camera  → announces view switch (voice_worker publishes CAMERA_MOVE to Redis)
      ledger  → reads out the wallet / Kaito status
      status  → brief shop health summary
    """
    intent     = state.get("intent",          "status")
    is_valid   = state.get("is_geometry_valid", False)
    action_type = state.get("action_type",     "none")
    iters      = state.get("iteration",        0)
    notes      = state.get("revision_notes",   [])
    issue      = state.get("collision_report", "") or ""
    category   = state.get("safety_category", "SAFE")
    cost       = state.get("estimated_cost",   0.0)
    kaito      = state.get("kaito_status",     "UNKNOWN")
    ledger_ok  = state.get("ledger_ok",        True)
    ledger_msg = state.get("ledger_message",   "")
    cad        = state.get("active_cad_path",  "")
    cad_err    = state.get("cad_error")
    dims       = state.get("current_dimensions", {})
    ctx        = state.get("forge_ctx",        {})
    text       = state.get("user_input",       "")

    lines:       list[str] = []
    needs_confirm          = False
    final_action           = action_type

    # ── Camera ────────────────────────────────────────────────────────────────
    if intent == "camera":
        m = _VIEW_RE.search(text)
        view_key = m.group(1).lower().replace(" ", "_") if m else None
        if view_key and view_key in _CAMERA_VIEWS:
            labels = {"top": "top-down", "collision_zoom": "collision zoom",
                      "isometric": "perspective", "overhead": "top-down"}
            label = labels.get(view_key, view_key)
            lines.append(f"Switching to {label} view.")
            final_action = "camera_move"
        else:
            lines.append("I don't have that angle. Try: top, side, front, perspective, or collision zoom.")

    # ── Ledger ────────────────────────────────────────────────────────────────
    elif intent == "ledger":
        lines.append(ledger_msg or "Can't reach the wallet right now.")

    # ── Status ────────────────────────────────────────────────────────────────
    elif intent == "status":
        fs  = ctx.get("forge_status", {}).get("status", "IDLE")
        bf  = ctx.get("biofeedback",  {})
        ords = ctx.get("orders",       {})
        fin  = ctx.get("finances",    {})
        usdc = fin.get("usdc")
        lines.append(f"Machine is {fs}.")
        lines.append(f"Biofeedback {bf.get('score','?')} ({bf.get('health','?')}).")
        lines.append(f"{ords.get('paid_count',0)} paid, {ords.get('in_production_count',0)} in production.")
        lines.append(f"Wallet: {'${:.2f} USDC'.format(usdc) if usdc is not None else 'offline'}.")
        if state.get("active_fixtures"):
            lines.append(f"Table: {', '.join(state['active_fixtures'])}.")

    # ── Design ────────────────────────────────────────────────────────────────
    else:
        # Abort (MAX_ITERATIONS exhausted)
        if action_type == "abort":
            lines.append(
                f"Hit the wall on this one, Chris — {iters} passes, still a "
                f"{category.lower()} problem."
            )
            if issue:
                lines.append(issue[:100])
            lines.append("Adjust the setup or geometry and try again.")
            final_action = "abort"

        # Ledger block
        elif not ledger_ok:
            lines.append(ledger_msg)
            final_action = "abort"

        # FreeCAD unavailable — dimension-only checks passed
        elif cad_err and not cad:
            lines.append(f"CAD engine offline: {cad_err[:60]}")
            lines.append("Dimension checks passed — safe to proceed if you trust the numbers.")
            final_action   = "forge_run"
            needs_confirm  = True

        # Happy path
        else:
            if iters == 0:
                lines.append("Checks out.")
            else:
                plural = "revisions" if iters > 1 else "revision"
                lines.append(f"{iters} {plural} to get there.")
                for note in notes:
                    lines.append(note)

            if dims.get("move_x") is not None:
                lines.append(f"Tool X at {dims['move_x']:.0f}mm.")
            if dims.get("wall_mm"):
                lines.append(f"Wall: {dims['wall_mm']:.1f}mm.")
            if dims.get("material") and dims["material"] != "unknown":
                lines.append(f"Material: {dims['material']}.")
            if cost > 0:
                lines.append(f"Estimated ${cost:.2f}.")
            if ledger_msg:
                lines.append(ledger_msg)
            if kaito == "PAID":
                lines.append("Kaito shows paid.")
            elif kaito == "PENDING":
                lines.append("Kaito still pending — confirm before cutting.")
            if cad:
                lines.append("Model is live on the dashboard.")
            if cad_err:
                lines.append(f"Note: {cad_err[:60]}")

            final_action  = "forge_run"
            needs_confirm = True
            lines.append("Say yes to kick it off.")

    response = " ".join(l for l in lines if l.strip())
    logger.info("[respond] intent=%s  action=%s  → '%s'", intent, final_action, response[:80])

    return {
        "response":      response,
        "needs_confirm": needs_confirm,
        "action_type":   final_action,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Graph wiring — exact Gemini topology
# ═══════════════════════════════════════════════════════════════════════════════

def build_aria_graph() -> StateGraph:
    """
    Compile the Forge Nervous System graph.

    Matches Gemini's wiring exactly:
      set_entry_point("parse_intent")
      linear edges for the main pipeline
      one conditional "self-healing" loop from validate_safety → generate_cad
    """
    workflow = StateGraph(ForgeState)

    # ── Specialized shop nodes ────────────────────────────────────────────────
    workflow.add_node("parse_intent",     parse_intent_node)
    workflow.add_node("generate_cad",     cad_engine_node)
    workflow.add_node("validate_safety",  safety_inspector_node)
    workflow.add_node("check_finances",   kaito_node)
    workflow.add_node("respond",          aria_voice_node)

    # ── The Nervous System (edges) ─────────────────────────────────────────────
    workflow.set_entry_point("parse_intent")
    workflow.add_edge("parse_intent", "generate_cad")
    workflow.add_edge("generate_cad", "validate_safety")

    # The "Self-Healing" loop — Gemini's exact lambda:
    #   If unsafe → generate_cad  (revision + regen)
    #   If safe   → check_finances
    # validate_safety handles MAX_ITERATIONS by forcing is_geometry_valid=True
    # so this lambda naturally routes forward on abort without modification.
    workflow.add_conditional_edges(
        "validate_safety",
        lambda x: "generate_cad" if not x["is_geometry_valid"] else "check_finances",
    )

    workflow.add_edge("check_finances", "respond")
    workflow.add_edge("respond", END)

    return workflow.compile(checkpointer=MemorySaver())


aria_graph = build_aria_graph()


# ═══════════════════════════════════════════════════════════════════════════════
# Public async runner
# ═══════════════════════════════════════════════════════════════════════════════

async def run_aria_graph(
    user_message: str,
    forge_ctx: dict,
    shop: Optional[ShopConfig] = None,
    session_id: str = "default",
) -> ForgeState:
    """Run the 5-node Forge Nervous System for one user utterance."""
    active_shop = shop or _default_shop_cfg
    active_shop.reload()

    initial: ForgeState = {
        "user_input":         user_message,
        "current_user":       _CHRIS_NAME,
        "current_dimensions": {},
        "active_cad_path":    "",
        "is_geometry_valid":  False,
        "cad_script":         "",
        "cad_error":          None,
        "active_fixtures":    [],
        "collision_report":   None,
        "kaito_status":       "UNKNOWN",
        "estimated_cost":     0.0,
        "ledger_ok":          True,
        "ledger_message":     "",
        "intent":             "status",
        "next_node":          "parse_intent",
        "forge_ctx":          forge_ctx,
        "revision_notes":     [],
        "iteration":          0,
        "response":           "",
        "needs_confirm":      False,
        "action_type":        "none",
        "safety_category":    "SAFE",
    }

    config = {"configurable": {"thread_id": session_id}}
    return await aria_graph.ainvoke(initial, config=config)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI smoke test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    mock_ctx = {
        "finances":     {"usdc": 450.00, "matic": 2.1},
        "biofeedback":  {"score": 1.2, "health": "STABLE"},
        "forge_status": {"status": "IDLE"},
        "orders": {
            "total": 2, "paid_count": 1, "in_production_count": 0,
            "items": [{"order_id": "order_abc", "status": "PAID",
                       "forge_run": {"stl_path": "/tmp/order_abc/part.stl"}}],
        },
    }
    mock_broke = dict(mock_ctx, finances={"usdc": 8.00, "matic": 0.1})

    SCENARIOS = [
        # name                 message                                      ctx         exp_intent  exp_valid  exp_iters  exp_action
        ("design-safe",     "Make a 50×30×10mm aluminum bracket",          mock_ctx,   "design",   True,  0, "forge_run"),
        ("design-collision","Machine a bracket, move to X 50",              mock_ctx,   "design",   True,  1, "forge_run"),
        ("design-wall",     "Cut a 0.4mm wall slot in steel",               mock_ctx,   "design",   True,  1, "forge_run"),
        ("design-abort",    "Hold a 110mm part in the vise at X 30",        mock_ctx,   "design",   True,  3, "abort"),
        ("camera-top",      "Show me the top view",                         mock_ctx,   "camera",   True,  0, "camera_move"),
        ("camera-front",    "Switch to front view",                         mock_ctx,   "camera",   True,  0, "camera_move"),
        ("ledger-pass",     "What's our balance?",                          mock_ctx,   "ledger",   True,  0, "none"),
        ("ledger-low",      "Can we afford this?",                          mock_broke, "ledger",   True,  0, "none"),
        ("status",          "How's the shop doing?",                        mock_ctx,   "status",   True,  0, "none"),
    ]

    async def run_all():
        print(f"\nARIA Forge Nervous System — {len(SCENARIOS)} scenarios\n{'═'*68}")
        passed = 0
        for name, msg, ctx, exp_intent, exp_valid, exp_iters, exp_action in SCENARIOS:
            r = await run_aria_graph(msg, ctx, session_id=f"test-{name}")

            ok = (r["intent"]           == exp_intent and
                  r["is_geometry_valid"] == exp_valid  and
                  r["iteration"]         == exp_iters  and
                  r["action_type"]       == exp_action)
            if ok: passed += 1

            fail = []
            if r["intent"]           != exp_intent: fail.append(f"intent={r['intent']} want={exp_intent}")
            if r["is_geometry_valid"] != exp_valid:  fail.append(f"valid={r['is_geometry_valid']} want={exp_valid}")
            if r["iteration"]         != exp_iters:  fail.append(f"iters={r['iteration']} want={exp_iters}")
            if r["action_type"]       != exp_action: fail.append(f"action={r['action_type']} want={exp_action}")

            print(f"\n{'✓' if ok else '✗'} [{name}]  "
                  f"intent={r['intent']}  valid={r['is_geometry_valid']}  "
                  f"iters={r['iteration']}  action={r['action_type']}")
            print(f"  Fixtures:{r['active_fixtures']}  Kaito:{r['kaito_status']}  "
                  f"Cost:${r['estimated_cost']:.2f}  LedgerOK:{r['ledger_ok']}")
            if r.get("cad_error"):    print(f"  CAD err: {r['cad_error'][:70]}")
            if r.get("collision_report") and r["action_type"] == "abort":
                print(f"  Collision: {r['collision_report'][:70]}")
            for i, n in enumerate(r.get("revision_notes") or [], 1):
                print(f"  REV {i}:  {n[:70]}")
            if r.get("ledger_message"): print(f"  Ledger:  {r['ledger_message'][:70]}")
            print(f"  ARIA:    {r['response'][:100]}")
            if fail: print(f"  FAIL:    {' | '.join(fail)}")

        print(f"\n{'═'*68}")
        print(f"Result: {passed}/{len(SCENARIOS)} passed")

    asyncio.run(run_all())
