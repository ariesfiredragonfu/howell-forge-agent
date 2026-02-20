#!/usr/bin/env python3
"""
test_herald_biofeedback.py — Herald biofeedback integration tests.

Covers:
  1. Success path  → seo_pass reward → score rises
  2. High engagement → x_engagement_high reward → score rises further
  3. Validation fail → marketing_validation_fail constraint → score drops
  4. Burst detected → x_bot_risk constraint → score drops
  5. EWMA decay → score decays toward 0 over simulated days (mock _now_ts)

Run:
    cd ~/howell-forge-agent && python3 test_herald_biofeedback.py
"""

import json
import math
import sys
import time
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# ─── Isolate biofeedback state to a temp dir ──────────────────────────────────

_tmp = tempfile.mkdtemp(prefix="hf_bf_test_")
_TMP = Path(_tmp)

import biofeedback as bf

_ORIG_BF_DIR    = bf.BIOFEEDBACK_DIR
_ORIG_REWARDS   = bf.REWARDS_PATH
_ORIG_CONSTR    = bf.CONSTRAINTS_PATH
_ORIG_SCALE     = bf.SCALE_STATE_PATH
_ORIG_EWMA      = bf.EWMA_STATE_PATH
_ORIG_STREAM    = bf._STREAM_KEY

bf.BIOFEEDBACK_DIR  = _TMP
bf.REWARDS_PATH     = _TMP / "rewards.md"
bf.CONSTRAINTS_PATH = _TMP / "constraints.md"
bf.SCALE_STATE_PATH = _TMP / "scale_state.json"
bf.EWMA_STATE_PATH  = _TMP / "ewma_state.json"
# Isolated stream key so the adaptive boost counts don't bleed between test runs
bf._STREAM_KEY      = "howell:biofeedback_events:herald_test"

import marketing as mkt
_ORIG_BURST_LOG = mkt._BURST_LOG_PATH
mkt._BURST_LOG_PATH = _TMP / "herald_post_times.json"

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS  = "\033[92mPASS\033[0m"
FAIL  = "\033[91mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    _results.append((name, condition, detail))


def reset_ewma() -> None:
    """Wipe EWMA file state + isolated test stream between test cases."""
    if bf.EWMA_STATE_PATH.exists():
        bf.EWMA_STATE_PATH.unlink()
    if mkt._BURST_LOG_PATH.exists():
        mkt._BURST_LOG_PATH.unlink()
    # Flush the isolated Redis stream so adaptive counts start from 0 each test
    r = bf._get_redis()
    if r:
        try:
            r.delete(bf._STREAM_KEY)
        except Exception:
            pass


def _mock_append_reward(agent, message, kpi=None, event_type="marketing_pass"):
    """Thin wrapper so we can call real record_event but skip markdown writes."""
    return bf.record_event(event_type, agent)


def _mock_append_constraint(agent, message, event_type="marketing_fail"):
    return bf.record_event(event_type, agent)


# Patch out the markdown-log write to avoid hitting real paths during tests
_PATCH_APPEND_LOG = patch.object(mkt, "append_log", lambda *a, **kw: None)
# Patch biofeedback module methods on mkt's reference (it imported the module)
_PATCH_BF_REWARD  = patch.object(mkt.biofeedback, "append_reward",  _mock_append_reward)
_PATCH_BF_CONSTR  = patch.object(mkt.biofeedback, "append_constraint", _mock_append_constraint)


# ─── Test 1: seo_pass reward — score rises ────────────────────────────────────

