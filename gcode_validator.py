#!/usr/bin/env python3
"""
G-code Validator — CNC safety check
Scans G-code for illegal/dangerous machine moves before sending to CNC.
Run before deploying to OctoPrint/Klipper.
Usage: python3 gcode_validator.py /path/to/file.gcode
"""

import re
import sys
from pathlib import Path

# Optional: use Monitor's log and biofeedback when available
try:
    from notifications import send_telegram_alert
    NOTIFICATIONS_AVAILABLE = True
except ImportError:
    NOTIFICATIONS_AVAILABLE = False

try:
    import biofeedback
    BIOFEEDBACK_AVAILABLE = True
except ImportError:
    BIOFEEDBACK_AVAILABLE = False

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"

# Machine limits (mm) — adjust for your CNC
X_MIN, X_MAX = -100, 300
Y_MIN, Y_MAX = -100, 300
Z_MIN, Z_MAX = -5, 100

# Rapid plunge threshold: G0 with Z drop > this (mm) is dangerous
RAPID_PLUNGE_THRESHOLD_MM = 2.0


def parse_gcode(path: Path) -> list[dict]:
    """Parse G-code into list of {cmd, x, y, z, ...} dicts."""
    lines = path.read_text().splitlines()
    result = []
    x, y, z = None, None, None
    for line in lines:
        line = line.split(";")[0].strip()
        if not line:
            continue
        m = re.match(r"G(\d+)(?:\s+(.+))?", line)
        if m:
            g = int(m.group(1))
            args = m.group(2) or ""
            d = {"cmd": f"G{g}", "raw": line}
            for part in args.split():
                if part.startswith("X"):
                    x = float(part[1:])
                    d["x"] = x
                elif part.startswith("Y"):
                    y = float(part[1:])
                    d["y"] = y
                elif part.startswith("Z"):
                    z = float(part[1:])
                    d["z"] = z
            if x is not None:
                d["x"] = x
            if y is not None:
                d["y"] = y
            if z is not None:
                d["z"] = z
            result.append(d)
    return result


def validate(path: Path) -> tuple[bool, list[str]]:
    """Validate G-code file. Returns (ok, list of issues)."""
    if not path.exists():
        return False, [f"File not found: {path}"]

    issues = []
    cmds = parse_gcode(path)
    prev_z = None

    for i, c in enumerate(cmds):
        # Out-of-bounds
        if "x" in c:
            if c["x"] < X_MIN or c["x"] > X_MAX:
                issues.append(f"Line ~{i+1}: X={c['x']} out of bounds [{X_MIN},{X_MAX}]")
        if "y" in c:
            if c["y"] < Y_MIN or c["y"] > Y_MAX:
                issues.append(f"Line ~{i+1}: Y={c['y']} out of bounds [{Y_MIN},{Y_MAX}]")
        if "z" in c:
            if c["z"] < Z_MIN or c["z"] > Z_MAX:
                issues.append(f"Line ~{i+1}: Z={c['z']} out of bounds [{Z_MIN},{Z_MAX}]")

        # Rapid plunge (G0 with large Z drop)
        if c["cmd"] == "G0" and "z" in c and prev_z is not None:
            drop = prev_z - c["z"]
            if drop > RAPID_PLUNGE_THRESHOLD_MM:
                issues.append(f"Line ~{i+1}: Rapid plunge (G0) Z drop {drop:.1f}mm — use G1 with feed")

        if "z" in c:
            prev_z = c["z"]

    # Check for safety header (optional)
    raw = path.read_text()
    if "M84" in raw and "G28" not in raw:
        pass  # Some files disable steppers without homing — warn?
    # Homing present is good; we don't require it for now

    return len(issues) == 0, issues


def append_log(severity: str, message: str) -> None:
    """Append to Monitor log."""
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [GCODE_VALIDATOR] [{severity}]\n{message}\n"
    if LOG_PATH.exists():
        content = LOG_PATH.read_text()
        insert_after = "*Agents append below. Newest at top.*"
        if insert_after in content:
            before, after = content.split(insert_after, 1)
            new_content = before + insert_after + entry + "\n" + after
        else:
            new_content = content + entry
    else:
        new_content = "## Live Log Entries\n\n*Agents append below. Newest at top.*" + entry
    LOG_PATH.write_text(new_content)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 gcode_validator.py <file.gcode>")
        return 2

    path = Path(sys.argv[1])
    ok, issues = validate(path)

    if ok:
        print(f"G-code OK: {path}")
        return 0

    msg = "; ".join(issues)
    print(f"G-code VALIDATION FAILED: {path}")
    print(msg)
    append_log("HIGH", f"G-code validation failed — {path.name}: {msg}")
    if BIOFEEDBACK_AVAILABLE:
        biofeedback.append_constraint("GCODE_VALIDATOR", f"G-code failed: {path.name} — {msg}")
    if NOTIFICATIONS_AVAILABLE:
        send_telegram_alert(f"⚠️ [HIGH] G-code validator: {path.name} — {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
