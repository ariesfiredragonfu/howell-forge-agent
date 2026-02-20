#!/usr/bin/env python3
"""
Marketing Agent — Layer 1  (Herald edition)
SEO, off-brand checks, and Herald (social post) gate with:
  - VALIDATE_FEATURE pre-post hook (feature must be LIVE)
  - Budget throttling (1 post/day in throttle or healing mode)
  - Entity whitelist enforcement (loaded from eliza-config.json)
"""

import asyncio
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from notifications import send_telegram_alert
import biofeedback

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
BASE_URL = "https://howell-forge.com"
HOST = "howell-forge.com"
CONFIG_PATH = Path(__file__).parent / "eliza-config.json"
SCALE_STATE_PATH = Path.home() / "project_docs" / "biofeedback" / "scale_state.json"

# Off-brand / content-safety blocklist (case-insensitive).
OFF_BRAND_BLOCKLIST = [
    "guaranteed", "100% free", "act now", "limited time", "best in the world",
    "#1 rated", "miracle", "instant results", "risk-free", "exclusive offer",
    "once in a lifetime", "too good to be true", "no risk",
]
OFF_BRAND_BLOCKLIST_PATH = Path.home() / "project_docs" / "howell-forge-off-brand-blocklist.txt"


# ─── Herald config helpers ────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _load_scale_state() -> dict:
    try:
        return json.loads(SCALE_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"mode": "normal", "score": 0.0}


# ─── Herald Budget ────────────────────────────────────────────────────────────

def check_herald_budget() -> dict:
    """
    Check whether Herald is allowed to post (and how many times today).

    Rules:
      - "throttle" mode (biofeedback score ≤ -2) → max 1 post/day (essentials)
      - Active Security Handshake proposals ("Healing") → max 1 post/day
      - Normal → unlimited

    Returns:
        {
          "posts_allowed":  int | None  (None = unlimited)
          "reason":         str
          "throttled":      bool
          "healing_active": bool
        }
    """
    scale = _load_scale_state()
    throttled = scale.get("mode") == "throttle"

    # Check if any security-fixes PRs are open (healing active)
    healing_active = _healing_is_active()

    if throttled or healing_active:
        reason = []
        if throttled:
            reason.append(f"biofeedback throttle (score={scale.get('score', '?')})")
        if healing_active:
            reason.append("Security Handshake healing in progress")
        return {
            "posts_allowed": 1,
            "reason": "; ".join(reason),
            "throttled": throttled,
            "healing_active": healing_active,
        }

    return {
        "posts_allowed": None,
        "reason": "normal operation",
        "throttled": False,
        "healing_active": False,
    }


def _healing_is_active() -> bool:
    """
    Return True if the SecurityContextProvider sees self-healing triggered
    in the last 24 hours.  Imports lazily to avoid circular import issues.
    """
    try:
        from eliza_providers import SecurityContextProvider
        from eliza_memory import get_agent_state
        ctx = SecurityContextProvider().get(get_agent_state(), {"since_minutes": 1440})
        return bool(ctx.get("self_healing_triggered"))
    except Exception:
        return False


# ─── Herald pre-post hook ─────────────────────────────────────────────────────

def validate_post(
    feature_name: str,
    proposed_post_text: str,
    agent: str = "HERALD_AGENT",
) -> dict:
    """
    Pre-post hook — must be called before any X / social post is generated
    or published.

    Runs VALIDATE_FEATURE (feature status + entity whitelist) synchronously.

    Returns:
        {
          "approved":  bool
          "reason":    str
          "data":      dict   (validation details from the action)
        }

    On failure: logs constraint, never raises (caller reads approved=False).
    """
    from eliza_actions import validate_feature, ValidationError
    from eliza_memory import get_agent_state

    state = get_agent_state()
    context = {
        "feature_name": feature_name,
        "proposed_post_text": proposed_post_text,
        "agent": agent,
    }

    async def _run():
        return await validate_feature.handler(state, context)

    try:
        # Use asyncio.get_event_loop().run_until_complete when already inside a
        # running loop (e.g. called from an async test); otherwise asyncio.run().
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                result = future.result(timeout=30)
        else:
            result = asyncio.run(_run())

        return {
            "approved": result.success,
            "reason":   result.message,
            "data":     result.data or {},
        }
    except Exception as exc:
        reason = str(exc)
        return {"approved": False, "reason": reason, "data": {}}


def generate_post(
    feature_name: str,
    draft_text: str,
    agent: str = "HERALD_AGENT",
    dry_run: bool = False,
) -> dict:
    """
    Full Herald post pipeline:
      1. Check budget (throttle / healing detection)
      2. Call validate_post() — VALIDATE_FEATURE pre-post hook
      3. If approved + budget allows → "publish" (log + Telegram)

    Returns result dict with keys: approved, budget, published, reason.
    """
    budget = check_herald_budget()

    if dry_run:
        return {
            "approved": None,
            "budget": budget,
            "published": False,
            "reason": "dry_run",
        }

    validation = validate_post(feature_name, draft_text, agent=agent)

    if not validation["approved"]:
        biofeedback.append_constraint(
            "MARKETING",
            f"Herald post blocked — {validation['reason']}",
            event_type="marketing_fail",
        )
        append_log("HIGH", f"Herald post blocked: {validation['reason']}")
        return {
            "approved": False,
            "budget": budget,
            "published": False,
            "reason": validation["reason"],
        }

    # Post is valid — simulate publish (real X API goes here)
    biofeedback.append_reward(
        "MARKETING",
        f"Herald post approved: '{draft_text[:60]}…'",
        kpi="herald_post",
        event_type="marketing_pass",
    )
    append_log("INFO", f"Herald post approved + published: {draft_text[:120]}")

    return {
        "approved": True,
        "budget": budget,
        "published": True,
        "reason": validation["reason"],
        "data": validation.get("data", {}),
    }