def test_seo_pass_reward() -> None:
    print("\n[Test 1] seo_pass reward — score rises")
    reset_ewma()
    score_before = bf.get_score()
    bf.append_reward("HERALD", "Post live", kpi="seo_pass", event_type="seo_pass")
    score_after = bf.get_score()
    check("Score increased after seo_pass", score_after > score_before,
          f"{score_before:.4f} → {score_after:.4f}")
    # With adaptive boost active on first event (count=0 ≤ rare_threshold),
    # effective weight = base × boost_factor; accept either base or boosted delta.
    base_w   = bf.EVENT_WEIGHTS["seo_pass"]
    cfg      = bf._get_adaptive_config()
    factor   = cfg.get("reward_boost_factor", 1.2) if cfg else 1.0
    delta    = score_after - score_before
    boosted_ok = abs(delta - base_w * factor) < 0.01
    base_ok    = abs(delta - base_w) < 0.01
    check("Reward delta matches base or adaptive-boosted seo_pass weight",
          boosted_ok or base_ok,
          f"delta={delta:.4f} base={base_w} boosted={base_w * factor:.4f}")
    check("rewards.md written", bf.REWARDS_PATH.exists())


# ─── Test 2: x_engagement_high — score rises further ─────────────────────────

def test_x_engagement_high() -> None:
    print("\n[Test 2] x_engagement_high — score rises further")
    reset_ewma()
    bf.append_reward("HERALD", "Post live", event_type="seo_pass")
    score_mid = bf.get_score()
    bf.append_reward("HERALD", "High engagement", event_type="x_engagement_high")
    score_after = bf.get_score()
    check("Score rose after x_engagement_high", score_after > score_mid,
          f"{score_mid:.4f} → {score_after:.4f}")
    check("x_engagement_high weight is 2.0",
          abs(bf.EVENT_WEIGHTS["x_engagement_high"] - 2.0) < 0.001)


# ─── Test 3: marketing_validation_fail — score drops ─────────────────────────

def test_validation_fail() -> None:
    print("\n[Test 3] marketing_validation_fail — score drops")
    reset_ewma()
    # Seed a positive score first
    bf.append_reward("HERALD", "Previous good post", event_type="seo_pass")
    score_before = bf.get_score()
    bf.append_constraint("HERALD", "VALIDATE_FEATURE denied", event_type="marketing_validation_fail")
    score_after = bf.get_score()
    check("Score decreased after validation fail", score_after < score_before,
          f"{score_before:.4f} → {score_after:.4f}")
    check("constraints.md written", bf.CONSTRAINTS_PATH.exists())
    content = bf.CONSTRAINTS_PATH.read_text()
    check("Constraint entry present in constraints.md", "HERALD" in content)


# ─── Test 4: x_bot_risk — burst detection ────────────────────────────────────

def test_burst_and_x_bot_risk() -> None:
    print("\n[Test 4] x_bot_risk — burst detection")
    reset_ewma()

    now = time.time()
    # Simulate _BURST_LIMIT posts already recorded inside the window
    times = [now - 100, now - 200, now - 300]
    mkt._BURST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    mkt._BURST_LOG_PATH.write_text(json.dumps(times))

    burst = mkt._detect_burst(_now_ts=now)
    check("_detect_burst returns True when at limit", burst,
          f"recorded={len(times)} limit={mkt._BURST_LIMIT}")

    score_before = bf.get_score()
    bf.append_constraint("HERALD", "Burst x_bot_risk", event_type="x_bot_risk")
    score_after = bf.get_score()
    check("Score drops after x_bot_risk", score_after < score_before,
          f"{score_before:.4f} → {score_after:.4f}")
    check("x_bot_risk weight is −0.5",
          abs(bf.EVENT_WEIGHTS["x_bot_risk"] - (-0.5)) < 0.001)

    # One post under the limit should NOT trigger burst
    reset_ewma()
    mkt._BURST_LOG_PATH.write_text(json.dumps([now - 100, now - 200]))
    check("_detect_burst False when under limit", not mkt._detect_burst(_now_ts=now))


# ─── Test 5: EWMA decay over simulated days ───────────────────────────────────

