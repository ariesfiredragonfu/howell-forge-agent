#!/usr/bin/env python3
"""
Forge Manager V1 — Linear happy-path: PAID order → In Production.

Flow:
  1. Verify order is PAID (reads from Eliza DB)
  2. Generate FreeCAD Python script via Claude Code CLI (turns description → CAD code)
     Falls back to smart local parametric generator if Claude CLI is unavailable.
  3. Run FreeCAD headlessly → exports STEP + STL
  4. Generate stub G-code (bounding-box contour) → validate with gcode_validator
  5. PAUSE for human visual check (open STL in FreeCAD, review G-code)
  6. On approval  → biofeedback reward, order → "In Production", Telegram + CS notify
     On rejection → biofeedback constraint, order stays, Telegram alert

Script generation uses Claude Code CLI (`claude -p`):
  - Zero API cost — uses your existing Cursor/Claude Code subscription
  - One-time setup: run `claude login` from the terminal to authenticate
  - Falls back to local parametric generator automatically if not logged in

No retries, no error branches — V1 happy path only.
Safety branches come in V2.

Usage:
    python3 forge_manager_v1.py <order_id> "<part description>"
    # or import and call directly:
    from forge_manager_v1 import forge_manager_v1
    result = forge_manager_v1("order_123", "5 inch steel bracket with 4 mounting holes")
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
from notifications import send_telegram_alert

# ─── Config ───────────────────────────────────────────────────────────────────

CLAUDE_CLI        = Path.home() / ".local" / "bin" / "claude"   # installed by Claude Code
OUTPUT_BASE       = Path.home() / "Hardware_Factory" / "forge_orders"
FREECADCMD        = "freecadcmd"
AGENT_NAME        = "FORGE_MANAGER"
GCODE_VALIDATOR   = Path(__file__).parent / "gcode_validator.py"

# Machine limits (mm) — must match gcode_validator
X_MAX_MM, Y_MAX_MM, Z_MAX_MM = 300.0, 300.0, 100.0


# ─── Claude Code CLI helper ───────────────────────────────────────────────────

def _call_claude_cli(prompt: str, timeout: int = 90) -> str:
    """
    Invoke Claude Code CLI in non-interactive print mode.

    Uses the locally installed `claude` binary (Claude Code v2+).
    Requires a one-time `claude login` from the terminal.

    Raises RuntimeError on failure (not logged in, timeout, etc.)
    so the caller can fall back to the local generator.
    """
    cli = str(CLAUDE_CLI) if CLAUDE_CLI.exists() else "claude"
    result = subprocess.run(
        [cli, "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"claude CLI exited {result.returncode}: {stderr[:300]}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude CLI returned empty output")
    return output


# ─── Step 1: Verify PAID ──────────────────────────────────────────────────────

def _verify_paid_order(order_id: str) -> dict:
    """
    Look up order in Eliza DB and confirm status is PAID or Success.
    Raises ValueError if not found or not PAID.
    """
    order = eliza_memory.get_order(order_id)
    if order is None:
        raise ValueError(f"Order {order_id!r} not found in DB")
    status = order.get("status", "")
    if status not in ("PAID", "Success"):
        raise ValueError(
            f"Order {order_id!r} is {status!r} — must be PAID before entering production"
        )
    print(f"[FORGE] ✓ Order {order_id} verified PAID")
    return order


# ─── Step 2: Generate FreeCAD script via Claude Code CLI ─────────────────────

_CLAUDE_FREECAD_PROMPT = """\
You are a FreeCAD 0.21.2 expert writing safe, complete, headless Python scripts \
for a CNC machine shop.

STRICT RULES — follow every one:
1. Import only: FreeCAD, Part, MeshPart  (NO FreeCADGui, NO App.Gui, NO Mesh)
2. All dimensions in mm (1 inch = 25.4 mm)
3. OUTPUT_STEP and OUTPUT_STL are already defined as Python str variables — use them as-is
4. Export STEP:  Part.export([doc.getObject("Part")], OUTPUT_STEP)
5. Export STL:   import MeshPart
                 _mesh = MeshPart.meshFromShape(Shape=_shape,
                     LinearDeflection=0.1, AngularDeflection=0.08, Relative=False)
                 _mesh.write(OUTPUT_STL)
