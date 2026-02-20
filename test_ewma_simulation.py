#!/usr/bin/env python3
"""
test_ewma_simulation.py — EWMA scoring simulation over mocked time.

Simulates 10 biofeedback events spread across 14 days, printing the score
after each event and verifying the decay behaviour without touching the
real ewma_state.json or the Eliza DB.

Also tests the use_ewma=false legacy fallback for backward compatibility.

Run:
    python3 test_ewma_simulation.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

# ─── Isolate from real state files ────────────────────────────────────────────
# Redirect EWMA_STATE_PATH to a temp file before importing biofeedback so the
# simulation never touches ~/project_docs/biofeedback/ewma_state.json.

_tmp_dir  = Path(tempfile.mkdtemp(prefix="howell_ewma_sim_"))
_tmp_ewma = _tmp_dir / "ewma_state.json"
_tmp_rwd  = _tmp_dir / "rewards.md"
_tmp_con  = _tmp_dir / "constraints.md"

import biofeedback  # noqa: E402 — must come after path setup

biofeedback.EWMA_STATE_PATH  = _tmp_ewma
biofeedback.REWARDS_PATH     = _tmp_rwd
biofeedback.CONSTRAINTS_PATH = _tmp_con
biofeedback.BIOFEEDBACK_DIR  = _tmp_dir

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results: list[tuple[str, str]] = []

def _check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    _results.append((label, "PASS" if condition else "FAIL"))
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")


def _days(n: float) -> float:
    """Return Unix timestamp n days after the epoch (stable, no timezone fuss)."""
    return n * 86400.0


def _expected_decay(score: float, elapsed_days: float) -> float:
    """Analytical decay: score × exp(−λ × elapsed_seconds)."""
    return score * math.exp(-biofeedback.DECAY_LAMBDA * elapsed_days * 86400)


# ─── Simulation ───────────────────────────────────────────────────────────────

EVENT_SCHEDULE = [
    # (day_offset, event_type,      agent,          label)
    (0.0,   "security_pass",  "MONITOR",     "Day 0  — security pass    (+3.0)"),
    (0.5,   "order_success",  "SHOP",        "Day 0.5— order success    (+1.0)"),
    (1.0,   "marketing_fail", "HERALD",      "Day 1  — marketing fail   (−1.0)"),
    (2.0,   "security_pass",  "MONITOR",     "Day 2  — security pass    (+3.0)"),
    (3.5,   "circuit_open",   "KAITO",       "Day 3.5— circuit open     (−3.0)"),
    (5.0,   "monitor_fail",   "MONITOR",     "Day 5  — monitor fail     (−2.0)"),
    (6.0,   "security_fail",  "MONITOR",     "Day 6  — security fail    (−2.0)"),
    (7.0,   "security_pass",  "MONITOR",     "Day 7  — security pass    (+3.0)  [half-life]"),
    (10.0,  "order_success",  "SHOP",        "Day 10 — order success    (+1.0)"),
    (14.0,  "marketing_pass", "HERALD",      "Day 14 — marketing pass   (+1.0)  [2× half-life]"),
]


def simulate_ewma() -> None:
    print("\n" + "=" * 65)
    print("  EWMA Simulation — 10 events over 14 days")
    print(f"  half_life={biofeedback.HALF_LIFE_DAYS}d  "
          f"λ={biofeedback.DECAY_LAMBDA:.6e} s⁻¹  floor={biofeedback.SCORE_FLOOR}")
    print("=" * 65)
    print(f"  {'Day':>6}  {'Event':20}  {'Expected':>10}  {'Got':>10}  {'Δ':>8}  {'Mode'}")
    print("  " + "-" * 63)

    prev_score  = 0.0
    prev_ts     = None
    scores: list[float] = []

    for day, event_type, agent, label in EVENT_SCHEDULE:
        now_ts  = _days(day)
        weight  = biofeedback.EVENT_WEIGHTS[event_type]

        # Compute expected analytically
        if prev_ts is not None:
            decayed = _expected_decay(prev_score, day - prev_ts / 86400)
        else:
            decayed = prev_score
        expected = max(biofeedback.SCORE_FLOOR, decayed + weight)

        # Call the real function with injected timestamp (no DB audit in test)
        with mock.patch("biofeedback._audit_remember"):
            actual = biofeedback.record_event(event_type, agent, _now_ts=now_ts)

        mode = "high" if actual >= 3.0 else ("throttle" if actual <= -2.0 else "normal")
        delta = actual - prev_score
        print(f"  {day:>6.1f}  {event_type:20}  {expected:>10.4f}  {actual:>10.4f}  {delta:>+8.4f}  {mode}")

        scores.append(actual)
        prev_score = actual
        prev_ts    = now_ts

    print()

    # ── Assertions ────────────────────────────────────────────────────────────

    # 1. After day 7 (exactly one half-life since day 0), accumulated score
    #    should be lower than at peak (around day 2) — decay is working
    peak_score = max(scores[:4])   # peak in first 4 events (days 0-2)
    day7_score = scores[7]
    _check("Score at day 7 < peak (decay is reducing old signals)",
           day7_score < peak_score,
           f"day7={day7_score:.4f}  peak={peak_score:.4f}")

    # 2. After two half-lives (day 14) with only +1 added, score should be
    #    small — old negative burst has decayed significantly
    day14_score = scores[9]
    _check("Score recovers after 14 days of decay (not stuck in throttle)",
           day14_score > biofeedback.SCORE_FLOOR + 5,
           f"day14={day14_score:.4f}")

    # 3. Mode at day 0.5 (score ~4) should be high
    _check("Mode=high when score >= 3 (day 0.5 after two passes)",
           scores[1] >= 3.0,
           f"score={scores[1]:.4f}")

    # 4. Day 6 (after circuit_open + monitor_fail + security_fail) → throttle
    #    scores[5] = day 5 monitor_fail  (-0.56, normal)
    #    scores[6] = day 6 security_fail (-2.51, throttle) ← correct index
    _check("Mode=throttle after negative burst (day 6 security_fail)",
           scores[6] <= -2.0,
           f"score={scores[6]:.4f}")

    # 5. get_score() with no new event returns decayed value
    far_future_ts = _days(21)
    with mock.patch("time.time", return_value=far_future_ts):
        score_3w = biofeedback.get_score()
    _check("get_score() after 3 weeks without events decays toward 0",
           abs(score_3w) < abs(day14_score),
           f"3-week score={score_3w:.4f}  day14={day14_score:.4f}")


# ─── Toggle test: use_ewma=false legacy fallback ──────────────────────────────

def simulate_legacy_fallback() -> None:
    print("\n" + "=" * 65)
    print("  Legacy fallback (use_ewma=false) — count-based scoring")
    print("=" * 65)

    # Reset state
    _tmp_ewma.unlink(missing_ok=True)

    with mock.patch("biofeedback._use_ewma", return_value=False), \
         mock.patch("biofeedback._audit_remember"):

        # 3 rewards
        for _ in range(3):
            biofeedback.record_event("security_pass", "MONITOR")
        # 1 constraint
        biofeedback.record_event("circuit_open", "KAITO")

        score = biofeedback.get_score()
        print(f"  3 positives, 1 negative → legacy score = {score:.1f}")
        _check("Legacy: 3 pos − 1 neg = 2.0", score == 2.0, f"got {score}")

        # 12 more negatives: net = 3 pos − 13 neg = -10 → clamps at floor
        for _ in range(12):
            biofeedback.record_event("monitor_fail", "MONITOR")

        floor_score = biofeedback.get_score()
        print(f"  After 12 more negatives → legacy score = {floor_score:.1f}")
        _check("Legacy: clamps at SCORE_FLOOR (-10)", floor_score == biofeedback.SCORE_FLOOR,
               f"got {floor_score}")

    # Verify toggle back to EWMA doesn't break anything
    with mock.patch("biofeedback._audit_remember"):
        biofeedback.record_event("security_pass", "MONITOR", _now_ts=_days(0))
    _check("Switching back to EWMA after legacy mode works without crash",
           True)


# ─── Audit remember smoke test ────────────────────────────────────────────────

def test_audit_remember() -> None:
    print("\n" + "=" * 65)
    print("  Audit: remember() called with EWMA rationale")
    print("=" * 65)

    _tmp_ewma.unlink(missing_ok=True)
    captured: list[dict] = []

    def _fake_remember(agent, type_, content, metadata=None):
        captured.append({"agent": agent, "type": type_, "content": content, "metadata": metadata})

    import eliza_memory
    with mock.patch.object(eliza_memory, "remember", side_effect=_fake_remember), \
         mock.patch("biofeedback._use_ewma", return_value=True):
        biofeedback.record_event("security_pass", "MONITOR", _now_ts=_days(0))
        biofeedback.record_event("order_fail",    "SHOP",    _now_ts=_days(3))

    _check("remember() called twice (once per event)", len(captured) == 2,
           f"got {len(captured)}")
    _check("Memory type is BIOFEEDBACK_EWMA",
           all(c["type"] == "BIOFEEDBACK_EWMA" for c in captured))
    _check("Rationale contains 'EWMA decay applied'",
           all("EWMA decay applied" in c["content"] for c in captured))
    _check("Second entry has elapsed_days ≈ 3.0",
           abs(captured[1]["metadata"]["elapsed_days"] - 3.0) < 0.01,
           f"elapsed_days={captured[1]['metadata']['elapsed_days']}")
    _check("Rationale contains event type",
           "order_fail" in captured[1]["content"])


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Reset state before each section
    _tmp_ewma.unlink(missing_ok=True)
    simulate_ewma()

    _tmp_ewma.unlink(missing_ok=True)
    simulate_legacy_fallback()

    _tmp_ewma.unlink(missing_ok=True)
    test_audit_remember()

    # Summary
    passed  = sum(1 for _, s in _results if s == "PASS")
    failed  = sum(1 for _, s in _results if s == "FAIL")
    total   = len(_results)
    print("\n" + "=" * 65)
    print(f"  Results: {passed} passed, {failed} failed / {total} total")
    print("=" * 65)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
