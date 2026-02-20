#!/usr/bin/env python3
"""
Fix Proposal Generator — Security Handshake.

Converts a structured fortress_errors.log entry into a human-readable
Fix Proposal that the Security Agent commits to the security-fixes branch.

Each proposal includes:
  - Severity classification
  - Root cause analysis (cross-referenced against the codebase)
  - Diff (if applicable) or step-by-step instructions
  - Files affected
  - Self-heal result (attempted / succeeded / failed)

Known fix patterns:
  AUTH_ERROR_401/403 + kaito/*    → rotate Kaito API key
  AUTH_ERROR_401/403 + stripe/*   → rotate Stripe secret key
  AUTH_ERROR_401/403 + github/*   → rotate GitHub token
  VERIFY_PAYMENT repeated fails   → check Kaito endpoint URL / network
  FAILED_TRANSACTION spike        → inspect order queue + Kaito health
  UNAUTHORIZED_ATTEMPT            → review API surface + rate limits
  IMPORT_ORDER errors             → check Stripe webhook signature
  missing config file             → create config from template
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AGENT_DIR = Path(__file__).parent.resolve()

# ─── Data structures ──────────────────────────────────────────────────────────

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


@dataclass
class FixProposal:
    """
    A structured security fix proposal ready for GitHub PR creation.
    """
    title: str
    severity: str                        # CRITICAL | HIGH | MEDIUM | LOW
    error_type: str
    agent: str
    order_id: Optional[str]
    timestamp: str
    root_cause: str
    files_affected: list[str]
    diff: Optional[str]                  # git diff format, if applicable
    instructions: list[str]              # Step-by-step remediation
    self_heal_attempted: bool = False
    self_heal_result: Optional[str] = None  # "succeeded" | "failed" | "not_applicable"
    raw_entry: dict = field(default_factory=dict)

    def as_markdown(self) -> str:
        """Render the full PR body in markdown."""
        sev_emoji = {
            "CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "🔶", "LOW": "🔷", "INFO": "ℹ️"
        }.get(self.severity, "❓")

        lines = [
            f"# {sev_emoji} Security Fix Proposal — {self.title}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| **Severity** | `{self.severity}` |",
            f"| **Error type** | `{self.error_type}` |",
            f"| **Triggered by agent** | `{self.agent}` |",
            f"| **Order ID** | `{self.order_id or 'N/A'}` |",
            f"| **Detected at** | `{self.timestamp}` |",
            f"| **Generated at** | `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}` |",
            "",
            "## Root Cause",
            "",
            self.root_cause,
            "",
        ]

        if self.files_affected:
            lines += [
                "## Files Affected",
                "",
                *[f"- `{f}`" for f in self.files_affected],
                "",
            ]

        if self.diff:
            lines += [
                "## Proposed Code Change",
                "",
                "```diff",
                self.diff,
                "```",
                "",
            ]

        if self.instructions:
            lines += ["## Remediation Steps", ""]
            for i, step in enumerate(self.instructions, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

        # Self-heal section
        if self.self_heal_attempted:
            icon = "✅" if self.self_heal_result == "succeeded" else "❌"
            lines += [
                "## Self-Healing Attempt",
                "",
                f"{icon} **Result:** {self.self_heal_result or 'unknown'}",
                "",
            ]
            if self.self_heal_result == "succeeded":
                lines.append(
                    "> The Security Agent successfully recovered the secret from the "
                    "local vault or WireGuard-protected remote vault. "
                    "Verify the fix is working and close this PR if no further action needed."
                )
            else:
                lines.append(
                    "> The Security Agent could not automatically recover the secret. "
                    "Manual remediation required. Follow the Remediation Steps above."
                )
            lines.append("")

        # Raw entry for traceability
        lines += [
            "<details>",
            "<summary>Raw fortress_errors.log entry</summary>",
            "",
            "```json",
            json.dumps(self.raw_entry, indent=2),
            "```",
            "",
            "</details>",
        ]

        return "\n".join(lines)

    def as_filename(self) -> str:
        """Safe filename for the fix proposal file."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = self.error_type.lower().replace("_", "-")
        return f"fix-{ts}-{safe}.md"

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "severity": self.severity,
            "error_type": self.error_type,
            "agent": self.agent,
            "order_id": self.order_id,
            "timestamp": self.timestamp,
            "root_cause": self.root_cause,
            "files_affected": self.files_affected,
            "has_diff": self.diff is not None,
            "instructions_count": len(self.instructions),
            "self_heal_attempted": self.self_heal_attempted,
            "self_heal_result": self.self_heal_result,
        }