6. Final line:   print("BBOX:", _shape.BoundBox.XLength, _shape.BoundBox.YLength, _shape.BoundBox.ZLength)
7. No try/except, no comments longer than 10 words, no placeholder TODO lines
8. Return ONLY the Python code — no markdown fences, no explanation text

Part to model:
{description}
"""


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that Claude sometimes wraps around code."""
    lines = text.splitlines()
    out   = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _generate_freecad_script(description: str, output_dir: Path) -> str:
    """
    Generate a FreeCAD 0.21.2 headless Python script for the described part.

    Primary:  Claude Code CLI (`claude -p`) — zero cost, uses Cursor subscription.
              Requires one-time: `claude login` from the terminal.
    Fallback: Smart local parametric generator (always works, no auth needed).

    Returns Python source code as a string (ready for freecadcmd).
    """
    print(f"[FORGE] Generating FreeCAD script for: {description!r}")
    prompt = _CLAUDE_FREECAD_PROMPT.format(description=description)

    # ── Primary: Claude Code CLI ───────────────────────────────────────────────
    try:
        raw  = _call_claude_cli(prompt)
        code = _strip_fences(raw)
        if len(code.strip()) < 50:
            raise RuntimeError("Claude returned suspiciously short output")
        # Save raw Claude output for debugging
        (output_dir / "claude_raw_output.txt").write_text(raw)
        print("[FORGE] ✓ FreeCAD script generated via Claude Code CLI")
        return code
    except Exception as exc:
        print(f"[FORGE] Claude CLI unavailable ({exc})")
        print("[FORGE] → falling back to local parametric generator")
        print("[FORGE]   (run `claude login` once to enable AI-generated scripts)")
        return _fallback_freecad_script(description)


def _fallback_freecad_script(description: str) -> str:
    """
    Smart local parametric generator — no API, no auth, always works.

    Parses description for:
      - Dimensions: first 3 numbers → L × W × T (inches unless "mm" present)
      - Shape type: round keywords → cylinder/tube; otherwise → rectangular plate
      - Hole count: "N hole(s)" or "N mounting holes"
      - Hole diameter: "N inch hole" pattern

    Delegates to _rect_part_script() or _round_part_script().
    """
    import re
    desc_lower = description.lower()

    nums   = [float(n) for n in re.findall(r"\d+\.?\d*", description)]
    use_mm = "mm" in desc_lower and "inch" not in desc_lower
    scale  = 1.0 if use_mm else 25.4

    l_mm = (nums[0] if len(nums) > 0 else 5.0) * scale
    w_mm = (nums[1] if len(nums) > 1 else 3.0) * scale
    t_mm = (nums[2] if len(nums) > 2 else 0.5) * scale

    hole_match = re.search(r"(\d+)\s+(?:mounting\s+)?holes?", desc_lower)
    n_holes    = int(hole_match.group(1)) if hole_match else 0

    hdm = re.search(r"(\d+\.?\d*)\s*(?:inch|in)\s+(?:dia|diameter|hole)", desc_lower)
    hole_r_mm = (float(hdm.group(1)) * 25.4 / 2) if hdm else 3.175  # default ¼ in

    round_kw = ("cylinder", "cylindrical", "round", "rod", "shaft",
                "disk", "disc", "ring", "tube", "pipe", "bore", "circular")
    if any(k in desc_lower for k in round_kw):
        inner_mm = (w_mm / 2) if any(k in desc_lower for k in ("tube", "pipe", "ring", "bore")) else 0.0
        return _round_part_script(l_mm / 2, inner_mm, t_mm)

    return _rect_part_script(l_mm, w_mm, t_mm, n_holes, hole_r_mm)


