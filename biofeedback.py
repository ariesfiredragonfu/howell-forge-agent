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

_CONFIG_PATH = Path(__file__).parent / "eliza-config.json"

# ─── Config reader ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Read the biofeedback block from eliza-config.json.  Thread-safe: each
    call re-reads the file so a live config change takes effect on the next event."""
    try:
        raw = json.loads(_CONFIG_PATH.read_text())
        return raw.get("biofeedback", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _use_ewma() -> bool:
    return _load_config().get("use_ewma", True)


# ─── EWMA constants ───────────────────────────────────────────────────────────

HALF_LIFE_DAYS = 7
HALF_LIFE_SEC  = HALF_LIFE_DAYS * 24 * 3600      # 604 800 s
DECAY_LAMBDA   = math.log(2) / HALF_LIFE_SEC     # ≈ 1.1447e-6 s⁻¹
SCORE_FLOOR    = -10.0

# Directive-specified weights + sensible defaults for the full event set
EVENT_WEIGHTS: dict[str, float] = {
    "security_pass":            3.0,
    "security_fail":           -2.0,
    "marketing_pass":           1.0,
    "marketing_fail":          -1.0,
    "monitor_pass":             1.0,
    "monitor_fail":            -2.0,
    "order_success":            1.0,
    "order_fail":              -1.0,
    "circuit_open":            -3.0,   # Kaito circuit breaker opened
    # Herald / X (Twitter) signals
    "seo_pass":                 1.0,   # SEO check passes (distinct from generic marketing_pass)
    "x_engagement_high":        2.0,   # X post hits high engagement threshold
    "marketing_validation_fail":-1.0,  # Herald content validation failure
    "x_bot_risk":              -0.5,   # X post flagged for bot-risk patterns
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
    return {
        "score": 0.0,
        "last_event_ts": None,
        "event_count": 0,
        "positive_count": 0,   # for legacy fallback
        "negative_count": 0,   # for legacy fallback
    }


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


# ─── Legacy (count-based) fallback ───────────────────────────────────────────

def _legacy_score(state: dict) -> float:
    """
    Simple event-count score used when use_ewma=false.

    score = (positive_count - negative_count) normalised to [-10, +10].
    Capped at ±10 so extreme histories don't lock the scaler.
    No decay — every event counts equally regardless of age.
    """
    pos = state.get("positive_count", 0)
    neg = state.get("negative_count", 0)
    raw = float(pos - neg)
    return max(SCORE_FLOOR, min(10.0, raw))


# ─── EWMA core ────────────────────────────────────────────────────────────────

def _audit_remember(
    agent: str,
    event_type: str,
    pre_decay_score: float,
    decayed_score: float,
    weight: float,
    new_score: float,
    elapsed_sec: float,
) -> None:
    """
    Write an audit memory entry to Eliza memory.

    Best-effort — DB unavailability must never break the scoring path.
    Lazy import avoids the circular-import risk (biofeedback ← eliza_memory
    ← eliza_db; none of those import biofeedback).
    """
    try:
        import eliza_memory  # noqa: PLC0415  (lazy intentionally)
        elapsed_days = elapsed_sec / 86400
        decay_delta  = decayed_score - pre_decay_score
        rationale = (
            f"EWMA decay applied: {decay_delta:+.4f} "
            f"from {elapsed_days:.2f}-day-old score "
            f"(λ·t={DECAY_LAMBDA * elapsed_sec:.4f}); "
            f"event={event_type} weight={weight:+.1f}; "
            f"score {pre_decay_score:.4f} → {decayed_score:.4f} → {new_score:.4f}"
        )
        eliza_memory.remember(
            agent   = agent,
            type_   = "BIOFEEDBACK_EWMA",
            content = rationale,
            metadata= {
                "event_type":    event_type,
                "weight":        weight,
                "pre_decay":     round(pre_decay_score, 6),
                "post_decay":    round(decayed_score,   6),
                "new_score":     round(new_score,       6),
                "elapsed_days":  round(elapsed_days,    4),
                "half_life_days": HALF_LIFE_DAYS,
            },
        )
    except Exception:
        pass  # audit is best-effort; never crash the scoring path


def record_event(
    event_type: str,
    agent: str = "UNKNOWN",
    _now_ts: Optional[float] = None,   # injectable for tests; None → time.time()
) -> float:
    """
    Record a biofeedback event and return the new score.

    Primary write path — append_reward() and append_constraint() call this.

    When use_ewma=true (default):
      1. Decay the stored score for time elapsed since the last event.
      2. Add the event weight.
      3. Clamp to [SCORE_FLOOR, +∞].
      4. Write an audit entry to Eliza memory.

    When use_ewma=false:
      Increment positive_count or negative_count and return _legacy_score().

    Args:
        event_type: Key from EVENT_WEIGHTS (unknown types score 0 and skip audit).
        agent:      Agent name for logging and memory entry.
        _now_ts:    Unix timestamp override (for deterministic tests).
    """
    _ensure_dir()
    weight = EVENT_WEIGHTS.get(event_type, 0.0)
    if weight == 0.0:
        return get_score()

    now_ts = _now_ts if _now_ts is not None else time.time()
    state  = _load_ewma()

    # ── Track raw counts for legacy fallback regardless of mode ───────────────
    if weight > 0:
        state["positive_count"] = state.get("positive_count", 0) + 1
    else:
        state["negative_count"] = state.get("negative_count", 0) + 1

    state["event_count"] = state.get("event_count", 0) + 1

    if not _use_ewma():
        _save_ewma(state)
        return _legacy_score(state)

    # ── EWMA path ─────────────────────────────────────────────────────────────
    pre_decay_score = state["score"]
    elapsed_sec     = max(0.0, now_ts - state["last_event_ts"]) \
                      if state.get("last_event_ts") is not None else 0.0
    decayed         = _decay(pre_decay_score, state.get("last_event_ts"), now_ts)
    new_score       = max(SCORE_FLOOR, decayed + weight)

    state["score"]         = new_score
    state["last_event_ts"] = now_ts
    _save_ewma(state)

    _audit_remember(agent, event_type, pre_decay_score, decayed, weight, new_score, elapsed_sec)
    return new_score


def get_score() -> float:
    """
    Current score with decay applied up to now (no new event recorded).

    When use_ewma=false returns _legacy_score() from stored counts.
    Returns 0.0 if no events have ever been recorded.
    """
    state = _load_ewma()
    if not _use_ewma():
        return _legacy_score(state)
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
