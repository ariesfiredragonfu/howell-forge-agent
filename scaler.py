#!/usr/bin/env python3
"""
Scaler — Biofeedback Phase 2 (Grok-Gemini 2026 Resilience Stack).

Reads the EWMA score from biofeedback.get_score() (7-day half-life exponential
decay) instead of counting raw file lines.  This means old rewards/constraints
age out naturally and recent signals dominate.

Scale thresholds (unchanged from original):
    score >= 3   → high    (aggressive deployment, more iterations)
    score in     → normal  (current cadence)
    score <= -2  → throttle (Monitor + Security only, pause experiments)

Exit codes for cron:
    0 = normal
    1 = throttle
    2 = high
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import biofeedback

BIOFEEDBACK_DIR  = Path.home() / "project_docs" / "biofeedback"
SCALE_STATE_PATH = BIOFEEDBACK_DIR / "scale_state.json"

HIGH_THRESHOLD     =  3.0
THROTTLE_THRESHOLD = -2.0


def compute_scale_state() -> dict:
    """Compute scale mode from the live EWMA score and return a state dict."""
    score      = biofeedback.get_score()
    ewma_state = biofeedback.get_ewma_state()

    if score >= HIGH_THRESHOLD:
        mode = "high"
    elif score <= THROTTLE_THRESHOLD:
        mode = "throttle"
    else:
        mode = "normal"

    return {
        "mode":          mode,
        "score":         round(score, 4),
        "event_count":   ewma_state.get("event_count", 0),
        "half_life_days": biofeedback.HALF_LIFE_DAYS,
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "engine":        "ewma",
    }


def main() -> int:
    BIOFEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    state = compute_scale_state()
    SCALE_STATE_PATH.write_text(json.dumps(state, indent=2))
    print(
        f"Scale: {state['mode']} "
        f"(ewma_score={state['score']:.3f}, "
        f"events={state['event_count']}, "
        f"half_life={state['half_life_days']}d)"
    )
    if state["mode"] == "throttle":
        return 1
    if state["mode"] == "high":
        return 2
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