def _rect_part_script(l: float, w: float, t: float, n_holes: int, hole_r: float) -> str:
    """FreeCAD script: rectangular plate/bracket with optional holes."""
    hd     = t + 2.0                             # hole depth (pierces through)
    margin = max(6.35, min(12.7, l * 0.1, w * 0.1))

    lines: list[str] = [
        "import FreeCAD, Part, MeshPart",
        'doc   = FreeCAD.newDocument("ForgePart")',
        f"stock = Part.makeBox({l:.3f}, {w:.3f}, {t:.3f})",
    ]

    if n_holes == 4:
        lines += [
            "holes = []",
            f"for xo in [{margin:.3f}, {l:.3f} - {margin:.3f}]:",
            f"    for yo in [{margin:.3f}, {w:.3f} - {margin:.3f}]:",
            f"        holes.append(Part.makeCylinder({hole_r:.3f}, {hd:.3f}, FreeCAD.Vector(xo, yo, -1)))",
            "_shape = stock",
            "for h in holes:",
            "    _shape = _shape.cut(h)",
        ]
    elif n_holes == 2:
        lines += [
            "holes = []",
            f"for xo in [{l/2:.3f} - {margin:.3f}, {l/2:.3f} + {margin:.3f}]:",
            f"    holes.append(Part.makeCylinder({hole_r:.3f}, {hd:.3f}, FreeCAD.Vector(xo, {w/2:.3f}, -1)))",
            "_shape = stock",
            "for h in holes:",
            "    _shape = _shape.cut(h)",
        ]
    elif n_holes > 0:
        lines += [
            "holes = []",
            f"for i in range({n_holes}):",
            f"    xo = {margin:.3f} + i * ({l:.3f} - 2 * {margin:.3f}) / max({n_holes} - 1, 1)",
            f"    holes.append(Part.makeCylinder({hole_r:.3f}, {hd:.3f}, FreeCAD.Vector(xo, {w/2:.3f}, -1)))",
            "_shape = stock",
            "for h in holes:",
            "    _shape = _shape.cut(h)",
        ]
    else:
        lines.append("_shape = stock")

    lines += [
        'doc.addObject("Part::Feature", "Part").Shape = _shape',
        "doc.recompute()",
        'Part.export([doc.getObject("Part")], OUTPUT_STEP)',
        "_mesh = MeshPart.meshFromShape(Shape=_shape, LinearDeflection=0.1, AngularDeflection=0.08, Relative=False)",
        "_mesh.write(OUTPUT_STL)",
        'print("BBOX:", _shape.BoundBox.XLength, _shape.BoundBox.YLength, _shape.BoundBox.ZLength)',
    ]
    return "\n".join(lines) + "\n"


def _round_part_script(outer_r: float, inner_r: float, height: float) -> str:
    """FreeCAD script: solid cylinder, disk, or hollow tube/ring."""
    lines: list[str] = [
        "import FreeCAD, Part, MeshPart",
        'doc = FreeCAD.newDocument("ForgePart")',
        f"_outer = Part.makeCylinder({outer_r:.3f}, {height:.3f})",
    ]
    if inner_r > 0 and inner_r < outer_r:
        lines += [
            f"_inner = Part.makeCylinder({inner_r:.3f}, {height + 2:.3f}, FreeCAD.Vector(0, 0, -1))",
            "_shape = _outer.cut(_inner)",
        ]
    else:
        lines.append("_shape = _outer")

    lines += [
        'doc.addObject("Part::Feature", "Part").Shape = _shape',
        "doc.recompute()",
        'Part.export([doc.getObject("Part")], OUTPUT_STEP)',
        "_mesh = MeshPart.meshFromShape(Shape=_shape, LinearDeflection=0.1, AngularDeflection=0.08, Relative=False)",
        "_mesh.write(OUTPUT_STL)",
        'print("BBOX:", _shape.BoundBox.XLength, _shape.BoundBox.YLength, _shape.BoundBox.ZLength)',
    ]
    return "\n".join(lines) + "\n"


# ─── Step 3: Run FreeCAD headlessly ──────────────────────────────────────────

_RUNNER_TEMPLATE = """\
# Auto-generated runner — do not edit manually
import sys, os
sys.path.insert(0, os.path.expanduser("~/.local/lib/python3/dist-packages"))

OUTPUT_STEP = {step!r}
OUTPUT_STL  = {stl!r}

# ── Generated part script ────────────────────────────────────────────────────
{part_code}
"""


