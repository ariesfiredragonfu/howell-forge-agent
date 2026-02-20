#!/usr/bin/env python3
"""
Fortress Watcher — fortress_errors.log tail daemon for the Security Handshake.

Continuously tails fortress_errors.log (like `tail -f`) and classifies each
new JSONL entry.  When a CRITICAL or SECURITY level event is detected it
dispatches the entry to security_agent.handle_event().

Features:
  - File position tracking  → survives log rotation without missing entries
  - Event batching          → groups rapid-fire errors (60s window) to avoid PR spam
  - Deduplication           → suppresses identical events within DEDUP_WINDOW seconds
  - Threshold checking      → only fires handshake when count ≥ DISPATCH_THRESHOLD
  - Graceful SIGTERM        → drains pending batch before exit

Usage:
  python3 fortress_watcher.py               # daemon (loops forever)
  python3 fortress_watcher.py --once        # single pass through log (for cron)
  python3 fortress_watcher.py --replay      # re-process entire log from beginning
  python3 fortress_watcher.py --dry-run     # classify without dispatching

State file: ~/project_docs/.fortress_watcher_state.json
  {"position": 12345, "last_seen": {...}, "dispatched_at": "..."}
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import security_agent

# ─── Config ───────────────────────────────────────────────────────────────────

FORTRESS_LOG      = Path.home() / "project_docs" / "fortress_errors.log"
STATE_FILE        = Path.home() / "project_docs" / ".fortress_watcher_state.json"
WEBSITE_LOG       = Path.home() / "project_docs" / "howell-forge-website-log.md"

POLL_INTERVAL_SEC = 5      # How often to check for new log lines
BATCH_WINDOW_SEC  = 60     # Collect events for N seconds before dispatching
DEDUP_WINDOW_SEC  = 300    # Suppress identical (action+code) events within N seconds
DISPATCH_THRESHOLD = 1     # Min events in batch before triggering handshake

# Event types that always dispatch immediately (no batching)
IMMEDIATE_DISPATCH = frozenset({"UNAUTHORIZED_ATTEMPT", "SELF_HEALING_TRIGGERED"})

# ─── Severity classification ──────────────────────────────────────────────────

def classify_entry(entry: dict) -> str:
    """
    Return the severity level of a fortress_errors.log entry.
    CRITICAL → dispatch to Security Agent
    SECURITY → dispatch to Security Agent
    WARNING  → log only, no handshake
    INFO     → ignore
    """
    error_code  = entry.get("error_code")
    action      = entry.get("action", "")
    error_type  = entry.get("error_type", "")
    detail      = (entry.get("detail") or "").lower()
    endpoint    = (entry.get("endpoint") or "").lower()

    # Auth errors on any endpoint → CRITICAL
    if error_code in (401, 403):
        return "CRITICAL"

    # Unauthorized / injection attempts → CRITICAL
    if "unauthorized" in detail or error_type == "UNAUTHORIZED_ATTEMPT":
        return "CRITICAL"

    # Self-healing was already triggered (upstream) → SECURITY
    if "self_healing" in detail or action == "SELF_HEALING_TRIGGERED":
        return "SECURITY"

    # Payment verification failures → HIGH (maps to WARNING here; 
    # security_agent will escalate if repeat count is high)
    if action in ("VERIFY_PAYMENT", "REFRESH_PAYMENT", "GENERATE_URI"):
        return "HIGH"

    # Failed transactions
    if "failed_transaction" in action.lower() or "failed" in detail:
        return "WARNING"

    return "INFO"


# ─── State management ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"position": 0, "last_seen": {}, "dispatched_at": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Log reader ──────────────────────────────────────────────────────────────

def read_new_entries(state: dict, replay: bool = False) -> list[dict]:
    """
    Read new JSONL entries from fortress_errors.log since the last known position.
    Updates state["position"] in-place.
    """
    if not FORTRESS_LOG.exists():
        return []

    start = 0 if replay else state.get("position", 0)
    entries = []

    try:
        with open(FORTRESS_LOG, "r") as f:
            f.seek(start)
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    pass
            state["position"] = f.tell()
    except OSError:
        pass

    return entries


# ─── Deduplication ────────────────────────────────────────────────────────────

def _dedup_key(entry: dict) -> str:
    return f"{entry.get('action')}:{entry.get('error_code')}:{entry.get('endpoint')}"


def _is_duplicate(entry: dict, last_seen: dict) -> bool:
    key = _dedup_key(entry)
    last_ts_str = last_seen.get(key)
    if not last_ts_str:
        return False
    try:
        last_ts = datetime.fromisoformat(last_ts_str)
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return age < DEDUP_WINDOW_SEC
    except ValueError:
        return False


def _record_seen(entry: dict, last_seen: dict) -> None:
    key = _dedup_key(entry)
    last_seen[key] = datetime.now(timezone.utc).isoformat()


# ─── Dispatch ────────────────────────────────────────────────────────────────

def dispatch_batch(batch: list[dict], dry_run: bool = False) -> None:
    """
    Send a batch of classified entries to the Security Agent.
    Picks the highest-severity entry as the "lead" for the Fix Proposal.
    """
    if not batch:
        return

    sev_order = {"CRITICAL": 0, "SECURITY": 1, "HIGH": 2, "WARNING": 3, "INFO": 4}
    batch.sort(key=lambda e: sev_order.get(e.get("_severity", "INFO"), 4))
    lead = batch[0]

    _log(
        f"Dispatching batch of {len(batch)} events. "
        f"Lead: [{lead.get('_severity')}] {lead.get('action')} "
        f"(code={lead.get('error_code')})"
    )

    if dry_run:
        print(f"[DRY-RUN] Would dispatch to Security Agent:")
        print(f"  Lead entry: {json.dumps(lead, indent=4)}")
        if len(batch) > 1:
            print(f"  + {len(batch) - 1} additional event(s) in batch")
        return

    _append_website_log(
        "HIGH" if lead.get("_severity") == "CRITICAL" else lead.get("_severity", "HIGH"),
        f"Fortress Watcher dispatching batch of {len(batch)} events to Security Agent. "
        f"Lead: [{lead.get('_severity')}] {lead.get('action')} "
        f"code={lead.get('error_code')} agent={lead.get('agent')}",
    )

    try:
        result = security_agent.handle_event(lead)
        if result:
            _log(f"Security Agent result: {result.get('title')} | PR={result.get('pr_url', 'N/A')}")
        else:
            _log("Security Agent: event below threshold (no handshake triggered)")
    except Exception as exc:
        _log(f"Security Agent raised exception: {exc}", err=True)
        # Never let watcher crash because of agent errors
        _append_website_log("EMERGENCY",
                             f"Fortress Watcher: Security Agent crashed: {exc}")


# ─── Main daemon ──────────────────────────────────────────────────────────────

def run_daemon(dry_run: bool = False) -> None:
    """
    Run indefinitely, polling fortress_errors.log every POLL_INTERVAL_SEC seconds.
    Dispatches batches when BATCH_WINDOW_SEC elapses or an IMMEDIATE_DISPATCH entry appears.
    """
    state  = _load_state()
    batch: list[dict] = []
    batch_start: Optional[float] = None
    shutdown = False

    def _handle_signal(sig, _frame):
        nonlocal shutdown
        _log(f"Signal {sig} — draining batch and stopping…")
        shutdown = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _log(f"Daemon started. Watching: {FORTRESS_LOG}")
    _log(f"Poll: {POLL_INTERVAL_SEC}s | Batch window: {BATCH_WINDOW_SEC}s | "
         f"Dedup: {DEDUP_WINDOW_SEC}s | Threshold: {DISPATCH_THRESHOLD}")

    while not shutdown:
        new_entries = read_new_entries(state)
        _save_state(state)

        for entry in new_entries:
            severity = classify_entry(entry)
            entry["_severity"] = severity

            if severity == "INFO":
                continue

            if _is_duplicate(entry, state.get("last_seen", {})):
                _log(f"Dedup: suppressing {entry.get('action')} (seen within {DEDUP_WINDOW_SEC}s)")
                continue

            _record_seen(entry, state.setdefault("last_seen", {}))
            _log(f"New event [{severity}]: {entry.get('action')} "
                 f"code={entry.get('error_code')} agent={entry.get('agent')}")

            # Immediate dispatch for certain event types
            if entry.get("action") in IMMEDIATE_DISPATCH or severity == "CRITICAL":
                _log(f"Immediate dispatch for [{severity}] event")
                batch.append(entry)
                dispatch_batch(batch, dry_run=dry_run)
                batch = []
                batch_start = None
                state["dispatched_at"] = datetime.now(timezone.utc).isoformat()
                _save_state(state)
                continue

            # Accumulate into batch
            if batch_start is None:
                batch_start = time.monotonic()
            batch.append(entry)

        # Flush batch when window expires
        if batch and batch_start is not None:
            elapsed = time.monotonic() - batch_start
            if elapsed >= BATCH_WINDOW_SEC or shutdown:
                if len(batch) >= DISPATCH_THRESHOLD:
                    dispatch_batch(batch, dry_run=dry_run)
                    state["dispatched_at"] = datetime.now(timezone.utc).isoformat()
                    _save_state(state)
                batch = []
                batch_start = None

        if not shutdown:
            time.sleep(POLL_INTERVAL_SEC)

    # Final drain
    if batch:
        _log(f"Final drain: dispatching {len(batch)} pending events")
        dispatch_batch(batch, dry_run=dry_run)

    _log("Watcher stopped.")


def run_once(dry_run: bool = False) -> int:
    """Single pass — read all new entries, dispatch if threshold met. For cron."""
    state   = _load_state()
    entries = read_new_entries(state)
    _save_state(state)

    batch = []
    for entry in entries:
        severity = classify_entry(entry)
        entry["_severity"] = severity
        if severity in ("CRITICAL", "SECURITY", "HIGH"):
            batch.append(entry)

    _log(f"One-shot: {len(entries)} new entries, {len(batch)} actionable")
    if batch:
        dispatch_batch(batch, dry_run=dry_run)
    return 0


def run_replay(dry_run: bool = False) -> int:
    """Re-process entire fortress_errors.log from beginning."""
    state = _load_state()
    state["position"] = 0
    entries = read_new_entries(state, replay=True)
    _save_state(state)

    _log(f"Replay: processing {len(entries)} total entries")
    batch = [
        {**e, "_severity": classify_entry(e)}
        for e in entries
        if classify_entry(e) in ("CRITICAL", "SECURITY", "HIGH")
    ]
    if batch:
        dispatch_batch(batch[:1], dry_run=dry_run)  # Lead entry only on replay
    return 0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str, err: bool = False) -> None:
    target = sys.stderr if err else sys.stdout
    print(f"[FORTRESS-WATCHER] {msg}", file=target, flush=True)


def _append_website_log(severity: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [FORTRESS-WATCHER] [{severity}]\n{message}\n"
    if WEBSITE_LOG.exists():
        content = WEBSITE_LOG.read_text()
        marker = "*Agents append below. Newest at top.*"
        if marker in content:
            before, after = content.split(marker, 1)
            WEBSITE_LOG.write_text(before + marker + entry + "\n" + after)
            return
    with open(WEBSITE_LOG, "a") as f:
        f.write(entry)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Fortress Watcher — fortress_errors.log tail daemon"
    )
    parser.add_argument("--once",    action="store_true", help="Single pass (for cron)")
    parser.add_argument("--replay",  action="store_true", help="Re-process full log")
    parser.add_argument("--dry-run", action="store_true", help="Classify only, no dispatch")
    args = parser.parse_args()

    if args.once:
        return run_once(dry_run=args.dry_run)
    if args.replay:
        return run_replay(dry_run=args.dry_run)

    run_daemon(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
