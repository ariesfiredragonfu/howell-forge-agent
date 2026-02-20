#!/usr/bin/env python3
"""
Security Agent — The Security Handshake.

Distinct from security.py (which does HTTP/headers checks).
This agent is the "brain" of the Security Handshake triggered by fortress_watcher.py.

Responsibilities:
  1. Receive a classified fortress_errors.log entry from the Watcher
  2. Cross-reference against the codebase to identify affected files
  3. Attempt self-healing for known "Environment Sync" issues via vault_client
  4. Generate a structured Fix Proposal (fix_proposal.py)
  5. Commit the Fix Proposal to the security-fixes branch (github_integration.py)
  6. Open / update a PR with the HitL notice
  7. Ping the Master Pot (Cursor) via Telegram notification

Safety:
  - NEVER auto-merges anything into main
  - NEVER deletes secrets or rewrites production config without human approval
  - All self-healing writes are reversible (old config backed up)

Can also be run as a CLI for manual invocation:
  python3 security_agent.py --analyze <fortress_errors.log entry as JSON>
  python3 security_agent.py --diagnose
  python3 security_agent.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
import fix_proposal as fp_module
import github_integration as gh
import vault_client as vault
from fix_proposal import FixProposal, build_fix_proposal
from notifications import send_telegram_alert

AGENT_NAME   = "SECURITY_AGENT"
AGENT_DIR    = Path(__file__).parent.resolve()
LOG_PATH     = Path.home() / "project_docs" / "howell-forge-website-log.md"
BACKUP_DIR   = Path.home() / ".config" / "howell-forge-config-backups"

# ── "Environment Sync" secret names that trigger self-healing ─────────────────
_ENV_SYNC_MAP: dict[str, str] = {
    # action_pattern → vault secret name
    "kaito":  "kaito_api_key",
    "stripe": "stripe_secret_key",
    "github": "github_token",
}

# ── Error classification thresholds ───────────────────────────────────────────
CRITICAL_ACTIONS = frozenset({
    "VERIFY_PAYMENT", "REFRESH_PAYMENT", "GENERATE_URI",
    "STRIPE_GET", "STRIPE_SYNC", "IMPORT_ORDER",
})
CRITICAL_CODES = frozenset({401, 403})


# ─── Entry point for Watcher ──────────────────────────────────────────────────

def handle_event(entry: dict) -> Optional[dict]:
    """
    Main Security Handshake handler — called by fortress_watcher.py.

    1. Classify the entry
    2. Attempt self-heal for known Environment Sync issues
    3. Build Fix Proposal
    4. Push to GitHub security-fixes branch
    5. Open / update PR (HitL required)
    6. Telegram ping to Master Pot (Cursor)
    7. Log to Eliza memory

    Returns a summary dict, or None on non-actionable events.
    """
    _log(f"Handling event: action={entry.get('action')}, "
         f"type={entry.get('error_type')}, code={entry.get('error_code')}")

    severity = _classify_severity(entry)
    if severity not in ("CRITICAL", "HIGH"):
        _log(f"Severity {severity} — no handshake needed")
        return None

    # ── 1. Backup relevant config files before any write ──────────────────────
    _backup_configs()

    # ── 2. Self-heal: try to resolve "Environment Sync" issues ────────────────
    self_heal_attempted, self_heal_result, extra_ctx = _attempt_self_heal(entry)

    # ── 3. Generate Fix Proposal ──────────────────────────────────────────────
    proposal = build_fix_proposal(
        entry,
        self_heal_attempted=self_heal_attempted,
        self_heal_result=self_heal_result,
        extra_context=extra_ctx,
    )
    _log(f"Fix Proposal: [{proposal.severity}] {proposal.title}")

    # ── 4. GitHub: ensure branch + commit + PR ────────────────────────────────
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None

    try:
        gh.ensure_security_branch()
        commit_result = gh.commit_fix_proposal(
            filename=proposal.as_filename(),
            content=proposal.as_markdown(),
            commit_message=_commit_message(proposal),
        )
        pr_data = gh.ensure_pull_request(
            title=f"[SECURITY-HANDSHAKE] {proposal.title}",
            body=proposal.as_markdown(),
        )
        pr_url    = pr_data.get("html_url")
        pr_number = pr_data.get("number")
        _log(f"PR #{pr_number}: {pr_url}")

    except gh.GitHubError as exc:
        _log(f"GitHub error: {exc}", err=True)
        _append_website_log("HIGH", f"Security Agent: GitHub push failed — {exc}")

    # ── 5. Telegram: ping Master Pot ─────────────────────────────────────────
    _ping_master_pot(proposal, pr_url, pr_number, self_heal_result)

    # ── 6. Eliza memory: log the handshake event ──────────────────────────────
    eliza_memory.log_security_event(
        agent=AGENT_NAME,
        event_type="SECURITY_HANDSHAKE",
        endpoint=entry.get("endpoint"),
        status_code=entry.get("error_code"),
        detail=f"Handshake completed: PR={pr_url or 'N/A'} | self_heal={self_heal_result}",
    )
    eliza_memory.remember(
        AGENT_NAME,
        "SECURITY_HANDSHAKE",
        f"[{proposal.severity}] {proposal.title} → PR #{pr_number or 'N/A'}",
        {
            "proposal_title":   proposal.title,
            "severity":         proposal.severity,
            "pr_url":           pr_url,
            "self_heal_result": self_heal_result,
            "order_id":         entry.get("order_id"),
        },
    )

    if self_heal_result == "succeeded":
        biofeedback.append_reward(
            AGENT_NAME,
            f"Self-healed: {proposal.title}",
            kpi="security_self_heal",
        )
    else:
        biofeedback.append_constraint(
            AGENT_NAME,
            f"Security event requires human review: {proposal.title}",
        )

    summary = {
        "severity":         proposal.severity,
        "title":            proposal.title,
        "self_heal_result": self_heal_result,
        "pr_url":           pr_url,
        "pr_number":        pr_number,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }
    _log(f"Handshake complete: {json.dumps(summary)}")
    return summary


# ─── Classification ───────────────────────────────────────────────────────────

def _classify_severity(entry: dict) -> str:
    """Map fortress_errors.log entry → CRITICAL / HIGH / MEDIUM / LOW."""
    error_code: Optional[int] = entry.get("error_code")
    action: str               = entry.get("action", "")
    error_type: str           = entry.get("error_type", "")
    detail: str               = entry.get("detail", "").lower()

    # Auth errors on critical actions are always CRITICAL
    if error_code in CRITICAL_CODES:
        return "CRITICAL"

    # Unauthorized / injection attempts
    if "unauthorized" in detail or error_type == "UNAUTHORIZED_ATTEMPT":
        return "CRITICAL"

    # Critical action failing without auth error
    if action in CRITICAL_ACTIONS:
        return "HIGH"

    # Transaction failures
    if "failed" in detail or "transaction" in detail.lower():
        return "HIGH"

    return "MEDIUM"


# ─── Self-healing ─────────────────────────────────────────────────────────────

def _attempt_self_heal(entry: dict) -> tuple[bool, Optional[str], dict]:
    """
    Try to resolve "Environment Sync" issues before escalating to human.

    Returns: (attempted, result, extra_context)
      result = "succeeded" | "failed" | "not_applicable"
    """
    endpoint: str      = (entry.get("endpoint") or "").lower()
    error_code         = entry.get("error_code")
    extra_ctx: dict    = {}

    # Only attempt self-heal for auth errors (missing/expired credentials)
    if error_code not in (401, 403):
        return False, "not_applicable", extra_ctx

    # Identify which secret to fetch
    secret_name: Optional[str] = None
    for pattern, vault_key in _ENV_SYNC_MAP.items():
        if pattern in endpoint:
            secret_name = vault_key
            break

    if not secret_name:
        return False, "not_applicable", extra_ctx

    _log(f"Self-heal: attempting to fetch '{secret_name}' from vault…")
    result = vault.fetch_secret(secret_name)

    if not result.found:
        _log(f"Self-heal: '{secret_name}' not found in vault (WG active: {vault.is_wireguard_active()})")
        return True, "failed", extra_ctx

    # Secret found — write it back to the appropriate config file
    _log(f"Self-heal: '{secret_name}' found via {result.source} — writing to config…")
    write_ok = vault.write_secret_to_cursor_config(secret_name, result.value)

    if write_ok:
        extra_ctx["new_key"] = result.value  # For diff generation
        extra_ctx["vault_source"] = result.source
        _log(f"Self-heal: '{secret_name}' written from {result.source} ✓")
        _append_website_log(
            "INFO",
            f"Security Agent self-heal: '{secret_name}' recovered from {result.source} "
            f"and written to config. Monitor for resolution.",
        )
        return True, "succeeded", extra_ctx
    else:
        _log(f"Self-heal: write failed for '{secret_name}'", err=True)
        return True, "failed", extra_ctx


# ─── GitHub helpers ───────────────────────────────────────────────────────────

def _commit_message(proposal: FixProposal) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"[security-handshake] {proposal.severity}: {proposal.title}\n\n"
        f"Auto-generated by Security Agent at {ts}.\n"
        f"Action: {proposal.error_type} | Agent: {proposal.agent}\n"
        f"Self-heal: {proposal.self_heal_result or 'not attempted'}\n\n"
        f"⚠️ HitL required — do not merge without human review."
    )


# ─── Notifications ────────────────────────────────────────────────────────────

def _ping_master_pot(
    proposal: FixProposal,
    pr_url: Optional[str],
    pr_number: Optional[int],
    self_heal_result: Optional[str],
) -> None:
    """Send Telegram notification to Master Pot (Cursor) for HitL review."""
    sev_icon = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🔶"}.get(proposal.severity, "🔷")
    heal_line = ""
    if proposal.self_heal_attempted:
        icon = "✅" if self_heal_result == "succeeded" else "❌"
        heal_line = f"\n{icon} Self-heal: {self_heal_result}"

    pr_line = f"\n🔗 PR #{pr_number}: {pr_url}" if pr_url else "\n❌ PR creation failed — check GitHub"

    msg = (
        f"{sev_icon} [SECURITY HANDSHAKE] {proposal.severity}\n"
        f"Issue: {proposal.title}\n"
        f"Agent: {proposal.agent} | Action: {proposal.error_type}"
        f"{heal_line}"
        f"{pr_line}\n"
        f"⚠️ HitL Required — review in Cursor before merging"
    )
    send_telegram_alert(msg)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _log(msg: str, err: bool = False) -> None:
    target = sys.stderr if err else sys.stdout
    print(f"[{AGENT_NAME}] {msg}", file=target, flush=True)


def _append_website_log(severity: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [SECURITY-AGENT] [{severity}]\n{message}\n"
    if LOG_PATH.exists():
        content = LOG_PATH.read_text()
        marker = "*Agents append below. Newest at top.*"
        if marker in content:
            before, after = content.split(marker, 1)
            LOG_PATH.write_text(before + marker + entry + "\n" + after)
            return
    with open(LOG_PATH, "a") as f:
        f.write(entry)


# ─── Config backup ────────────────────────────────────────────────────────────

def _backup_configs() -> None:
    """Snapshot all cursor config files before any self-heal write."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for name, (path, _) in vault._LOCAL_CONFIG_MAP.items():
        if path.exists():
            dest = BACKUP_DIR / f"{ts}-{path.name}"
            shutil.copy2(path, dest)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _cli_diagnose() -> None:
    """Print environment health report."""
    report = vault.diagnose_environment()
    print("\n=== Environment Diagnosis ===")
    print(f"WireGuard active  : {report['wireguard_active']}")
    print(f"Local vault exists: {report['vault_dir_exists']}")
    print(f"Remote vault cfg  : {report['remote_vault_configured']}")
    print("\nSecret Health:")
    for name, info in report["secrets"].items():
        status = "✓" if info["found"] else "✗ MISSING"
        print(f"  {status}  {name:<25}  source={info['source']}")

    print("\nRecent Handshake Events (Eliza memory):")
    events = eliza_memory.recall(type_="SECURITY_HANDSHAKE", limit=5)
    for e in events:
        print(f"  [{e['created_at']}] {e['content']}")

    print("\nFortress Error Count (last 60 min):")
    from eliza_actions import count_fortress_errors
    n = count_fortress_errors(since_minutes=60)
    print(f"  {n} error(s)")