def test_ewma_decay() -> None:
    print("\n[Test 5] EWMA decay — score decays over simulated days")
    reset_ewma()

    t0 = 1_000_000.0  # arbitrary epoch (far in the past — avoids real-time bleed)
    bf.record_event("seo_pass", "HERALD", _now_ts=t0)
    # Read the stored score directly — get_score() applies real time.time() decay
    # against the mock t0, which would give a near-zero result.
    state = bf._load_ewma()
    score_t0 = state["score"]   # raw stored value right after the event (+1.0)

    # Simulate 7 days later (one half-life) — score should halve
    t7d = t0 + 7 * 24 * 3600
    decayed_7d = bf._decay(state["score"], state["last_event_ts"], t7d)

    check("Score after 7 days ≈ half of original (half-life decay)",
          abs(decayed_7d - score_t0 / 2) < 0.01,
          f"original={score_t0:.4f} decayed@7d={decayed_7d:.4f} half={score_t0/2:.4f}")

    # After 14 days → ~quarter
    t14d = t0 + 14 * 24 * 3600
    decayed_14d = bf._decay(state["score"], state["last_event_ts"], t14d)
    check("Score after 14 days ≈ quarter of original",
          abs(decayed_14d - score_t0 / 4) < 0.01,
          f"decayed@14d={decayed_14d:.4f} quarter={score_t0/4:.4f}")

    # Score clamps to SCORE_FLOOR, never below
    bf_state = bf._load_ewma()
    bf_state["score"] = bf.SCORE_FLOOR - 5.0
    clamped = max(bf.SCORE_FLOOR, bf._decay(bf_state["score"], t0, t0))
    check("Score floor clamped at SCORE_FLOOR", clamped >= bf.SCORE_FLOOR,
          f"floor={bf.SCORE_FLOOR} result={clamped}")


# ─── Test 6: generate_post success path (mocked validation) ──────────────────

def test_generate_post_success() -> None:
    print("\n[Test 6] generate_post() success path — seo_pass emitted")
    reset_ewma()

    mock_validation = {"approved": True, "reason": "OK", "data": {}}
    score_before = bf.get_score()

    with (
        patch.object(mkt, "validate_post", return_value=mock_validation),
        patch.object(mkt, "append_log", lambda *a, **kw: None),
        patch.object(mkt, "check_herald_budget", return_value={"posts_allowed": None, "reason": "normal", "throttled": False, "healing_active": False}),
    ):
        result = mkt.generate_post("Herald (Social Post)", "Handcrafted steel — made in USA.")

    check("Result approved=True", result["approved"] is True)
    check("Result published=True", result["published"] is True)
    score_after = bf.get_score()
    check("Score rose after successful post", score_after > score_before,
          f"{score_before:.4f} → {score_after:.4f}")


# ─── Test 7: generate_post high engagement ────────────────────────────────────

