#!/usr/bin/env python3
"""
Forge Manager V1 — Linear happy-path: PAID order → In Production.

Flow:
  1. Verify order is PAID (reads from Eliza DB)
  2. Generate FreeCAD Python script via xAI Grok (turns description → CAD code)
  3. Run FreeCAD headlessly → exports STEP + STL
  4. Generate stub G-code (bounding-box contour) → validate with gcode_validator
  5. PAUSE for human visual check (open STL in FreeCAD, review G-code)
  6. On approval  → biofeedback reward, order → "In Production", Telegram + CS notify
     On rejection → biofeedback constraint, order stays, Telegram alert

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

XAI_API_KEY_PATH  = Path.home() / ".config" / "howell-forge" / "xai-api-key"
OUTPUT_BASE       = Path.home() / "Hardware_Factory" / "forge_orders"
FREECADCMD        = "freecadcmd"
AGENT_NAME        = "FORGE_MANAGER"
GCODE_VALIDATOR   = Path(__file__).parent / "gcode_validator.py"

# Machine limits (mm) — must match gcode_validator
X_MAX_MM, Y_MAX_MM, Z_MAX_MM = 300.0, 300.0, 100.0


# ─── xAI Grok helper ─────────────────────────────────────────────────────────

def _xai_key() -> str:
    """Read xAI API key from file or env."""
    env_key = os.environ.get("XAI_API_KEY", "").strip()
    if env_key:
        return env_key
    if XAI_API_KEY_PATH.exists():
        return XAI_API_KEY_PATH.read_text().strip()
    raise RuntimeError(f"xAI API key not found. Set XAI_API_KEY env or create {XAI_API_KEY_PATH}")


def _call_grok(prompt: str, model: str = "grok-2-latest") -> str:
    """Call xAI Grok via OpenAI-compatible endpoint. Returns content string."""
    import urllib.request, urllib.error
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_xai_key()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


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


# ─── Step 2: Generate FreeCAD script via xAI Grok ────────────────────────────

_FREECAD_SYSTEM_PROMPT = """\
You are a FreeCAD 0.21.2 expert writing headless Python scripts for a CNC shop.
Rules:
- Use only: FreeCAD, Part, MeshPart modules (no GUI, no App.Gui, no Mesh.createFrom)
- To export STL use: import MeshPart; mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.1, AngularDeflection=0.08, Relative=False); mesh.write(OUTPUT_STL)
- Create ONE document, build the part, export STEP to OUTPUT_STEP and STL to OUTPUT_STL
- OUTPUT_STEP and OUTPUT_STL are Python variables already defined before your code runs
- Use inch units (25.4 mm per inch) for all dimensions
- Add a print() at the end with the bounding box in mm: print("BBOX:", bbox.XLength, bbox.YLength, bbox.ZLength)
- Keep it under 80 lines
- No try/except — V1 happy path only
"""

_FREECAD_USER_PROMPT = """\
Write a FreeCAD 0.21.2 headless Python script for this part:

  {description}

The script will be embedded inside a runner that already defines:
    OUTPUT_STEP = "/path/to/part.step"
    OUTPUT_STL  = "/path/to/part.stl"

Use Part.makeBox / Part.makeCylinder / Part.Cut as appropriate.
End with: print("BBOX:", shape.BoundBox.XLength, shape.BoundBox.YLength, shape.BoundBox.ZLength)
"""


def _generate_freecad_script(description: str, output_dir: Path) -> str:
    """
    Call xAI Grok to turn a part description into a FreeCAD Python script.
    Falls back to a parametric template if the API call fails.
    Returns the Python source code as a string.
    """
    print(f"[FORGE] Generating FreeCAD script for: {description!r}")
    try:
        prompt = _FREECAD_USER_PROMPT.format(description=description)
        raw    = _call_grok(_FREECAD_SYSTEM_PROMPT + "\n\n" + prompt)
        # Strip markdown fences if Grok wraps in ```python ... ```
        code = raw.strip()
        if code.startswith("```"):
            lines = code.splitlines()
            code  = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            )
        print("[FORGE] ✓ FreeCAD script generated via xAI Grok")
        return code
    except Exception as exc:
        print(f"[FORGE] xAI call failed ({exc}) — using parametric fallback template")
        return _fallback_freecad_script(description)


def _fallback_freecad_script(description: str) -> str:
    """
    Parametric fallback: parse simple dimensions from the description text
    and generate a rectangular bracket with 4 corner holes.
    Used when the xAI API is unavailable.
    """
    import re
    # Extract first number sequence for length/width/thickness
    nums = re.findall(r"\d+\.?\d*", description)
    l_in = float(nums[0]) if len(nums) > 0 else 5.0
    w_in = float(nums[1]) if len(nums) > 1 else 3.0
    t_in = float(nums[2]) if len(nums) > 2 else 0.5
    l_mm, w_mm, t_mm = l_in * 25.4, w_in * 25.4, t_in * 25.4

    return textwrap.dedent(f"""\
        import FreeCAD, Part, Mesh
        doc = FreeCAD.newDocument("ForgePart")

        # Main stock block
        stock = Part.makeBox({l_mm:.3f}, {w_mm:.3f}, {t_mm:.3f})

        # Four corner mounting holes (Ø6.35 mm = 0.25 in)
        hole_r  = 3.175
        hole_d  = {t_mm:.3f} + 2
        margin  = 12.7   # 0.5 in from each edge
        holes   = []
        for xo in [margin, {l_mm:.3f} - margin]:
            for yo in [margin, {w_mm:.3f} - margin]:
                c = Part.makeCylinder(hole_r, hole_d, FreeCAD.Vector(xo, yo, -1))
                holes.append(c)
        cut = stock
        for h in holes:
            cut = cut.cut(h)

        doc.addObject("Part::Feature", "Bracket").Shape = cut
        doc.recompute()

        # Export STEP
        Part.export([doc.getObject("Bracket")], OUTPUT_STEP)

        # Export STL — MeshPart is the headless-safe API in FreeCAD 0.21.x
        import MeshPart
        mesh = MeshPart.meshFromShape(Shape=cut, LinearDeflection=0.1, AngularDeflection=0.08, Relative=False)
        mesh.write(OUTPUT_STL)

        bbox = cut.BoundBox
        print("BBOX:", bbox.XLength, bbox.YLength, bbox.ZLength)
    """)


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


# ─── Step 5: Human visual check ──────────────────────────────────────────────

def _human_review(stl_path: Path, gcode_path: Path, validation: dict) -> bool:
    """
    Pause for human inspection.
    Prints file paths so the operator can open them, then prompts Y/N.
    Returns True if approved.
    """
    print()
    print("=" * 60)
    print("  FORGE MANAGER V1 — HUMAN REVIEW REQUIRED")
    print("=" * 60)
    print()
    print("  Open the following files and inspect before approving:")
    print()
    print(f"  STL (3D preview):   {stl_path}")
    print(f"  G-code:             {gcode_path}")
    print()
    print("  Commands:")
    print(f"    freecadcmd --open {stl_path}   (or open in FreeCAD GUI)")
    print(f"    cat {gcode_path}")
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
