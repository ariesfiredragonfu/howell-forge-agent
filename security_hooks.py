#!/usr/bin/env python3
"""
Security Hooks — 401/403 pattern detection and self-healing protocol trigger.

Any agent that encounters an auth error or failed transaction calls into this
module.  If a pattern of AUTH_ERROR_THRESHOLD or more errors is detected within
AUTH_ERROR_WINDOW_MIN minutes, the Monitor Agent log is updated and a Telegram
alert fires, triggering the self-healing credential-rotation protocol.

This module is the bridge between individual agent errors and the Monitor Agent's
watchdog loop — it writes structured entries that monitor.py and monitor_loop.py
can detect and act on.

Integration points:
  → eliza_memory.log_security_event()   — persists every event to SQLite
  → eliza_memory.count_security_events() — pattern detection query
  → biofeedback.append_constraint()      — feeds the Scaler's penalty signal
  → notifications.send_telegram_alert()  — owner notification
  → LOG_PATH                             — Monitor Agent reads this log file
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
from notifications import send_telegram_alert

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"

# ─── Thresholds ───────────────────────────────────────────────────────────────

AUTH_ERROR_THRESHOLD: int = 3   # N auth errors in the window triggers self-healing
AUTH_ERROR_WINDOW_MIN: int = 30  # Rolling window (minutes)

FAILED_TX_THRESHOLD: int = 5   # N failed transactions in the window triggers alert
FAILED_TX_WINDOW_MIN: int = 60


# ─── Public API ───────────────────────────────────────────────────────────────

def log_auth_error(
    agent: str,
    endpoint: str,
    status_code: int,
    detail: Optional[str] = None,
) -> None:
    """
    Log an HTTP 401 or 403 error from any agent.

    Persists to Eliza memory, feeds biofeedback constraints, then checks if the
    pattern threshold has been crossed.  If so, fires the self-healing protocol.
    """
    # 1. Persist to Eliza memory
    eliza_memory.log_security_event(
        agent=agent,
        event_type=f"AUTH_ERROR_{status_code}",
        endpoint=endpoint,
        status_code=status_code,
        detail=detail,
    )

    # 2. Biofeedback penalty
    biofeedback.append_constraint(
        agent,
        f"Auth error HTTP {status_code} on {endpoint}: {detail or 'no detail'}",
    )

    print(
        f"[SECURITY-HOOKS] {agent} auth error {status_code} on {endpoint}",
        file=sys.stderr,
        flush=True,
    )

    # 3. Pattern check — count ALL auth errors in the window
    auth_count = eliza_memory.count_security_events(
        event_type=None,  # counts AUTH_ERROR_401 + AUTH_ERROR_403
        since_minutes=AUTH_ERROR_WINDOW_MIN,
    )
    # Filter to auth-error types only
    auth_count = sum(
        1 for e in eliza_memory.get_recent_security_events(since_minutes=AUTH_ERROR_WINDOW_MIN)
        if e.get("event_type", "").startswith("AUTH_ERROR_")
    )

    if auth_count >= AUTH_ERROR_THRESHOLD:
        _trigger_self_healing(agent, auth_count, endpoint, status_code)


def log_failed_transaction(
    agent: str,
    order_id: str,
    detail: Optional[str] = None,
) -> None:
    """
    Log a failed transaction to Eliza memory for Monitor Agent pickup.
    Fires an alert if FAILED_TX_THRESHOLD is crossed in FAILED_TX_WINDOW_MIN.
    """
    eliza_memory.log_security_event(
        agent=agent,
        event_type="FAILED_TRANSACTION",
        endpoint=f"order/{order_id}",
        detail=detail,
    )
    biofeedback.append_constraint(
        agent,
        f"Failed transaction {order_id}: {detail or 'unknown reason'}",
    )

    failed_count = sum(
        1 for e in eliza_memory.get_recent_security_events(since_minutes=FAILED_TX_WINDOW_MIN)
        if e.get("event_type") == "FAILED_TRANSACTION"
    )

    if failed_count >= FAILED_TX_THRESHOLD:
        _alert_failed_tx_spike(agent, failed_count)


def log_unauthorized_api_attempt(
    agent: str,
    endpoint: str,
    detail: Optional[str] = None,
) -> None:
    """
    Log any unauthorized API attempt (not just auth errors — also malformed
    tokens, IP blocks, rate-limit 429s, etc.).  Always triggers a biofeedback
    constraint; checks the auth-error threshold as a side-effect.
    """
    eliza_memory.log_security_event(
        agent=agent,
        event_type="UNAUTHORIZED_ATTEMPT",
        endpoint=endpoint,
        status_code=None,
        detail=detail,
    )
    biofeedback.append_constraint(
        agent,
        f"Unauthorized API attempt on {endpoint}: {detail or ''}",
    )
    _append_log(
        "HIGH",
        f"[{agent}] Unauthorized API attempt on {endpoint}: {detail or ''}",
    )


# ─── Internal ─────────────────────────────────────────────────────────────────

def _trigger_self_healing(
    agent: str,
    error_count: int,
    endpoint: str,
    status_code: int,
) -> None:
    """
    Write a SELF-HEALING TRIGGERED entry to the website log (Monitor Agent
    reads this) and send a Telegram alert to the owner.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = (
        f"🚨 [SELF-HEALING TRIGGERED] "
        f"{error_count} auth errors (HTTP {status_code}) detected "
        f"in last {AUTH_ERROR_WINDOW_MIN} min. "
        f"Last endpoint: {endpoint} | Agent: {agent}. "
        f"ACTION REQUIRED: rotate API credentials and verify Kaito/Stripe keys."
    )

    _append_log("EMERGENCY", message)
    send_telegram_alert(message)

    print(
        f"[SECURITY-HOOKS] ⚠️  Self-healing protocol triggered "
        f"({error_count} auth errors / {AUTH_ERROR_WINDOW_MIN} min)",
        file=sys.stderr,
        flush=True,
    )

    # Emit into Eliza memory so Customer Service / Shop Agents can read it
    eliza_memory.remember(
        "SECURITY_HOOKS",
        "SELF_HEALING_TRIGGERED",
        message,
        {
            "error_count": error_count,
            "endpoint": endpoint,
            "status_code": status_code,
            "agent": agent,
        },
    )


def _alert_failed_tx_spike(agent: str, count: int) -> None:
    """Alert when failed transaction count spikes above threshold."""
    message = (
        f"⚠️  [FAILED-TX SPIKE] {count} failed transactions in last "
        f"{FAILED_TX_WINDOW_MIN} min. Agent: {agent}. "
        f"Check Kaito engine and Stripe webhook health."
    )
    _append_log("HIGH", message)
    send_telegram_alert(message)
    print(f"[SECURITY-HOOKS] Failed TX spike: {count} in {FAILED_TX_WINDOW_MIN} min", file=sys.stderr)


def _append_log(severity: str, message: str) -> None:
    """Append a structured entry to the Monitor Agent's log file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [SECURITY-HOOK] [{severity}]\n{message}\n"
    if LOG_PATH.exists():
        content = LOG_PATH.read_text()
        marker = "*Agents append below. Newest at top.*"
        if marker in content:
            before, after = content.split(marker, 1)
            LOG_PATH.write_text(before + marker + entry + "\n" + after)
            return
    # Fallback: just append
    with open(LOG_PATH, "a") as f:
        f.write(entry)
