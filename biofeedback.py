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
import logging
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

logger = logging.getLogger(__name__)

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


def _get_adaptive_config() -> dict:
    """Return the biofeedback.adaptive block, or {} if missing/disabled."""
    cfg = _load_config().get("adaptive", {})
    if not cfg.get("enabled", True):
        return {}
    return cfg


# ─── Redis event stream ───────────────────────────────────────────────────────
# howell:biofeedback_events — append-only stream; IDs encode event timestamp
# so XRANGE queries respect mock _now_ts in tests.

_STREAM_KEY = "howell:biofeedback_events"
_redis_client = None   # module-level singleton; lazy-init on first use


def _get_redis():
    """Return a live Redis client, or None if unavailable. Never raises."""
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None
    try:
        import redis as _redis_lib          # noqa: PLC0415
        r = _redis_lib.Redis(decode_responses=True)
        r.ping()
        _redis_client = r
        return _redis_client
    except Exception:
        return None


def _stream_xadd(event_type: str, base_weight: float, now_ts: float) -> None:
    """
    Append an event to the Redis stream using an explicit millisecond-epoch ID.

    The ID encodes `now_ts` so XRANGE-based count queries respect mock timestamps
    in tests.  Sub-millisecond uniqueness comes from time.time_ns().
    Falls back to auto-ID ('*') on ID collision (extremely rare).
    Best-effort — never raises.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        ms  = int(now_ts * 1000)
        seq = int(time.time_ns() % 1_000_000)   # sub-ms uniqueness
        fields = {
            "type":      event_type,
            "timestamp": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "weight":    str(base_weight),
        }
        try:
            r.xadd(_STREAM_KEY, fields, id=f"{ms}-{seq}")
        except Exception:
            r.xadd(_STREAM_KEY, fields)   # fallback: let Redis pick the ID
    except Exception:
        pass   # stream write is best-effort; never crash the scoring path


def get_recent_event_count(
    event_type: str,
    hours: int = 24,
    _now_ts: Optional[float] = None,
) -> int:
    """
    Count occurrences of `event_type` in the Redis stream within the last
    `hours` hours.

    Uses XRANGE with explicit millisecond-epoch boundaries so mock _now_ts
    values (for tests) work correctly.

    Returns 0 if Redis is unavailable — adaptive boost simply won't fire,
    which is the safe degraded behaviour.
    """
    r = _get_redis()
    if r is None:
        return 0
    now    = _now_ts if _now_ts is not None else time.time()
    min_ms = int((now - hours * 3600) * 1000)
    max_ms = int(now * 1000)
    try:
        entries = r.xrange(_STREAM_KEY, min=f"{min_ms}-0", max=f"{max_ms}-9999999")
        return sum(1 for _, fields in entries if fields.get("type") == event_type)
    except Exception:
        return 0


def get_adaptive_weight(
    base_weight: float,
    event_type: str,
    _now_ts: Optional[float] = None,
    _log: bool = True,
) -> tuple[float, str]:
    """
    Return (effective_weight, boost_note) for the given base weight.

    Boost rules (from eliza-config.json biofeedback.adaptive):
      Negative weight — if count in last boost_decay_hours ≥ constraint_repeat_threshold:
        effective = base × constraint_boost_factor   (e.g. -1.0 → -1.5)
      Positive weight — if count in last boost_decay_hours ≤ reward_rare_threshold:
        effective = base × reward_boost_factor       (e.g. 1.0 → 1.2)

    boost_decay_hours acts as the sliding count window.  After that many hours
    without a repeat the count falls to 0 and the weight reverts to base.

    Returns (base_weight, "") when adaptive is disabled or Redis is down.

    _log=False suppresses the INFO log (used by status queries to avoid noise).
    """
    cfg = _get_adaptive_config()
    if not cfg:
        return base_weight, ""

    decay_hours = cfg.get("boost_decay_hours", 48)
    count = get_recent_event_count(event_type, hours=decay_hours, _now_ts=_now_ts)

    if base_weight < 0:
        threshold = cfg.get("constraint_repeat_threshold", 3)
        factor    = cfg.get("constraint_boost_factor", 1.5)
        if count >= threshold:
            effective = base_weight * factor
            note = f"adaptive boost {factor}× applied (count_{decay_hours}h={count} ≥ {threshold})"
            if _log:
                logger.info(
                    "Adaptive boost applied: %s %+.1f → %+.1f (%d repeats in %dh)",
                    event_type, base_weight, effective, count, decay_hours,
                )
            return effective, note
    elif base_weight > 0:
        threshold = cfg.get("reward_rare_threshold", 1)
        factor    = cfg.get("reward_boost_factor", 1.2)
        if count <= threshold:
            effective = base_weight * factor
            note = f"adaptive boost {factor}× applied (count_{decay_hours}h={count} ≤ {threshold})"
            if _log:
                logger.info(
                    "Adaptive boost applied: %s %+.1f → %+.1f (%d repeats in %dh)",
                    event_type, base_weight, effective, count, decay_hours,
                )
            return effective, note

    return base_weight, ""


# ─── Telegram boost alert (once-per-day gate via Redis TTL key) ───────────────

_BOOST_ALERT_KEY_PREFIX = "howell:biofeedback:boost_alert_today"


def _maybe_send_boost_alert(
    event_type: str,
    base_weight: float,
    effective_weight: float,
) -> None:
    """
    Send a Telegram alert on the first boost of the day for this event_type.

    Uses a Redis key with a 24-hour TTL as the once-per-day gate so only one
    alert fires per type per calendar-rolling day regardless of event volume.
    Best-effort — never raises; silent when Redis or webhook is unavailable.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        key = f"{_BOOST_ALERT_KEY_PREFIX}:{event_type}"
        if not r.set(key, "1", ex=86400, nx=True):
            return  # already alerted today for this type
        from notifications import send_telegram_alert  # noqa: PLC0415
        factor = abs(effective_weight / base_weight) if base_weight != 0 else 0
        send_telegram_alert(f"Boost active: {event_type} ×{factor:.2g}")
    except Exception:
        pass


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
    base_weight: float,
    effective_weight: float,
    new_score: float,
    elapsed_sec: float,
    boost_note: str = "",
) -> None:
    """
    Write an audit memory entry to Eliza memory.

    Includes effective_weight and boost_note when an adaptive boost is active.
    Best-effort — DB unavailability must never break the scoring path.
    Lazy import avoids the circular-import risk (biofeedback ← eliza_memory
    ← eliza_db; none of those import biofeedback).
    """
    try:
        import eliza_memory  # noqa: PLC0415  (lazy intentionally)
        elapsed_days = elapsed_sec / 86400
        decay_delta  = decayed_score - pre_decay_score
        boost_part   = f"; {boost_note}" if boost_note else ""
        rationale = (
            f"EWMA decay applied: {decay_delta:+.4f} "
            f"from {elapsed_days:.2f}-day-old score "
            f"(λ·t={DECAY_LAMBDA * elapsed_sec:.4f}); "
            f"event={event_type} base_weight={base_weight:+.1f} "
            f"effective_weight={effective_weight:+.4f}{boost_part}; "
            f"score {pre_decay_score:.4f} → {decayed_score:.4f} → {new_score:.4f}"
        )
        eliza_memory.remember(
            agent   = agent,
            type_   = "BIOFEEDBACK_EWMA",
            content = rationale,
            metadata= {
                "event_type":      event_type,
                "base_weight":     base_weight,
                "effective_weight": effective_weight,
                "boost_note":      boost_note,
                "pre_decay":       round(pre_decay_score, 6),
                "post_decay":      round(decayed_score,   6),
                "new_score":       round(new_score,       6),
                "elapsed_days":    round(elapsed_days,    4),
                "half_life_days":  HALF_LIFE_DAYS,
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

    Adaptive weight logic (when Redis is available):
      Before writing to the stream, get_adaptive_weight() checks the count of
      this event_type over the last boost_decay_hours hours:
        - Negative weight + count ≥ constraint_repeat_threshold → boost ×1.5
        - Positive weight + count ≤ reward_rare_threshold       → boost ×1.2
      The effective (boosted) weight is used in the EWMA update; the base
      weight is stored in the stream entry and the audit log for transparency.

    When use_ewma=true (default):
      1. Compute adaptive weight (queries stream prior event count).
      2. XADD current event to Redis stream (encodes now_ts in stream ID).
      3. Decay the stored score for time elapsed since the last event.
      4. Add the effective weight.
      5. Clamp to [SCORE_FLOOR, +∞].
      6. Write an audit entry to Eliza memory (includes boost note if active).

    When use_ewma=false:
      Increment positive_count or negative_count and return _legacy_score()
      (adaptive weights do not apply in legacy mode).

    Args:
        event_type: Key from EVENT_WEIGHTS (unknown types score 0 and skip audit).
        agent:      Agent name for logging and memory entry.
        _now_ts:    Unix timestamp override (for deterministic tests).
    """
    _ensure_dir()
    base_weight = EVENT_WEIGHTS.get(event_type, 0.0)
    if base_weight == 0.0:
        return get_score()

    now_ts = _now_ts if _now_ts is not None else time.time()

    # ── Adaptive weight (query stream BEFORE this event is added) ─────────────
    effective_weight, boost_note = get_adaptive_weight(base_weight, event_type, _now_ts=_now_ts)

    # ── Optional: Telegram alert on first boost of the day ────────────────────
    if boost_note:
        _maybe_send_boost_alert(event_type, base_weight, effective_weight)

    # ── Append to Redis event stream ──────────────────────────────────────────
    _stream_xadd(event_type, base_weight, now_ts)

    state = _load_ewma()

    # ── Track raw counts for legacy fallback regardless of mode ───────────────
    if base_weight > 0:
        state["positive_count"] = state.get("positive_count", 0) + 1
    else:
        state["negative_count"] = state.get("negative_count", 0) + 1

    state["event_count"] = state.get("event_count", 0) + 1

    if not _use_ewma():
        _save_ewma(state)
        return _legacy_score(state)

    # ── EWMA path (uses effective_weight) ─────────────────────────────────────
    pre_decay_score = state["score"]
    elapsed_sec     = max(0.0, now_ts - state["last_event_ts"]) \
                      if state.get("last_event_ts") is not None else 0.0
    decayed         = _decay(pre_decay_score, state.get("last_event_ts"), now_ts)
    new_score       = max(SCORE_FLOOR, decayed + effective_weight)

    state["score"]         = new_score
    state["last_event_ts"] = now_ts
    _save_ewma(state)

    _audit_remember(
        agent, event_type,
        pre_decay_score, decayed,
        base_weight, effective_weight,
        new_score, elapsed_sec, boost_note,
    )
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


def get_biofeedback_status(_now_ts: Optional[float] = None) -> dict:
    """
    Return a live status snapshot of the biofeedback system.

    Returns:
        {
            "current_score": float,        # EWMA score decayed to right now
            "scale_mode":    str,          # "high" | "normal" | "throttle"
            "recent_counts": {type: int},  # event count per type in last 48h
            "active_boosts": {type: str},  # boost note per currently boosted type
        }

    Scale mode is computed inline (mirrors scaler.py thresholds from config)
    to avoid the circular import that would arise from importing scaler here.
    Adaptive weight queries use _log=False to suppress per-type log noise.
    _now_ts is injectable for deterministic tests; None → time.time().
    """
    _ts = _now_ts if _now_ts is not None else time.time()

    # Compute current score respecting the optional mock timestamp
    if _now_ts is not None and _use_ewma():
        state = _load_ewma()
        score = max(SCORE_FLOOR, _decay(state["score"], state.get("last_event_ts"), _ts))
    else:
        score = get_score()

    cfg        = _load_config()
    thresholds = cfg.get("thresholds", {})
    high_thresh     = thresholds.get("high",     3.0)
    throttle_thresh = thresholds.get("throttle", -2.0)

    if score >= high_thresh:
        scale_mode = "high"
    elif score <= throttle_thresh:
        scale_mode = "throttle"
    else:
        scale_mode = "normal"

    recent_counts: dict[str, int] = {}
    active_boosts: dict[str, str] = {}

    for etype, base in EVENT_WEIGHTS.items():
        recent_counts[etype] = get_recent_event_count(etype, hours=48, _now_ts=_ts)
        eff, note = get_adaptive_weight(base, etype, _now_ts=_ts, _log=False)
        if eff != base:
            active_boosts[etype] = note

    return {
        "current_score": round(score, 4),
        "scale_mode":    scale_mode,
        "recent_counts": recent_counts,
        "active_boosts": active_boosts,
    }


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
