#!/usr/bin/env python3
"""
test_adaptive_weights.py — Adaptive weight EWMA tests.

Covers:
  1.  4× marketing_validation_fail in 20h → weight is -1.5 on the 4th
  2.  1× seo_pass in 24h  → next seo_pass gets 1.2× boost
  3.  Mock 48h forward    → count falls outside window, weights revert to base
  4.  Redis unavailable   → degrades gracefully, base weights used, no crash
  5.  Negative revert     → after events age out of the 48h window, weight = base
  6.  Score delta matches boosted weight, not base weight

Run:
    cd ~/howell-forge-agent && python3 test_adaptive_weights.py
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# ─── Isolate EWMA file state to a temp dir ────────────────────────────────────

_tmp = tempfile.mkdtemp(prefix="hf_adapt_test_")
_TMP = Path(_tmp)

import biofeedback as bf

_ORIG = {
    "BF_DIR":    bf.BIOFEEDBACK_DIR,
    "REWARDS":   bf.REWARDS_PATH,
    "CONSTR":    bf.CONSTRAINTS_PATH,
    "SCALE":     bf.SCALE_STATE_PATH,
    "EWMA":      bf.EWMA_STATE_PATH,
    "REDIS":     bf._redis_client,
    "STREAM_KEY": bf._STREAM_KEY,
}

bf.BIOFEEDBACK_DIR  = _TMP
bf.REWARDS_PATH     = _TMP / "rewards.md"
bf.CONSTRAINTS_PATH = _TMP / "constraints.md"
bf.SCALE_STATE_PATH = _TMP / "scale_state.json"
bf.EWMA_STATE_PATH  = _TMP / "ewma_state.json"

# Use an isolated stream key so tests don't pollute the production stream
_TEST_STREAM = "howell:biofeedback_events:test"
bf._STREAM_KEY = _TEST_STREAM

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    _results.append((name, cond, detail))


def _flush_stream() -> None:
    """Delete the test stream between cases."""
    r = bf._get_redis()
    if r:
        try:
            r.delete(_TEST_STREAM)
        except Exception:
            pass


def reset() -> None:
    """Wipe EWMA file state + stream entries between test cases."""
    if bf.EWMA_STATE_PATH.exists():
        bf.EWMA_STATE_PATH.unlink()
    _flush_stream()
    # Reset cached Redis client so a fresh ping happens
    bf._redis_client = None


def _stream_len() -> int:
    r = bf._get_redis()
    if r is None:
        return 0
    try:
        return r.xlen(_TEST_STREAM)
    except Exception:
        return 0


# ─── T1: 4× constraint in 20h → -1.5 on 4th ─────────────────────────────────

def test_constraint_boost_on_repeat() -> None:
    print("\n[Test 1] 4× marketing_validation_fail in 20h → -1.5 on 4th call")
    reset()

    base = bf.EVENT_WEIGHTS["marketing_validation_fail"]   # -1.0
    cfg  = bf._get_adaptive_config()
    factor    = cfg.get("constraint_boost_factor", 1.5)
    threshold = cfg.get("constraint_repeat_threshold", 3)
    window_h  = cfg.get("boost_decay_hours", 48)

    t0 = 1_700_000_000.0   # fixed epoch; far enough in the past for stream IDs
    hours = [0, 5, 10, 20]

    scores = []
    for i, h in enumerate(hours):
        ts = t0 + h * 3600
        score = bf.record_event("marketing_validation_fail", "HERALD", _now_ts=ts)
        scores.append(score)

    # On the 4th event: 3 previous entries in stream → count=3 ≥ 3 → boost
    eff_4th, note_4th = bf.get_adaptive_weight(base, "marketing_validation_fail", _now_ts=t0 + 20 * 3600)
    check("Effective weight on 4th is base × 1.5",
          abs(eff_4th - base * factor) < 0.001,
          f"base={base} factor={factor} effective={eff_4th:.4f}")
    check("boost_note is non-empty on 4th", bool(note_4th), note_4th)
    check("Stream has 4 entries after 4 events", _stream_len() == 4,
          f"len={_stream_len()}")

    # Verify the 4th event score: previous score decayed over the 10h gap + effective weight
    # (EWMA delta includes decay of the accumulated score, not just the event weight)
    t3 = t0 + 10 * 3600
    t4 = t0 + 20 * 3600
    decayed_into_4 = bf._decay(scores[2], t3, t4)
    expected_s4 = decayed_into_4 + base * factor
    check("Score after 4th event = decayed_score + effective_weight",
          abs(scores[3] - expected_s4) < 0.001,
          f"actual={scores[3]:.4f} expected={expected_s4:.4f}")


# ─── T2: 1× seo_pass in window → 1.2× on next ───────────────────────────────

def test_reward_boost_when_rare() -> None:
    print("\n[Test 2] 1× seo_pass → next call gets 1.2× boost")
    reset()

    base   = bf.EVENT_WEIGHTS["seo_pass"]   # 1.0
    cfg    = bf._get_adaptive_config()
    factor = cfg.get("reward_boost_factor", 1.2)

    t0 = 1_700_000_000.0

    # Before any events: count=0 ≤ 1 → boost applies even on 1st call
    eff_pre, note_pre = bf.get_adaptive_weight(base, "seo_pass", _now_ts=t0)
    check("Boost applies before any events (count=0 ≤ 1)",
          abs(eff_pre - base * factor) < 0.001,
          f"effective={eff_pre:.4f} expected={base * factor:.4f}")

    # Record 1 event
    bf.record_event("seo_pass", "HERALD", _now_ts=t0)

    # After 1 event: count=1 ≤ 1 → still boost on next call
    eff_post, note_post = bf.get_adaptive_weight(base, "seo_pass", _now_ts=t0 + 3600)
    check("Boost still applies after 1 event (count=1 ≤ 1)",
          abs(eff_post - base * factor) < 0.001,
          f"effective={eff_post:.4f}")

    # Record 2nd event — count in window now = 2 > 1 → no more boost
    bf.record_event("seo_pass", "HERALD", _now_ts=t0 + 3600)
    eff_2, _ = bf.get_adaptive_weight(base, "seo_pass", _now_ts=t0 + 7200)
    check("No boost after 2nd event (count=2 > 1)",
          abs(eff_2 - base) < 0.001,
          f"effective={eff_2:.4f} expected base={base}")


# ─── T3: Mock 48h forward → weights revert ───────────────────────────────────

def test_weight_reverts_after_decay_window() -> None:
    print("\n[Test 3] Mock 48h+ forward → weights revert to base")
    reset()

    base = bf.EVENT_WEIGHTS["marketing_validation_fail"]   # -1.0
    cfg  = bf._get_adaptive_config()
    factor    = cfg.get("constraint_boost_factor", 1.5)
    threshold = cfg.get("constraint_repeat_threshold", 3)
    decay_h   = cfg.get("boost_decay_hours", 48)

    t0 = 1_700_000_000.0

    # Seed 4 events within 20h
    for h in [0, 5, 10, 19]:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + h * 3600)

    # Confirm boost is active just after the last event
    eff_active, _ = bf.get_adaptive_weight(base, "marketing_validation_fail",
                                           _now_ts=t0 + 20 * 3600)
    check("Boost active right after seeding (count=4 ≥ 3)",
          abs(eff_active - base * factor) < 0.001,
          f"effective={eff_active:.4f}")

    # Advance time past last event by > boost_decay_hours (events at 0–19h are now > 48h old)
    # Latest event at t0+19h; 49h later = t0+68h → all events outside 48h window
    t_future = t0 + (19 + decay_h + 1) * 3600
    count_future = bf.get_recent_event_count(
        "marketing_validation_fail",
        hours=decay_h,
        _now_ts=t_future,
    )
    eff_reverted, note_reverted = bf.get_adaptive_weight(
        base, "marketing_validation_fail", _now_ts=t_future
    )
    check(f"Count is 0 after {decay_h + 1}h past last event",
          count_future == 0, f"count={count_future}")
    check("Weight reverts to base after decay window",
          abs(eff_reverted - base) < 0.001,
          f"effective={eff_reverted:.4f} base={base}")
    check("boost_note is empty after revert", not note_reverted, note_reverted)


# ─── T4: Redis unavailable → graceful degradation ────────────────────────────

def test_redis_unavailable_degrades_gracefully() -> None:
    print("\n[Test 4] Redis unavailable → base weight used, no crash")
    reset()

    base = bf.EVENT_WEIGHTS["marketing_validation_fail"]

    with patch.object(bf, "_get_redis", return_value=None):
        count = bf.get_recent_event_count("marketing_validation_fail", hours=48)
        eff, note = bf.get_adaptive_weight(base, "marketing_validation_fail")
        # record_event must not raise
        score = bf.record_event("marketing_validation_fail", "HERALD")

    check("Count returns 0 when Redis is down", count == 0, f"count={count}")
    check("Weight is base when Redis is down",
          abs(eff - base) < 0.001, f"effective={eff:.4f}")
    check("No boost_note when Redis is down", not note, note)
    check("record_event returns a float (no crash)", isinstance(score, float),
          f"score={score}")


# ─── T5: Score delta matches boosted weight ───────────────────────────────────

def test_score_delta_matches_effective_weight() -> None:
    print("\n[Test 5] Score delta matches effective (boosted) weight, not base")
    reset()

    base = bf.EVENT_WEIGHTS["marketing_validation_fail"]   # -1.0
    cfg  = bf._get_adaptive_config()
    factor = cfg.get("constraint_boost_factor", 1.5)

    t0 = 1_700_000_000.0

    # Seed 3 events so the 4th gets a boost
    for h in [0, 5, 10]:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + h * 3600)

    score_before = bf._load_ewma()["score"]
    score_after  = bf.record_event("marketing_validation_fail", "HERALD",
                                   _now_ts=t0 + 15 * 3600)

    # Apply same decay manually to compute expected delta
    state_mid    = bf._load_ewma()
    decayed_mid  = bf._decay(score_before, t0 + 10 * 3600, t0 + 15 * 3600)
    expected     = decayed_mid + base * factor

    check("score_after matches decayed + effective_weight",
          abs(score_after - expected) < 0.001,
          f"actual={score_after:.4f} expected={expected:.4f}")
    check("Delta is larger than base weight (boost applied)",
          abs(score_after - score_before) > abs(base),
          f"|delta|={abs(score_after-score_before):.4f} > |base|={abs(base):.4f}")


# ─── T6: Reward boost — score reflects 1.2× ──────────────────────────────────

def test_reward_score_reflects_boost() -> None:
    print("\n[Test 6] First seo_pass score reflects 1.2× boost")
    reset()

    base   = bf.EVENT_WEIGHTS["seo_pass"]   # 1.0
    cfg    = bf._get_adaptive_config()
    factor = cfg.get("reward_boost_factor", 1.2)

    t0    = 1_700_000_000.0
    score = bf.record_event("seo_pass", "HERALD", _now_ts=t0)

    # First event from zero: score = 0 + base*1.2 = 1.2
    check("First seo_pass score = base × boost_factor",
          abs(score - base * factor) < 0.001,
          f"score={score:.4f} expected={base * factor:.4f}")


# ─── T7: Regression — existing EWMA tests still pass ─────────────────────────

def test_regression_existing_ewma() -> None:
    print("\n[Test 7] Regression — basic EWMA still works with adaptive layer")
    reset()

    t0 = 1_700_000_000.0
    # security_pass = +3.0; count=0 initially, but boost only applies to reward
    # when count ≤ 1 → first security_pass gets 1.2× = 3.6
    # After 2nd security_pass count=2 > 1 → base = 3.0

    s1 = bf.record_event("security_pass", "ARIA", _now_ts=t0)
    # count was 0 ≤ 1 → 3.0 × 1.2 = 3.6
    check("1st security_pass with rare boost = 3.6",
          abs(s1 - 3.6) < 0.001, f"score={s1:.4f}")

    s2 = bf.record_event("security_pass", "ARIA", _now_ts=t0 + 3600)
    # count is now 1 ≤ 1 → still boost on 2nd call
    # decayed score: s1 × exp(-λ × 3600)
    decayed = bf._decay(s1, t0, t0 + 3600)
    expected_s2 = decayed + 3.0 * 1.2
    check("2nd security_pass (count=1≤1) still boosted",
          abs(s2 - expected_s2) < 0.01, f"score={s2:.4f} expected≈{expected_s2:.4f}")

    s3 = bf.record_event("security_pass", "ARIA", _now_ts=t0 + 7200)
    # count is now 2 > 1 → no more boost → base weight = 3.0
    decayed2 = bf._decay(s2, t0 + 3600, t0 + 7200)
    expected_s3 = decayed2 + 3.0
    check("3rd security_pass (count=2>1) reverts to base weight",
          abs(s3 - expected_s3) < 0.01, f"score={s3:.4f} expected≈{expected_s3:.4f}")


# ─── T8: logger.info emits boost message on 4th constraint event ─────────────

def test_boost_log_message() -> None:
    print("\n[Test 8] logger.info emits 'Adaptive boost' on 4th constraint event")
    reset()

    t0 = 1_700_000_000.0
    for h in [0, 5, 10]:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + h * 3600)

    with patch.object(bf.logger, "info") as mock_log:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + 20 * 3600)
        boost_logged = any(
            "Adaptive boost" in str(call)
            for call in mock_log.call_args_list
        )
    check("logger.info called with 'Adaptive boost' on 4th event", boost_logged,
          f"calls={[str(c) for c in mock_log.call_args_list]}")


# ─── T9: get_biofeedback_status() shows active boost and count ────────────────

def test_biofeedback_status_shows_boost() -> None:
    print("\n[Test 9] get_biofeedback_status() reflects active boost and recent count")
    reset()

    t0 = 1_700_000_000.0
    for h in [0, 5, 10, 20]:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + h * 3600)

    # Pass _now_ts=1h after last event so the 48h window covers all 4 seeded events
    status = bf.get_biofeedback_status(_now_ts=t0 + 21 * 3600)

    check("current_score is float",
          isinstance(status["current_score"], float), str(status["current_score"]))
    check("scale_mode is valid string",
          status["scale_mode"] in ("high", "normal", "throttle"), status["scale_mode"])
    count_mvf = status["recent_counts"].get("marketing_validation_fail", 0)
    check("recent_counts[marketing_validation_fail] >= 4",
          count_mvf >= 4, f"count={count_mvf}")
    check("active_boosts contains marketing_validation_fail",
          "marketing_validation_fail" in status["active_boosts"],
          str(status["active_boosts"]))


# ─── T10: get_biofeedback_status() clean after 48h decay window ───────────────

def test_biofeedback_status_clean_after_window() -> None:
    print("\n[Test 10] get_biofeedback_status() shows no active boosts after 48h+")
    reset()

    t0 = 1_700_000_000.0
    for h in [0, 5, 10, 20]:
        bf.record_event("marketing_validation_fail", "HERALD", _now_ts=t0 + h * 3600)

    # All events are at most t0+20h old; advance 70h → last event is now 50h old,
    # which is beyond the 48h boost_decay_hours window → count drops to 0.
    t_future = t0 + 70 * 3600

    status = bf.get_biofeedback_status(_now_ts=t_future)

    # The constraint boost on marketing_validation_fail should be gone;
    # reward-type boosts (count=0 ≤ rare threshold) are expected to remain.
    check("marketing_validation_fail NOT in active_boosts after 48h+",
          "marketing_validation_fail" not in status["active_boosts"],
          str(status["active_boosts"]))
    count_mvf = status["recent_counts"].get("marketing_validation_fail", -1)
    check("marketing_validation_fail count is 0 after window",
          count_mvf == 0, f"count={count_mvf}")


# ─── Summary ──────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("Adaptive Weights Tests")
    print("=" * 60)

    test_constraint_boost_on_repeat()
    test_reward_boost_when_rare()
    test_weight_reverts_after_decay_window()
    test_redis_unavailable_degrades_gracefully()
    test_score_delta_matches_effective_weight()
    test_reward_score_reflects_boost()
    test_regression_existing_ewma()
    test_boost_log_message()
    test_biofeedback_status_shows_boost()
    test_biofeedback_status_clean_after_window()

    passed = sum(1 for _, ok, _ in _results if ok)
    total  = len(_results)
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed")
    print(f"{'='*60}")

    # Cleanup
    shutil.rmtree(_tmp, ignore_errors=True)
    _flush_stream()
    for k, v in _ORIG.items():
        if k == "BF_DIR":    bf.BIOFEEDBACK_DIR  = v
        elif k == "REWARDS": bf.REWARDS_PATH     = v
        elif k == "CONSTR":  bf.CONSTRAINTS_PATH = v
        elif k == "SCALE":   bf.SCALE_STATE_PATH = v
        elif k == "EWMA":    bf.EWMA_STATE_PATH   = v
        elif k == "REDIS":   bf._redis_client     = v
        elif k == "STREAM_KEY": bf._STREAM_KEY    = v

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