def _run_freecad_headless(order_id: str, part_code: str, output_dir: Path) -> dict:
    """
    Write a runner script, execute with freecadcmd, return paths to outputs.
    Returns: {"step": Path, "stl": Path, "bbox_mm": [x, y, z] or None}
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    step_path   = output_dir / "part.step"
    stl_path    = output_dir / "part.stl"
    script_path = output_dir / "part_script.py"

    runner = _RUNNER_TEMPLATE.format(
        step=str(step_path),
        stl=str(stl_path),
        part_code=part_code,
    )
    script_path.write_text(runner)
    (output_dir / "part_code_raw.py").write_text(part_code)

    print(f"[FORGE] Running FreeCAD headless: {script_path}")
    result = subprocess.run(
        [FREECADCMD, str(script_path)],
        capture_output=True, text=True, timeout=120,
    )
    stdout = result.stdout + result.stderr

    # Parse bounding box from output
    bbox = None
    for line in stdout.splitlines():
        if line.startswith("BBOX:"):
            try:
                parts = line.split()
                bbox  = [float(parts[1]), float(parts[2]), float(parts[3])]
            except (IndexError, ValueError):
                pass

    if result.returncode != 0 or not stl_path.exists():
        print(f"[FORGE] FreeCAD stdout:\n{stdout[-2000:]}")
        raise RuntimeError(
            f"FreeCAD headless failed (rc={result.returncode}). "
            f"Check {output_dir}/part_script.py"
        )

    print(f"[FORGE] ✓ FreeCAD export complete")
    print(f"        STEP → {step_path}")
    print(f"        STL  → {stl_path}")
    if bbox:
        print(f"        BBox → {bbox[0]:.1f} × {bbox[1]:.1f} × {bbox[2]:.1f} mm")

    return {"step": step_path, "stl": stl_path, "bbox_mm": bbox}


# ─── Step 4a: Generate stub G-code ───────────────────────────────────────────

def _generate_stub_gcode(
    order_id: str, description: str, freecad_paths: dict, output_dir: Path
) -> Path:
    """
    Generate a simple rectangular perimeter contour G-code from bounding box.
    V1 stub — full FreeCAD Path CAM toolpaths come in V2.

    The generated file is review-safe: feed rate is conservative, Z safe height
    is 5 mm, cut depth is only 1 mm (surface pass).
    """
    bbox = freecad_paths.get("bbox_mm") or [127.0, 76.2, 12.7]  # 5×3×0.5 in default
    x_len, y_len = bbox[0], bbox[1]

    # Clamp to machine limits
    x_len = min(x_len, X_MAX_MM - 10)
    y_len = min(y_len, Y_MAX_MM - 10)

    gcode_path = output_dir / "part.gcode"
    gcode_path.write_text(textwrap.dedent(f"""\
        ; Forge Manager V1 — Stub G-code for: {description}
        ; Order: {order_id}
        ; Generated: {datetime.now(timezone.utc).isoformat()}
        ; BBox (mm): {bbox[0]:.2f} x {bbox[1]:.2f} x {bbox[2]:.2f}
        ; WARNING: V1 STUB — review toolpath before machining
        ; Full CAM from FreeCAD Path GUI required for production run.
        ;
        G21           ; mm units
        G90           ; absolute positioning
        G17           ; XY plane
        G0 Z5.000     ; safe height
        G0 X0.000 Y0.000
        M3 S1000      ; spindle on (adjust RPM)
        G1 Z0.500 F200    ; controlled descent to clearance
        G1 Z-1.000 F100   ; plunge 1 mm (surface pass only)
        ; ── Perimeter contour ──────────────────────────────────
        G1 X{x_len:.3f} Y0.000      F300
        G1 X{x_len:.3f} Y{y_len:.3f} F300
        G1 X0.000      Y{y_len:.3f} F300
        G1 X0.000      Y0.000       F300
        ; ── Retract ───────────────────────────────────────────
        G0 Z5.000
        M5            ; spindle off
        G0 X0.000 Y0.000
        M30           ; end of program
    """))

    print(f"[FORGE] ✓ Stub G-code written → {gcode_path}")
    return gcode_path


# ─── Step 4b: Validate G-code ────────────────────────────────────────────────

def _validate_gcode(gcode_path: Path) -> dict:
    """Run gcode_validator.py and return {'ok': bool, 'issues': [str]}."""
    result = subprocess.run(
        [sys.executable, str(GCODE_VALIDATOR), str(gcode_path)],
        capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    ok     = result.returncode == 0
    issues = [l for l in output.splitlines() if l.strip() and not l.startswith("#")]
    print(f"[FORGE] G-code validation: {'✓ PASS' if ok else '✗ ISSUES'}")
    for issue in issues:
        print(f"        {issue}")
    return {"ok": ok, "issues": issues, "raw": output}


# ─── Step 4b: G-code toolpath visualiser ────────────────────────────────────

def _render_toolpath(gcode_path: Path, output_dir: Path, bbox_mm: list) -> Path | None:
    """
    Parse G-code and render a 2D toolpath plot as a PNG.

    Color coding:
      Blue solid   — G1 cutting moves (XY)
      Red dashed   — G0 rapid moves (XY)
      Orange dot   — plunge / retract (Z change while XY fixed)
      Green circle — start point
      Red X        — end point

    Returns the PNG path, or None if matplotlib unavailable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyArrowPatch
    except ImportError:
        print("[FORGE] matplotlib not available — skipping toolpath render")
        return None

    # ── Parse G-code moves ────────────────────────────────────────────────────
    x, y, z   = 0.0, 0.0, 0.0
    mode      = "G0"    # current motion mode
    rapids:  list[tuple] = []   # (x0,y0,x1,y1)
    cuts:    list[tuple] = []
    plunges: list[tuple] = []   # (x, y) where Z changes

    for raw_line in gcode_path.read_text().splitlines():
        line = raw_line.split(";")[0].strip().upper()
        if not line:
            continue

        if line.startswith("G0 ") or line == "G0":
            mode = "G0"
        elif line.startswith("G1 ") or line == "G1":
            mode = "G1"

        nx, ny, nz = x, y, z
        for token in line.split():
            try:
                if token.startswith("X"):
                    nx = float(token[1:])
                elif token.startswith("Y"):
                    ny = float(token[1:])
                elif token.startswith("Z"):
                    nz = float(token[1:])
            except ValueError:
                pass

        moved_xy = (nx != x or ny != y)
        moved_z  = (nz != z)

        if moved_z and not moved_xy:
            plunges.append((x, y))

        if moved_xy:
            seg = (x, y, nx, ny)
            if mode == "G0":
                rapids.append(seg)
            else:
                cuts.append(seg)

        x, y, z = nx, ny, nz

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#12121f")

    bx, by = (bbox_mm[0], bbox_mm[1]) if bbox_mm else (0, 0)

    # Part outline (stock boundary)
    if bx > 0 and by > 0:
        rect = plt.Rectangle((0, 0), bx, by,
                              linewidth=1.5, edgecolor="#555577",
                              facecolor="#2a2a3e", zorder=1, label="Stock boundary")
        ax.add_patch(rect)

    def draw_arrow_segments(segs, color, lw, ls, zorder, label):
        for i, (x0, y0, x1, y1) in enumerate(segs):
            ax.annotate(
                "", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(
                    arrowstyle="->" if ls == "solid" else "->",
                    color=color, lw=lw,
                    linestyle=ls,
                    connectionstyle="arc3,rad=0",
                ),
                zorder=zorder,
            )
        if segs:
            ax.plot([], [], color=color, lw=lw, ls="--" if ls == "dashed" else "-",
                    label=label)

    # Rapid moves
    for x0, y0, x1, y1 in rapids:
        ax.plot([x0, x1], [y0, y1], color="#ff4444", lw=1.2,
                ls="--", zorder=2, alpha=0.7)
    if rapids:
        ax.plot([], [], color="#ff4444", lw=1.2, ls="--", label="Rapid (G0)")

    # Cutting moves with arrows every segment
    for i, (x0, y0, x1, y1) in enumerate(cuts):
        ax.annotate(
            "", xy=(x1, y1), xytext=(x0, y0),
            arrowprops=dict(arrowstyle="->", color="#44aaff",
                            lw=2.0, connectionstyle="arc3,rad=0"),
            zorder=3,
        )
    if cuts:
        ax.plot([], [], color="#44aaff", lw=2.0, label="Cut (G1)")

    # Plunge points
    for px, py in plunges:
        ax.plot(px, py, "o", color="#ffaa00", ms=8, zorder=4)
    if plunges:
        ax.plot([], [], "o", color="#ffaa00", ms=8, label="Plunge / retract")

    # Start and end markers
    if cuts or rapids:
        all_moves = rapids + cuts
        sx, sy = all_moves[0][0], all_moves[0][1]
        ex, ey = all_moves[-1][2], all_moves[-1][3]
        ax.plot(sx, sy, "o", color="#00ff88", ms=12, zorder=5, label="Start")
        ax.plot(ex, ey, "X", color="#ff4444", ms=12, zorder=5,
                markeredgewidth=2, label="End")

    # Axes and labels
    pad = max(bx, by) * 0.12 if (bx or by) else 10
    ax.set_xlim(-pad, bx + pad)
    ax.set_ylim(-pad, by + pad)
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)", color="#aaaacc")
    ax.set_ylabel("Y (mm)", color="#aaaacc")
    ax.tick_params(colors="#888899")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444466")
    ax.grid(True, color="#333355", lw=0.5)

    title = f"Toolpath — {gcode_path.parent.name}"
    if bx and by:
        title += f"\nStock: {bx:.1f} × {by:.1f} mm"
    ax.set_title(title, color="#ccccee", fontsize=12, pad=10)

    legend = ax.legend(facecolor="#1a1a2e", edgecolor="#555577",
                       labelcolor="#ccccee", fontsize=9)

    out_path = output_dir / "preview_toolpath.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[FORGE] ✓ Toolpath render → {out_path}")
    return out_path