def _cli_analyze(raw_entry: str) -> None:
    """Analyze a specific log entry from CLI."""
    try:
        entry = json.loads(raw_entry)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    result = handle_event(entry)
    if result:
        print(f"\nHandshake result:\n{json.dumps(result, indent=2)}")
    else:
        print("Event did not meet threshold for handshake.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Security Handshake Agent")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--diagnose", action="store_true",
                     help="Print environment health and recent handshake events")
    grp.add_argument("--analyze", metavar="JSON",
                     help="Analyze a fortress_errors.log entry (JSON string)")
    grp.add_argument("--status", action="store_true",
                     help="Show recent Eliza security events and fortress log tail")
    args = parser.parse_args()

    if args.diagnose:
        _cli_diagnose()
        return 0

    if args.analyze:
        _cli_analyze(args.analyze)
        return 0

    if args.status:
        from eliza_actions import tail_fortress_log
        entries = tail_fortress_log(lines=10)
        print(f"\nLast {len(entries)} fortress_errors.log entries:")
        for e in entries:
            print(f"  [{e.get('timestamp')}] {e.get('action')} | "
                  f"{e.get('agent')} | code={e.get('error_code')} | {e.get('detail', '')[:60]}")
        events = eliza_memory.get_recent_security_events(since_minutes=120, limit=5)
        print(f"\nRecent security events (Eliza memory):")
        for e in events:
            print(f"  [{e['created_at']}] [{e['event_type']}] {e['agent']}: {e.get('detail', '')[:80]}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
