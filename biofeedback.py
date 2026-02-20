#!/usr/bin/env python3
"""
Biofeedback — Reward/constraint logging + EWMA score (Grok-Gemini 2026 Resilience Stack).

Score model
───────────
Uses a continuous-time Exponentially Weighted Moving Average with a 7-day
half-life.  When a new event arrives at time t:

    1. elapsed   = t - last_event_ts               (seconds since last event)
    2. decayed   = score * exp(−λ × elapsed)        (apply decay for idle time)
    3. new_score = max(FLOOR, decayed + weight)     (add event weight, clamp floor)

    λ = ln(2) / HALF_LIFE_SEC                      (≈ 1.14e-6 s⁻¹)

This means the score naturally trends toward 0 when nothing happens, rewards
recent wins more than old ones, and never "remembers" a burst of errors forever.

Event weights (from directive):
    security_pass   +3.0    monitoring pass, no auth errors
    security_fail   −2.0    headers/HTTPS check failure
    marketing_pass  +1.0    SEO check passes
    marketing_fail  −1.0    SEO check fails
    monitor_pass    +1.0    site health check passes
    monitor_fail    −2.0    site down / Stripe API unreachable
    order_success   +1.0    order processed PAID
    order_fail      −1.0    order failed / expired
    circuit_open    −3.0    Kaito circuit breaker tripped

Human-readable markdown logs are preserved for auditing.
Machine-readable EWMA state lives in biofeedback/ewma_state.json.
scaler.py reads get_score() instead of counting file lines.
"""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

BIOFEEDBACK_DIR  = Path.home() / "project_docs" / "biofeedback"
REWARDS_PATH     = BIOFEEDBACK_DIR / "rewards.md"
CONSTRAINTS_PATH = BIOFEEDBACK_DIR / "constraints.md"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"
EWMA_STATE_PATH  = BIOFEEDBACK_DIR / "ewma_state.json"

INSERT_MARKER = "---\n\n"

# ─── EWMA constants ───────────────────────────────────────────────────────────

HALF_LIFE_DAYS = 7
HALF_LIFE_SEC  = HALF_LIFE_DAYS * 24 * 3600      # 604 800 s
DECAY_LAMBDA   = math.log(2) / HALF_LIFE_SEC     # ≈ 1.1447e-6 s⁻¹
SCORE_FLOOR    = -10.0

# Directive-specified weights + sensible defaults for the full event set
EVENT_WEIGHTS: dict[str, float] = {
    "security_pass":  3.0,
    "security_fail":  -2.0,
    "marketing_pass": 1.0,
    "marketing_fail": -1.0,   # directive weight
    "monitor_pass":   1.0,
    "monitor_fail":   -2.0,
    "order_success":  1.0,
    "order_fail":     -1.0,
    "circuit_open":   -3.0,   # Kaito circuit breaker opened
}


# ─── EWMA state I/O ──────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    BIOFEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _load_ewma() -> dict:
    if EWMA_STATE_PATH.exists():
        try:
            return json.loads(EWMA_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"score": 0.0, "last_event_ts": None, "event_count": 0}


def _save_ewma(state: dict) -> None:
    _ensure_dir()
    EWMA_STATE_PATH.write_text(json.dumps(state, indent=2))


# ─── EWMA core ────────────────────────────────────────────────────────────────

def _decay(score: float, last_ts: Optional[float], now_ts: float) -> float:
    """Apply exponential decay for the time elapsed since the last event."""
    if last_ts is None:
        return score
    elapsed = max(0.0, now_ts - last_ts)
    return score * math.exp(-DECAY_LAMBDA * elapsed)


def record_event(event_type: str, agent: str = "UNKNOWN") -> float:
    """
    Record a biofeedback event and return the new EWMA score.

    This is the primary write path for the score engine.
    append_reward() and append_constraint() call this internally.

    Args:
        event_type: Key from EVENT_WEIGHTS (unknown types score 0).
        agent:      Agent name for logging.

    Returns:
        The new EWMA score after applying decay and the event weight.
    """
    _ensure_dir()
    weight = EVENT_WEIGHTS.get(event_type, 0.0)
    if weight == 0.0:
        return get_score()

    now_ts = time.time()
    state  = _load_ewma()

    decayed   = _decay(state["score"], state.get("last_event_ts"), now_ts)
    new_score = max(SCORE_FLOOR, decayed + weight)

    state["score"]         = new_score
    state["last_event_ts"] = now_ts
    state["event_count"]   = state.get("event_count", 0) + 1
    _save_ewma(state)
    return new_score


def get_score() -> float:
    """
    Current EWMA score with decay applied up to now (no new event recorded).
    Returns 0.0 if no events have ever been recorded.
    """
    state = _load_ewma()
    if state.get("last_event_ts") is None:
        return state.get("score", 0.0)
    decayed = _decay(state["score"], state["last_event_ts"], time.time())
    return max(SCORE_FLOOR, decayed)


def get_ewma_state() -> dict:
    """Return the full EWMA state dict (score, last_event_ts, event_count) for diagnostics."""
    state = _load_ewma()
    state["current_score"] = get_score()
    state["half_life_days"] = HALF_LIFE_DAYS
    return state


# ─── Human-readable markdown logs ─────────────────────────────────────────────
# Kept for auditing.  append_reward / append_constraint also drive record_event.

def append_reward(
    agent: str,
    message: str,
    kpi: Optional[str] = None,
    event_type: str = "marketing_pass",
) -> float:
    """
    Append a reward entry to rewards.md and update the EWMA score.

    Args:
        event_type: EWMA event key (default: "marketing_pass").
                    Pass "security_pass", "monitor_pass", or "order_success"
                    for the appropriate agent context.

    Returns:
        New EWMA score.
    """
    _ensure_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kpi_part  = f" | {kpi}" if kpi else ""
    entry     = f"- [{timestamp}] [{agent}] {message}{kpi_part}\n"

    if REWARDS_PATH.exists():
        content = REWARDS_PATH.read_text()
        if INSERT_MARKER in content:
            before, after = content.split(INSERT_MARKER, 1)
            REWARDS_PATH.write_text(before + INSERT_MARKER + entry + after)
        else:
            REWARDS_PATH.write_text(content.rstrip() + "\n\n" + entry)
    else:
        REWARDS_PATH.write_text(
            "# Biofeedback — Rewards (Positive Feedback)\n\n"
            "*Newest at top.*\n\n---\n\n" + entry
        )

    return record_event(event_type, agent)


def append_constraint(
    agent: str,
    message: str,
    event_type: str = "marketing_fail",
) -> float:
    """
    Append a constraint entry to constraints.md and update the EWMA score.

    Args:
        event_type: EWMA event key (default: "marketing_fail").
                    Pass "security_fail", "monitor_fail", or "order_fail"
                    for the appropriate agent context.

    Returns:
        New EWMA score.
    """
    _ensure_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry     = f"- [{timestamp}] [{agent}] {message}\n"

    if CONSTRAINTS_PATH.exists():
        content = CONSTRAINTS_PATH.read_text()
        if INSERT_MARKER in content:
            before, after = content.split(INSERT_MARKER, 1)
            CONSTRAINTS_PATH.write_text(before + INSERT_MARKER + entry + after)
        else:
            CONSTRAINTS_PATH.write_text(content.rstrip() + "\n\n" + entry)
    else:
        CONSTRAINTS_PATH.write_text(
            "# Biofeedback — Constraints (Negative Feedback)\n\n"
            "*Newest at top.*\n\n---\n\n" + entry
        )

    return record_event(event_type, agent)