# ─── Step 4c: Blender headless preview render ────────────────────────────────

_BLENDER_RENDER_SCRIPT = """\
import bpy, math, sys

stl_path = {stl!r}
out_dir  = {out!r}

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

bpy.ops.import_mesh.stl(filepath=stl_path)
obj = bpy.context.selected_objects[0]
bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
obj.location = (0, 0, 0)

dims  = obj.dimensions
scale = 2.0 / max(dims) if max(dims) > 0 else 1.0
obj.scale = (scale, scale, scale)
bpy.ops.object.transform_apply(scale=True)

mat = bpy.data.materials.new("Steel")
mat.diffuse_color = (0.55, 0.57, 0.62, 1.0)
obj.data.materials.append(mat)

bpy.ops.object.light_add(type='SUN', location=(3, 3, 5))
bpy.context.object.data.energy = 3.0

scene = bpy.context.scene
scene.render.resolution_x = 800
scene.render.resolution_y = 600
scene.render.image_settings.file_format = 'PNG'
scene.render.engine = 'BLENDER_EEVEE'

cam_data = bpy.data.cameras.new("Cam")
cam_obj  = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam_obj)
scene.camera = cam_obj

def render_view(name, loc, rot_deg):
    cam_obj.location = loc
    cam_obj.rotation_euler = [math.radians(r) for r in rot_deg]
    scene.render.filepath = f"{{out_dir}}/preview_{{name}}.png"
    bpy.ops.render.render(write_still=True)
    print(f"[RENDER] {{name}} → {{scene.render.filepath}}")

render_view("perspective", (3.5, -3.5, 2.5), (65, 0, 45))
render_view("top",         (0,   0,   5.0),  (0,  0, 0))
render_view("front",       (0,  -4.0, 0.5),  (90, 0, 0))
print("[RENDER] done")
"""