# ─── Known fix patterns ───────────────────────────────────────────────────────

def build_fix_proposal(
    entry: dict,
    self_heal_attempted: bool = False,
    self_heal_result: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> FixProposal:
    """
    Build a FixProposal from a fortress_errors.log entry dict.
    Cross-references the codebase to identify affected files and generate
    the appropriate diff/instructions.
    """
    error_type: str = entry.get("error_type", "UnknownError")
    action: str     = entry.get("action", "UNKNOWN_ACTION")
    agent: str      = entry.get("agent", "UNKNOWN_AGENT")
    endpoint: str   = entry.get("endpoint") or ""
    error_code: Optional[int] = entry.get("error_code")
    detail: str     = entry.get("detail", "")
    order_id        = entry.get("order_id")
    timestamp       = entry.get("timestamp", datetime.now(timezone.utc).isoformat())

    ctx = extra_context or {}

    # ── AUTH_ERROR_401/403 on Kaito endpoints ─────────────────────────────────
    # Match KaitoAPIError by type (endpoint path may not contain "kaito")
    if error_type == "KaitoAPIError" and error_code in (401, 403):
        return FixProposal(
            title="Rotate Kaito API Key",
            severity="CRITICAL",
            error_type=action,
            agent=agent,
            order_id=order_id,
            timestamp=timestamp,
            root_cause=textwrap.dedent(f"""\
                The Kaito Stablecoin Engine returned HTTP **{error_code}** on endpoint `{endpoint}`.
                This indicates the Kaito API key stored in `~/.config/cursor-kaito-config`
                is **expired, revoked, or missing**.

                Detail from fortress_errors.log: `{detail}`

                Repeated 401/403 errors from `{agent}` indicate this is not a transient
                network issue — the credential itself is invalid.
            """),
            files_affected=[
                "~/.config/cursor-kaito-config",
                "howell-forge-agent/kaito_engine.py",
            ],
            diff=_kaito_key_diff(ctx.get("new_key")),
            instructions=[
                "Log in to your Kaito Finance dashboard (https://app.kaito.finance)",
                "Navigate to **API Keys** → **Rotate Key** or **Create New Key**",
                "Copy the new API key",
                "Update `~/.config/cursor-kaito-config`: set `\"api_key\": \"<new_key>\"`",
                "Set `\"dev_mode\": false` in the same file to re-enable live mode",
                "Restart the Order Loop: `python3 run_order_loop.py`",
                "Monitor `fortress_errors.log` — auth errors should stop within 2 sync cycles",
                "Close this PR once confirmed working",
            ],
            self_heal_attempted=self_heal_attempted,
            self_heal_result=self_heal_result,
            raw_entry=entry,
        )

    # ── AUTH_ERROR_401/403 on Stripe endpoints ────────────────────────────────
    if error_type in ("HTTPError", "KaitoAPIError") and error_code in (401, 403) \
            and "stripe" in endpoint.lower():
        return FixProposal(
            title="Rotate Stripe Secret Key",
            severity="CRITICAL",
            error_type=action,
            agent=agent,
            order_id=order_id,
            timestamp=timestamp,
            root_cause=textwrap.dedent(f"""\
                Stripe returned HTTP **{error_code}** on endpoint `{endpoint}`.
                The Stripe secret key at `~/.config/cursor-stripe-secret-key` is likely
                revoked, rolled, or pointing to the wrong Stripe account (test vs live).

                Detail: `{detail}`
            """),
            files_affected=[
                "~/.config/cursor-stripe-secret-key",
                "howell-forge-agent/shop_agent.py",
                "howell-forge-agent/customer_service.py",
            ],
            diff=None,
            instructions=[
                "Open Stripe Dashboard → Developers → API keys",
                "Verify you are in the correct mode (Test / Live)",
                "Click 'Reveal secret key' or create a restricted key",
                "Overwrite `~/.config/cursor-stripe-secret-key` with the new key (no newline at end)",
                "Re-run `python3 shop_agent.py` to verify Stripe connectivity",
                "Close this PR once confirmed working",
            ],
            self_heal_attempted=self_heal_attempted,
            self_heal_result=self_heal_result,
            raw_entry=entry,
        )

    # ── Repeated VERIFY_PAYMENT failures (non-auth) ───────────────────────────
    if action == "VERIFY_PAYMENT" and error_code not in (401, 403):
        return FixProposal(
            title="Kaito Payment Verification Failures — Check Endpoint & Network",
            severity="HIGH",
            error_type=action,
            agent=agent,
            order_id=order_id,
            timestamp=timestamp,
            root_cause=textwrap.dedent(f"""\
                `VerifyPaymentAction` is repeatedly failing for order `{order_id or 'N/A'}`.
                Error: `{detail}` (code: {error_code or 'N/A'})

                This is NOT an auth error. Likely causes:
                - Kaito API endpoint URL changed (`api_url` in cursor-kaito-config)
                - Kaito service outage or maintenance window
                - Network connectivity issue (check VPN / firewall)
                - Order `{order_id}` has an invalid `kaito_tx_id` in Eliza memory
            """),
            files_affected=[
                "~/.config/cursor-kaito-config",
                "howell-forge-agent/kaito_engine.py",
                "howell-forge-agent/eliza_actions.py",
            ],
            diff=None,
            instructions=[
                f"Check Kaito API status: https://status.kaito.finance",
                "Verify `api_url` in `~/.config/cursor-kaito-config` matches current Kaito docs",
                f"Query the specific order: `python3 customer_service_agent.py order {order_id or '<order_id>'}`",
                "If order is stuck, manually set dev_mode=true in cursor-kaito-config to bypass",
                "Check VPN/firewall — Kaito API may require specific IP allowlist",
                "If the transaction is confirmed on-chain manually, use: "
                "`python3 -c \"import eliza_memory; eliza_memory.upsert_order('<id>', 'PAID')\"` "
                "to force-update state",
            ],
            self_heal_attempted=self_heal_attempted,
            self_heal_result=self_heal_result,
            raw_entry=entry,
        )

    # ── FAILED_TRANSACTION spike ───────────────────────────────────────────────
    if action in ("FAILED_TRANSACTION", "log_failed_transaction"):
        return FixProposal(
            title="Failed Transaction Spike — Order Pipeline Health Check",
            severity="HIGH",
            error_type=action,
            agent=agent,
            order_id=order_id,
            timestamp=timestamp,
            root_cause=textwrap.dedent(f"""\
                Multiple failed transactions detected. Order `{order_id or 'N/A'}` failed:
                `{detail}`

                A spike in failed transactions can indicate:
                - Kaito network congestion or service degradation
                - Incorrect wallet address in cursor-kaito-config
                - Expired payment URIs (customer took too long)
                - Gas fee spike on the configured network (polygon/ethereum)
            """),
            files_affected=[
                "~/.config/cursor-kaito-config",
                "howell-forge-agent/shop_agent.py",
                "howell-forge-agent/order_queue.py",
            ],
            diff=None,
            instructions=[
                "Run `python3 run_order_loop.py --status` to see all pending orders",
                "Run `python3 customer_service_agent.py pending` to list stuck orders",
                "Check the Kaito dashboard for network congestion or gas prices",
                "Verify wallet address: open `~/.config/cursor-kaito-config`, check `wallet_address`",
                "For each stuck order, trigger a manual refresh: "
                "`python3 customer_service_agent.py order <order_id>`",
                "If the issue persists, temporarily increase `PAYMENT_TIMEOUT` in `order_queue.py`",
            ],
            self_heal_attempted=self_heal_attempted,
            self_heal_result=self_heal_result,
            raw_entry=entry,
        )

    # ── UNAUTHORIZED_ATTEMPT (non-auth-error, potential injection) ────────────
    if action == "UNAUTHORIZED_ATTEMPT" or "unauthorized" in detail.lower():
        return FixProposal(
            title="Unauthorized API Attempt — Possible Injection or Replay Attack",
            severity="CRITICAL",
            error_type=action,
            agent=agent,
            order_id=order_id,
            timestamp=timestamp,
            root_cause=textwrap.dedent(f"""\
                An unauthorized API attempt was detected on endpoint `{endpoint}`.
                Agent: `{agent}` | Detail: `{detail}`

                This pattern can indicate:
                - Eliza State injection attempt (malformed order data injected into memory)
                - API token replay from a leaked/compromised credential
                - Internal misconfiguration causing agent-to-agent auth failure
                - Automated credential-stuffing against the Order Loop endpoints
            """),
            files_affected=[
                "howell-forge-agent/security_hooks.py",
                "howell-forge-agent/eliza_memory.py",
                "howell-forge-agent/kaito_engine.py",
            ],
            diff=_eliza_validation_diff(),
            instructions=[
                "IMMEDIATELY: rotate all API keys (Kaito, Stripe, GitHub) as a precaution",
                "Check Eliza memory for corrupted orders: "
                "`python3 -c \"import eliza_memory; print(eliza_memory.recall(limit=20))\"` ",
                "Review fortress_errors.log for the full event cluster",
                "Check `security_hooks.py` AUTH_ERROR_THRESHOLD — consider lowering from 3 to 2",
                "If Eliza State injection suspected: wipe and re-sync the orders table: "
                "`rm ~/.config/howell-forge-eliza.db && python3 run_order_loop.py --once`",
                "Add input validation to `eliza_memory.upsert_order()` for order_id format",
                "Enable WireGuard VPN before re-starting the Order Loop daemon",
            ],
            self_heal_attempted=self_heal_attempted,
            self_heal_result=self_heal_result,
            raw_entry=entry,
        )

    # ── Generic / catch-all fix proposal ──────────────────────────────────────
    return FixProposal(
        title=f"Action Failure: {action} — {error_type}",
        severity="MEDIUM",
        error_type=action,
        agent=agent,
        order_id=order_id,
        timestamp=timestamp,
        root_cause=textwrap.dedent(f"""\
            The ElizaOS Action `{action}` raised an unhandled exception.
            Agent: `{agent}` | Error: `{error_type}` | Code: {error_code or 'N/A'}
            Endpoint: `{endpoint or 'N/A'}`

            Detail: `{detail}`

            This may be a transient network error, a missing dependency,
            or a code-level bug introduced by a recent change.
        """),
        files_affected=[
            f"howell-forge-agent/eliza_actions.py",
            f"howell-forge-agent/shop_agent.py",
        ],
        diff=None,
        instructions=[
            f"Search the codebase for `{action}` to locate the originating Action class",
            "Check `fortress_errors.log` for recurrence patterns",
            f"Reproduce with: `python3 -c \"import eliza_actions; print(dir(eliza_actions))\"` ",
            "If transient: the OrderQueue retry logic will handle it automatically",
            "If persistent: add a try/except in the Action's `_handler()` for this specific case",
        ],
        self_heal_attempted=self_heal_attempted,
        self_heal_result=self_heal_result,
        raw_entry=entry,
    )


# ─── Diff generators ──────────────────────────────────────────────────────────

def _kaito_key_diff(new_key: Optional[str] = None) -> str:
    """Generate a diff showing the Kaito config key update."""
    placeholder = new_key or "<YOUR_NEW_KAITO_API_KEY>"
    return textwrap.dedent(f"""\
        --- a/.config/cursor-kaito-config
        +++ b/.config/cursor-kaito-config
        @@ -1,6 +1,6 @@
         {{
           "api_url": "https://api.kaito.finance/v1",
        -  "api_key": "",
        +  "api_key": "{placeholder}",
           "wallet_address": "0x...",
           "network": "polygon",
        -  "dev_mode": true
        +  "dev_mode": false
         }}
    """)


def _eliza_validation_diff() -> str:
    """Generate a diff adding order_id validation to eliza_memory.upsert_order."""
    return textwrap.dedent("""\
        --- a/howell-forge-agent/eliza_memory.py
        +++ b/howell-forge-agent/eliza_memory.py
        @@ -95,6 +95,12 @@ def upsert_order(
             raw_data: Optional[dict] = None,
         ) -> None:
        +    import re
        +    # Validate order_id format to prevent injection
        +    if not re.match(r'^[\\w\\-]{4,64}$', order_id):
        +        raise ValueError(f"Invalid order_id format: {order_id!r}")
        +    if amount_usd is not None and (amount_usd < 0 or amount_usd > 1_000_000):
        +        raise ValueError(f"amount_usd out of range: {amount_usd}")
        +
             _init_db()
             now = _now()
    """)