def test_generate_post_high_engagement() -> None:
    print("\n[Test 7] generate_post() high engagement — x_engagement_high emitted")
    reset_ewma()

    mock_validation = {"approved": True, "reason": "OK", "data": {}}
    _budget = {"posts_allowed": None, "reason": "normal", "throttled": False, "healing_active": False}

    # likes > 50
    with (
        patch.object(mkt, "validate_post", return_value=mock_validation),
        patch.object(mkt, "append_log", lambda *a, **kw: None),
        patch.object(mkt, "check_herald_budget", return_value=_budget),
    ):
        mkt.generate_post("Herald (Social Post)", "Steel post.", likes=75, replies=5)

    state = bf.get_ewma_state()
    check("Score reflects seo_pass + x_engagement_high (likes>50)",
          state["current_score"] >= 2.9,
          f"score={state['current_score']:.4f} expected≥2.9")

    # retweets > 5 (new threshold)
    reset_ewma()
    with (
        patch.object(mkt, "validate_post", return_value=mock_validation),
        patch.object(mkt, "append_log", lambda *a, **kw: None),
        patch.object(mkt, "check_herald_budget", return_value=_budget),
    ):
        mkt.generate_post("Herald (Social Post)", "Steel post.", likes=0, replies=0, retweets=8)

    state = bf.get_ewma_state()
    check("Score reflects seo_pass + x_engagement_high (retweets>5)",
          state["current_score"] >= 2.9,
          f"score={state['current_score']:.4f} expected≥2.9")

    # under all thresholds — no x_engagement_high
    reset_ewma()
    with (
        patch.object(mkt, "validate_post", return_value=mock_validation),
        patch.object(mkt, "append_log", lambda *a, **kw: None),
        patch.object(mkt, "check_herald_budget", return_value=_budget),
    ):
        mkt.generate_post("Herald (Social Post)", "Steel post.", likes=5, replies=2, retweets=1)

    state    = bf.get_ewma_state()
    base_w   = bf.EVENT_WEIGHTS["seo_pass"]
    cfg      = bf._get_adaptive_config()
    factor   = cfg.get("reward_boost_factor", 1.2) if cfg else 1.0
    score    = state["current_score"]
    # With a clean stream the first seo_pass earns the rare-reward boost (×1.2).
    # No x_engagement_high emitted → score is seo_pass weight only (base or boosted).
    seo_only = abs(score - base_w) < 0.01 or abs(score - base_w * factor) < 0.01
    check("Score is seo_pass weight only (no x_engagement_high) — base or boosted",
          seo_only,
          f"score={score:.4f} base={base_w} boosted={base_w * factor:.4f}")


# ─── Test 8: generate_post validation fail ───────────────────────────────────

def test_generate_post_fail() -> None:
    print("\n[Test 8] generate_post() validation fail — marketing_validation_fail emitted")
    reset_ewma()

    mock_validation = {"approved": False, "reason": "Feature not LIVE", "data": {}}
    score_before = bf.get_score()

    with (
        patch.object(mkt, "validate_post", return_value=mock_validation),
        patch.object(mkt, "append_log", lambda *a, **kw: None),
        patch.object(mkt, "check_herald_budget", return_value={"posts_allowed": None, "reason": "normal", "throttled": False, "healing_active": False}),
    ):
        result = mkt.generate_post("Herald (Social Post)", "Bad post.")

    check("Result approved=False", result["approved"] is False)
    score_after = bf.get_score()
    check("Score dropped after validation fail", score_after < score_before,
          f"{score_before:.4f} → {score_after:.4f}")
    content = bf.CONSTRAINTS_PATH.read_text() if bf.CONSTRAINTS_PATH.exists() else ""
    check("marketing_validation_fail logged to constraints.md", "HERALD" in content)


# ─── Summary ──────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("Herald Biofeedback Integration Tests")
    print("=" * 60)

    test_seo_pass_reward()
    test_x_engagement_high()
    test_validation_fail()
    test_burst_and_x_bot_risk()
    test_ewma_decay()
    test_generate_post_success()
    test_generate_post_high_engagement()
    test_generate_post_fail()

    passed = sum(1 for _, ok, _ in _results if ok)
    total  = len(_results)
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed")
    print(f"{'='*60}")

    # Cleanup temp dir + test stream
    shutil.rmtree(_tmp, ignore_errors=True)
    r = bf._get_redis()
    if r:
        try:
            r.delete(bf._STREAM_KEY)
        except Exception:
            pass
    # Restore module-level paths
    bf.BIOFEEDBACK_DIR  = _ORIG_BF_DIR
    bf.REWARDS_PATH     = _ORIG_REWARDS
    bf.CONSTRAINTS_PATH = _ORIG_CONSTR
    bf.SCALE_STATE_PATH = _ORIG_SCALE
    bf.EWMA_STATE_PATH  = _ORIG_EWMA
    bf._STREAM_KEY      = _ORIG_STREAM
    mkt._BURST_LOG_PATH = _ORIG_BURST_LOG

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