def _render_previews(stl_path: Path, output_dir: Path) -> list[Path]:
    """
    Use Blender headless to render perspective / top / front PNG previews.
    Returns list of PNG paths, or empty list if Blender is unavailable.
    """
    import shutil
    blender = shutil.which("blender")
    if not blender:
        print("[FORGE] Blender not found — skipping preview render")
        return []

    script_path = output_dir / "_render_script.py"
    script_path.write_text(
        _BLENDER_RENDER_SCRIPT.format(stl=str(stl_path), out=str(output_dir))
    )

    print("[FORGE] Rendering STL previews via Blender headless …")
    result = subprocess.run(
        [blender, "--background", "--python", str(script_path)],
        capture_output=True, text=True, timeout=60,
    )
    pngs = sorted(output_dir.glob("preview_*.png"))
    if pngs:
        print(f"[FORGE] ✓ Preview renders ready ({len(pngs)} images):")
        for p in pngs:
            print(f"        {p}")
    else:
        print(f"[FORGE] Blender render produced no output — check {output_dir}/_render_script.py")
        if result.stderr:
            print(result.stderr[-500:])
    return pngs


# ─── Step 5: Human visual check ──────────────────────────────────────────────

def _open_previews(pngs: list[Path]) -> None:
    """
    Launch EOG (Eye of GNOME) with all preview PNGs in the background.
    Falls back to xdg-open one-by-one if eog is unavailable.
    Non-blocking — returns immediately so the y/N prompt still appears.
    """
    import shutil
    if not pngs:
        return
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")

    eog = shutil.which("eog")
    if eog:
        # eog accepts multiple files and shows them in a single window gallery
        subprocess.Popen(
            [eog] + [str(p) for p in pngs],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        print(f"[FORGE] ✓ EOG image viewer opened ({len(pngs)} views) — inspect, then return here")
        return

    xdg = shutil.which("xdg-open")
    if xdg:
        for p in pngs:
            subprocess.Popen(
                [xdg, str(p)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        print(f"[FORGE] ✓ Opened {len(pngs)} preview images via xdg-open")
        return

    print("[FORGE] No image viewer found — open PNGs manually from the paths below")


def _human_review(stl_path: Path, gcode_path: Path, validation: dict) -> bool:
    """
    Render STL previews, auto-launch image viewer, then pause for Y/N.
    Returns True if approved.
    """
    output_dir = stl_path.parent
    pngs = _render_previews(stl_path, output_dir)

    # Pop open the image viewer before the prompt — non-blocking
    _open_previews(pngs)

    print()
    print("=" * 60)
    print("  FORGE MANAGER V1 — HUMAN REVIEW REQUIRED")
    print("=" * 60)
    print()
    if pngs:
        print("  3 render views are open in EOG — perspective, top, front.")
        print("  Use arrow keys in EOG to switch between views.")
    else:
        print(f"  STL (3D model):  {stl_path}")
    print()
    print(f"  G-code:  {gcode_path}")
    print(f"  (view:   cat {gcode_path})")
    print()

    if not validation["ok"]:
        print("  ⚠️  G-code validator flagged issues:")
        for issue in validation["issues"]:
            print(f"      {issue}")
        print()

    print("  G-code note: V1 stub — perimeter contour only.")
    print("  Full toolpath requires FreeCAD Path Job (V2).")
    print()

    answer = input("  Approve for production? [y/N]: ").strip().lower()
    approved = answer in ("y", "yes")
    print()
    if approved:
        print("  ✓ Approved — advancing to In Production")
    else:
        print("  ✗ Rejected — order will not advance")
    print("=" * 60)
    print()
    return approved


# ─── Step 6: Update order state + notify ─────────────────────────────────────

def _update_order_to_production(order_id: str, description: str, paths: dict) -> None:
    """
    - Upsert order status to "In Production" in Eliza DB
    - Write memory entry (CS agent can pick this up)
    - Send Telegram alert
    """
    # Eliza DB — status update
    eliza_memory.upsert_order(order_id, status="In Production")

    # Memory entry so Customer Service Agent can query "where is my order?"
    eliza_memory.remember(
        agent   = AGENT_NAME,
        type_   = "ORDER_PRODUCTION",
        content = (
            f"Order {order_id} entered production. "
            f"Part: {description[:120]}. "
            f"STL: {paths.get('stl', '')}. "
            f"G-code: {paths.get('gcode', '')}."
        ),
        metadata={
            "order_id":   order_id,
            "status":     "In Production",
            "stl_path":   str(paths.get("stl", "")),
            "gcode_path": str(paths.get("gcode", "")),
            "step_path":  str(paths.get("step", "")),
            "bbox_mm":    paths.get("bbox_mm"),
        },
    )

    # Telegram
    send_telegram_alert(
        f"🔨 FORGE: Order {order_id} → IN PRODUCTION\n"
        f"Part: {description[:80]}\n"
        f"STL ready: {paths.get('stl', 'N/A')}"
    )
    print(f"[FORGE] ✓ Order {order_id} status → In Production (DB + Telegram)")


# ─── Main entry point ─────────────────────────────────────────────────────────

def forge_manager_v1(order_id: str, description: str) -> dict:
    """
    Linear happy-path: PAID order → In Production.

    Args:
        order_id:    Eliza DB order identifier (must be PAID status).
        description: Plain-English part description fed to FreeCAD generator.

    Returns:
        dict with keys: status, order_id, paths (stl/step/gcode), approved.
    """
    print()
    print("=" * 60)
    print(f"  FORGE MANAGER V1 — Order {order_id}")
    print(f"  Part: {description}")
    print("=" * 60)
    print()

    output_dir = OUTPUT_BASE / order_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Verify PAID ────────────────────────────────────────────────────────
    _verify_paid_order(order_id)

    # ── 2. Generate FreeCAD script ────────────────────────────────────────────
    part_code = _generate_freecad_script(description, output_dir)

    # ── 3. Run FreeCAD headless → STEP + STL ─────────────────────────────────
    freecad_paths = _run_freecad_headless(order_id, part_code, output_dir)

    # ── 4. Generate stub G-code + validate ───────────────────────────────────
    gcode_path  = _generate_stub_gcode(order_id, description, freecad_paths, output_dir)
    validation  = _validate_gcode(gcode_path)
    freecad_paths["gcode"] = gcode_path

    # ── 4b. Render toolpath visualisation ────────────────────────────────────
    _render_toolpath(gcode_path, output_dir, freecad_paths.get("bbox_mm", []))

    # ── 5. Human review ───────────────────────────────────────────────────────
    approved = _human_review(freecad_paths["stl"], gcode_path, validation)

    # ── 6. Biofeedback + order update ─────────────────────────────────────────
    if approved:
        biofeedback.append_reward(
            AGENT_NAME,
            f"Order {order_id} approved at human review — entering production",
            event_type="order_success",
        )
        _update_order_to_production(order_id, description, freecad_paths)
        result = {"status": "in_production", "approved": True}
    else:
        biofeedback.append_constraint(
            AGENT_NAME,
            f"Order {order_id} rejected at human review",
            event_type="order_fail",
        )
        send_telegram_alert(f"⚠️ FORGE: Order {order_id} rejected at human review")
        result = {"status": "rejected", "approved": False}

    # Write run log
    log = {
        "order_id":    order_id,
        "description": description,
        "result":      result,
        "paths": {
            "step":  str(freecad_paths.get("step",  "")),
            "stl":   str(freecad_paths.get("stl",   "")),
            "gcode": str(freecad_paths.get("gcode", "")),
        },
        "bbox_mm":     freecad_paths.get("bbox_mm"),
        "validation":  validation,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "forge_log.json").write_text(json.dumps(log, indent=2))
    print(f"[FORGE] Run log → {output_dir}/forge_log.json")

    result["order_id"] = order_id
    result["paths"]    = freecad_paths
    return result


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 forge_manager_v1.py <order_id> \"<part description>\"")
        print()
        print("Example:")
        print('  python3 forge_manager_v1.py order_test_001 "5 inch steel bracket with 4 mounting holes"')
        sys.exit(1)

    _order_id    = sys.argv[1]
    _description = " ".join(sys.argv[2:])

    result = forge_manager_v1(_order_id, _description)
    print(f"\nResult: {json.dumps({k: str(v) if not isinstance(v, (str, bool, int, float, type(None))) else v for k, v in result.items() if k != 'paths'}, indent=2)}")
    sys.exit(0 if result.get("approved") else 1)
