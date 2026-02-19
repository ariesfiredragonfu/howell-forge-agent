#!/usr/bin/env python3
"""
Orchestrator — Biofeedback Phase 4
Runs scaler, then agents. In throttle mode, runs only Monitor + Security (critical).
In normal/high mode, runs all agents. Use with cron for automated checks.
"""

import json
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).parent.resolve()
BIOFEEDBACK_DIR = Path.home() / "project_docs" / "biofeedback"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"

AGENTS = ["monitor.py", "security.py", "marketing.py"]
CRITICAL_ONLY = ["monitor.py", "security.py"]


def run_scaler() -> str:
    """Run scaler, return mode (high, normal, throttle)."""
    result = subprocess.run(
        [sys.executable, str(AGENT_DIR / "scaler.py")],
        capture_output=True,
        text=True,
        cwd=str(AGENT_DIR),
    )
    if SCALE_STATE_PATH.exists():
        state = json.loads(SCALE_STATE_PATH.read_text())
        return state.get("mode", "normal")
    return "normal"


def run_agent(script: str) -> int:
    """Run agent script, return exit code."""
    return subprocess.run(
        [sys.executable, str(AGENT_DIR / script)],
        cwd=str(AGENT_DIR),
    ).returncode


def main() -> int:
    mode = run_scaler()
    agents_to_run = CRITICAL_ONLY if mode == "throttle" else AGENTS
    if mode == "throttle":
        print(f"Scale: THROTTLE — running Monitor + Security only (skipping Marketing)")
    elif mode == "high":
        print(f"Scale: HIGH — running all agents")
    else:
        print(f"Scale: NORMAL — running all agents")

    exit_codes = []
    for script in agents_to_run:
        code = run_agent(script)
        exit_codes.append(code)
        if code != 0:
            print(f"  {script}: FAILED ({code})")
        else:
            print(f"  {script}: OK")
    return 1 if any(c != 0 for c in exit_codes) else 0


if __name__ == "__main__":
    sys.exit(main())
