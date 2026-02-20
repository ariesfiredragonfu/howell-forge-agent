#!/usr/bin/env python3
"""
Break Test — Security Handshake Live Demo
==========================================
1. Backs up ~/.config/cursor-kaito-config
2. Writes a BROKEN config (dev_mode=false, api_key="")
3. Attempts a real Kaito API call → KaitoAPIError (401 or auth failure)
4. Logs the error via eliza_actions.log_action_error → fortress_errors.log
5. Resets watcher state so the new entry is visible
6. Runs fortress_watcher --once → Security Agent → GitHub PR
7. Restores the original config
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
KAITO_CONFIG_PATH = Path.home() / ".config" / "cursor-kaito-config"
KAITO_CONFIG_BAK  = Path.home() / ".config" / "cursor-kaito-config.bak"
WATCHER_STATE     = Path.home() / "project_docs" / ".fortress_watcher_state.json"
FORTRESS_LOG      = Path.home() / "project_docs" / "fortress_errors.log"
BREAK_ORDER_ID    = "pi_break_test_20260220"
BREAK_AGENT       = "SHOP_AGENT"
BROKEN_KEY        = "BROKEN_TEST_KEY_DO_NOT_USE"


def _step(n: int, msg: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  STEP {n}: {msg}")
    print(f"{'─'*60}")


def backup_config() -> dict:
    """Save original config and return its contents."""
    original = json.loads(KAITO_CONFIG_PATH.read_text())
    shutil.copy2(KAITO_CONFIG_PATH, KAITO_CONFIG_BAK)
    print(f"  ✅  Backed up config → {KAITO_CONFIG_BAK}")
    return original


def write_broken_config() -> None:
    """Write a config with dev_mode=false and an invalid api_key."""
    broken = {
        "api_url": "https://api.kaito.finance/v1",
        "api_key": BROKEN_KEY,
        "wallet_address": "0x0000000000000000000000000000000000000000",
        "network": "polygon",
        "dev_mode": False,          # ← force live mode
    }
    KAITO_CONFIG_PATH.write_text(json.dumps(broken, indent=2))
    print(f"  💀  Wrote BROKEN config:")
    print(f"      api_key  = '{BROKEN_KEY}'")
    print(f"      dev_mode = false  (live mode — will hit real API)")


def restore_config(original: dict) -> None:
    KAITO_CONFIG_PATH.write_text(json.dumps(original, indent=2))
    KAITO_CONFIG_BAK.unlink(missing_ok=True)
    print(f"  ✅  Config restored to dev_mode=true")


def advance_watcher_position() -> None:
    """Move watcher position to current EOF so only new entries are picked up."""
    pos = FORTRESS_LOG.stat().st_size if FORTRESS_LOG.exists() else 0
    state = {"position": pos, "last_seen": {}, "dispatched_at": None}
    WATCHER_STATE.write_text(json.dumps(state))
    print(f"  📌  Watcher position reset → byte {pos} (EOF before break)")


def inject_kaito_error() -> None:
    """
    Call kaito_engine.generate_payment_uri with the broken config loaded.
    The engine detects dev_mode=False + non-empty key and fires a real HTTPS
    request.  That request returns HTTP 401 (or a network error that we
    re-raise as KaitoAPIError).  We catch it and route it through
    eliza_actions.log_action_error so fortress_errors.log gets a proper entry.
    """
    import kaito_engine
    import eliza_actions

    # kaito_engine._load_config() reads from disk on every call — no cache to bust.
    print(f"  🔥  Calling kaito_engine.generate_payment_uri with broken key …")
    try:
        kaito_engine.generate_payment_uri(
            order_id=BREAK_ORDER_ID,
            amount_usd=99.00,
            customer_email="break-test@howell-forge.dev",
        )
        # If this somehow succeeds (shouldn't with a broken key), flag it.
        print("  ⚠️  Unexpected success — no error generated. Injecting synthetic 401.")
        _inject_synthetic(eliza_actions)
    except kaito_engine.KaitoAPIError as exc:
        print(f"  💥  KaitoAPIError caught: {exc}")
        eliza_actions.log_action_error(
            action_name="VERIFY_PAYMENT",
            agent=BREAK_AGENT,
            order_id=BREAK_ORDER_ID,
            exc=exc,
            endpoint="/payments/create",
            extra={"break_test": True},
        )
        print(f"  📝  Logged to fortress_errors.log")
    except Exception as exc:
        # Network / SSL / DNS error — also a meaningful "broken key" symptom.
        # Wrap in KaitoAPIError so downstream classifiers handle it correctly.
        print(f"  💥  Network/other error ({type(exc).__name__}): {exc}")
        wrapped = kaito_engine.KaitoAPIError(401, str(exc))
        eliza_actions.log_action_error(
            action_name="VERIFY_PAYMENT",
            agent=BREAK_AGENT,
            order_id=BREAK_ORDER_ID,
            exc=wrapped,
            endpoint="/payments/create",
            extra={"break_test": True, "original_error": type(exc).__name__},
        )
        print(f"  📝  Wrapped as KaitoAPIError 401 → fortress_errors.log")


def _inject_synthetic(eliza_actions) -> None:
    """Fallback: write a realistic 401 entry if the live call somehow passes."""
    import kaito_engine
    exc = kaito_engine.KaitoAPIError(401, f"Kaito API HTTP 401 — broken key: {BROKEN_KEY[:16]}…")
    eliza_actions.log_action_error(
        action_name="VERIFY_PAYMENT",
        agent=BREAK_AGENT,
        order_id=BREAK_ORDER_ID,
        exc=exc,
        endpoint="/payments/create",
        extra={"break_test": True, "synthetic": True},
    )


def run_fortress_watcher() -> int:
    """Invoke fortress_watcher --once and stream its output."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "fortress_watcher.py", "--once"],
        cwd=Path(__file__).parent,
        capture_output=False,  # let output flow to terminal
    )
    return result.returncode


def tail_log(n: int = 3) -> None:
    if not FORTRESS_LOG.exists():
        print("  [fortress_errors.log not found]")
        return
    lines = FORTRESS_LOG.read_text().splitlines()
    print(f"\n  📄  Last {n} lines of fortress_errors.log:")
    for line in lines[-n:]:
        try:
            entry = json.loads(line)
            print(f"    [{entry.get('timestamp','')}] "
                  f"{entry.get('action','')} | {entry.get('error_type','')} "
                  f"| code={entry.get('error_code','?')} "
                  f"| break_test={entry.get('break_test','')}")
        except Exception:
            print(f"    {line}")


def main() -> None:
    print("\n" + "═"*60)
    print("  🔬  BREAK TEST — Kaito API Key Corruption → Security Handshake")
    print("═"*60)

    _step(1, "Backing up ~/.config/cursor-kaito-config")
    original = backup_config()

    _step(2, "Writing BROKEN config (live mode + invalid key)")
    write_broken_config()

    _step(3, "Advancing watcher position to EOF (clean slate for new entry)")
    advance_watcher_position()

    _step(4, "Injecting KaitoAPIError via kaito_engine")
    try:
        inject_kaito_error()
    finally:
        # Restore config immediately after the error is injected.
        _step(5, "Restoring original config")
        restore_config(original)

    tail_log(3)

    _step(6, "Running fortress_watcher --once → Security Agent → GitHub PR")
    rc = run_fortress_watcher()

    print("\n" + "═"*60)
    if rc == 0:
        print("  ✅  Break test PASSED — Security Handshake triggered successfully.")
    else:
        print(f"  ❌  fortress_watcher exited with code {rc}")
    print("═"*60 + "\n")


if __name__ == "__main__":
    main()
