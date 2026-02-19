#!/usr/bin/env python3
"""
Biofeedback module — Reward and constraint logging for agent scaling logic.
Agents call append_reward() or append_constraint(); scaler.py reads and computes scale_state.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# Biofeedback dir: next to the log file in project_docs
BIOFEEDBACK_DIR = Path.home() / "project_docs" / "biofeedback"
REWARDS_PATH = BIOFEEDBACK_DIR / "rewards.md"
CONSTRAINTS_PATH = BIOFEEDBACK_DIR / "constraints.md"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"

INSERT_MARKER = "---\n\n"  # Append new entries after this


def _ensure_dir() -> None:
    BIOFEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def append_reward(agent: str, message: str, kpi: str | None = None) -> None:
    """Append a reward entry. Called when an action hits a KPI (e.g. SEO pass)."""
    _ensure_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kpi_part = f" | {kpi}" if kpi else ""
    entry = f"- [{timestamp}] [{agent}] {message}{kpi_part}\n"
    if REWARDS_PATH.exists():
        content = REWARDS_PATH.read_text()
        if INSERT_MARKER in content:
            before, after = content.split(INSERT_MARKER, 1)
            new_content = before + INSERT_MARKER + entry + after
        else:
            new_content = content.rstrip() + "\n\n" + entry
    else:
        new_content = "# Biofeedback — Rewards (Positive Feedback)\n\n*Newest at top.*\n\n---\n\n" + entry
    REWARDS_PATH.write_text(new_content)


def append_constraint(agent: str, message: str) -> None:
    """Append a constraint entry. Called when Monitor/Security/Marketing fails."""
    _ensure_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"- [{timestamp}] [{agent}] {message}\n"
    if CONSTRAINTS_PATH.exists():
        content = CONSTRAINTS_PATH.read_text()
        if INSERT_MARKER in content:
            before, after = content.split(INSERT_MARKER, 1)
            new_content = before + INSERT_MARKER + entry + after
        else:
            new_content = content.rstrip() + "\n\n" + entry
    else:
        new_content = "# Biofeedback — Constraints (Negative Feedback)\n\n*Newest at top.*\n\n---\n\n" + entry
    CONSTRAINTS_PATH.write_text(new_content)
