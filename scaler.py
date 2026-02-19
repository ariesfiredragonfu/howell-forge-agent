#!/usr/bin/env python3
"""
Scaler — Biofeedback Phase 2
Reads rewards and constraints from the last N days, computes Reward-to-Constraint ratio,
writes scale_state.json. Cron/orchestrator can use this to adjust run frequency.
"""

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

BIOFEEDBACK_DIR = Path.home() / "project_docs" / "biofeedback"
REWARDS_PATH = BIOFEEDBACK_DIR / "rewards.md"
CONSTRAINTS_PATH = BIOFEEDBACK_DIR / "constraints.md"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"

WINDOW_DAYS = 7
CONSTRAINT_WEIGHT = 2  # Constraints count more than rewards
HIGH_THRESHOLD = 3   # score >= this → high scale
THROTTLE_THRESHOLD = -2  # score <= this → throttle


def _parse_timestamp_from_line(line: str) -> datetime | None:
    """Extract timestamp from a line like '- [2026-02-13 14:30:00 UTC] [AGENT] message'"""
    match = re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]", line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "[%Y-%m-%d %H:%M:%S UTC]").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _count_entries_since(path: Path, since: datetime) -> int:
    """Count entries in file with timestamp >= since."""
    if not path.exists():
        return 0
    content = path.read_text()
    count = 0
    for line in content.splitlines():
        ts = _parse_timestamp_from_line(line)
        if ts and ts >= since:
            count += 1
    return count


def compute_scale_state() -> dict:
    """Compute rewards/constraints in window and return scale state dict."""
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    rewards = _count_entries_since(REWARDS_PATH, since)
    constraints = _count_entries_since(CONSTRAINTS_PATH, since)
    score = rewards - (constraints * CONSTRAINT_WEIGHT)

    if score >= HIGH_THRESHOLD:
        mode = "high"
    elif score <= THROTTLE_THRESHOLD:
        mode = "throttle"
    else:
        mode = "normal"

    return {
        "mode": mode,
        "score": score,
        "rewards_count": rewards,
        "constraints_count": constraints,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
    }


def main() -> int:
    """Compute and write scale_state.json. Exit code: 0=normal, 1=throttle, 2=high (for cron)."""
    BIOFEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    state = compute_scale_state()
    SCALE_STATE_PATH.write_text(json.dumps(state, indent=2))
    print(f"Scale: {state['mode']} (score={state['score']}, rewards={state['rewards_count']}, constraints={state['constraints_count']})")
    if state["mode"] == "throttle":
        return 1
    if state["mode"] == "high":
        return 2
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