def fetch_homepage() -> tuple[bool, str, str | None]:
    """Fetch homepage HTML. Returns (ok, message, html_or_none)."""
    try:
        req = urllib.request.Request(
            BASE_URL,
            headers={"User-Agent": "Howell-Forge-Marketing/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            return True, "OK", html
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", None
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}", None
    except Exception as e:
        return False, str(e), None


def check_seo(html: str | None = None) -> tuple[bool, list[str]]:
    """
    Check basic SEO elements (title, meta description).
    Returns (ok, list of issues). Pass html to avoid refetching.
    """
    if html is None:
        ok, msg, html = fetch_homepage()
        if not ok:
            return False, [f"Could not fetch site: {msg}"]

    issues = []

    # Title
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE | re.DOTALL)
    if not title_match:
        issues.append("Missing <title> tag")
    else:
        title = title_match.group(1).strip()
        if len(title) < 10:
            issues.append(f"Title too short: '{title}'")
        elif len(title) > 60:
            issues.append(f"Title too long ({len(title)} chars, ideal 50–60)")

    # Meta description
    desc_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not desc_match:
        # Alternate order: content before name
        desc_match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
            html,
            re.IGNORECASE,
        )
    if not desc_match:
        issues.append("Missing meta description")
    else:
        desc = desc_match.group(1).strip()
        if len(desc) < 50:
            issues.append(f"Meta description too short ({len(desc)} chars)")
        elif len(desc) > 160:
            issues.append(f"Meta description too long ({len(desc)} chars, ideal 150–160)")

    return len(issues) == 0, issues


def check_off_brand(html: str) -> tuple[bool, list[str]]:
    """
    Check visible content for off-brand or hallucinated phrases.
    Scans title, meta description. Returns (ok, list of issues).
    """
    text_parts = []
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        text_parts.append(title_match.group(1))
    desc_match = re.search(
        r'<meta[^>]+(?:name=["\']description["\'][^>]+content|content[^>]+name=["\']description["\'])=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not desc_match:
        desc_match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
            html,
            re.IGNORECASE,
        )
    if desc_match:
        text_parts.append(desc_match.group(1))
    combined = " ".join(text_parts).lower()
    blocklist = list(OFF_BRAND_BLOCKLIST)
    if OFF_BRAND_BLOCKLIST_PATH.exists():
        for line in OFF_BRAND_BLOCKLIST_PATH.read_text().splitlines():
            p = line.strip()
            if p and not p.startswith("#"):
                blocklist.append(p)
    issues = []
    for phrase in blocklist:
        if phrase.lower() in combined:
            issues.append(f"Off-brand phrase: '{phrase}'")
    return len(issues) == 0, issues


def append_log(severity: str, message: str) -> None:
    """Append an entry to the log file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [MARKETING] [{severity}]\n{message}\n"
    if LOG_PATH.exists():
        content = LOG_PATH.read_text()
        insert_after = "*Agents append below. Newest at top.*"
        if insert_after in content:
            before, after = content.split(insert_after, 1)
            new_content = before + insert_after + entry + "\n" + after
        else:
            new_content = content + entry
    else:
        new_content = "## Live Log Entries\n\n*Agents append below. Newest at top.*" + entry
    LOG_PATH.write_text(new_content)


def main() -> int:
    ok, msg, html = fetch_homepage()
    if not ok:
        issues = [msg]
        severity = "HIGH"
        append_log(severity, f"SEO check failed — {msg}")
        biofeedback.append_constraint("MARKETING", f"Could not fetch site — {msg}")
        send_telegram_alert(f"📢 [{severity}] Marketing (SEO): {msg}")
        print(f"Marketing: ALERT — {msg} (wrote to log, Telegram sent)")
        return 1

    seo_ok, seo_issues = check_seo(html)
    brand_ok, brand_issues = check_off_brand(html)
    issues = seo_issues + brand_issues
    ok = seo_ok and brand_ok

    if ok:
        biofeedback.append_reward("MARKETING", "SEO + brand check passed", kpi="SEO")
        print(f"Marketing: {BASE_URL} OK (title, meta description, brand safe)")
        return 0

    msg = "; ".join(issues)
    severity = "HIGH"
    append_log(severity, f"Marketing check failed — {msg}")
    biofeedback.append_constraint("MARKETING", f"Marketing check failed — {msg}")
    telegram_msg = f"📢 [{severity}] Marketing (SEO): {msg}"
    send_telegram_alert(telegram_msg)
    print(f"Marketing: ALERT — {msg} (wrote to log, Telegram sent)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
