#!/usr/bin/env python3
"""
Orchestrator — Biofeedback Phase 4 + ElizaOS Order Loop Edition

Runs scaler, then agents. In throttle mode, runs only Monitor + Security.
In normal/high mode, runs all agents including an Order Loop one-shot sync.

ElizaOS Order Loop:
  --order-loop-once   Run a single Stripe sync + Kaito drain pass (default in cron)
  --order-loop-start  Start the Order Loop daemon in the background (long-running)
  --order-loop-status Print current Eliza memory state and exit

Existing flags unchanged — backward compatible.
"""

import json
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).parent.resolve()
BIOFEEDBACK_DIR = Path.home() / "project_docs" / "biofeedback"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"

# Standard check agents (unchanged)
AGENTS = ["monitor.py", "security.py", "marketing.py"]
CRITICAL_ONLY = ["monitor.py", "security.py"]

# Order Loop script (ElizaOS edition)
ORDER_LOOP_SCRIPT = "run_order_loop.py"


def run_scaler() -> str:
    """Run scaler, return mode (high, normal, throttle)."""
    subprocess.run(
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
    """Run agent script synchronously, return exit code."""
    return subprocess.run(
        [sys.executable, str(AGENT_DIR / script)],
        cwd=str(AGENT_DIR),
    ).returncode


def run_order_loop_once() -> int:
    """Run a single Order Loop sync + drain pass (suitable for cron)."""
    return subprocess.run(
        [sys.executable, str(AGENT_DIR / ORDER_LOOP_SCRIPT), "--once"],
        cwd=str(AGENT_DIR),
    ).returncode


def start_order_loop_daemon(workers: int = 3) -> subprocess.Popen:
    """
    Launch the Order Loop daemon as a background process.
    Returns the Popen handle so the caller can monitor or terminate it.
    """
    proc = subprocess.Popen(
        [sys.executable, str(AGENT_DIR / ORDER_LOOP_SCRIPT), "--workers", str(workers)],
        cwd=str(AGENT_DIR),
    )
    print(f"Order Loop daemon started (PID {proc.pid}, workers={workers})")
    return proc


def print_order_loop_status() -> None:
    """Print Eliza memory state via run_order_loop.py --status."""
    subprocess.run(
        [sys.executable, str(AGENT_DIR / ORDER_LOOP_SCRIPT), "--status"],
        cwd=str(AGENT_DIR),
    )


def main() -> int:
    args = sys.argv[1:]

    # ── Order Loop sub-commands ───────────────────────────────────────────────
    if "--order-loop-status" in args:
        print_order_loop_status()
        return 0

    if "--order-loop-start" in args:
        workers = 3
        try:
            idx = args.index("--workers")
            workers = int(args[idx + 1])
        except (ValueError, IndexError):
            pass
        start_order_loop_daemon(workers=workers)
        return 0

    # ── Standard orchestration ────────────────────────────────────────────────
    mode = run_scaler()
    agents_to_run = CRITICAL_ONLY if mode == "throttle" else AGENTS

    if mode == "throttle":
        print("Scale: THROTTLE — running Monitor + Security only (skipping Marketing + Order Loop)")
    elif mode == "high":
        print("Scale: HIGH — running all agents + Order Loop sync")
    else:
        print("Scale: NORMAL — running all agents + Order Loop sync")

    exit_codes = []
    for script in agents_to_run:
        code = run_agent(script)
        exit_codes.append(code)
        print(f"  {script}: {'OK' if code == 0 else f'FAILED ({code})'}")

    # Run Order Loop one-shot sync unless throttled
    if mode != "throttle" or "--order-loop-once" in args:
        print("  run_order_loop.py (--once):", end=" ", flush=True)
        code = run_order_loop_once()
        exit_codes.append(code)
        print("OK" if code == 0 else f"FAILED ({code})")

    return 1 if any(c != 0 for c in exit_codes) else 0


if __name__ == "__main__":
    sys.exit(main())
