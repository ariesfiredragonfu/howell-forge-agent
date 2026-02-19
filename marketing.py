#!/usr/bin/env python3
"""
Marketing Agent — Layer 1
SEO basics, traffic (future), outreach (future). Reports to log on SEO issues.
"""

import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from notifications import send_telegram_alert
import biofeedback

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
BASE_URL = "https://howell-forge.com"
HOST = "howell-forge.com"

# Off-brand / content-safety blocklist (case-insensitive). Avoid marketing hallucinations / spammy copy.
# Extend in OFF_BRAND_BLOCKLIST_PATH if present.
OFF_BRAND_BLOCKLIST = [
    "guaranteed", "100% free", "act now", "limited time", "best in the world",
    "#1 rated", "miracle", "instant results", "risk-free", "exclusive offer",
    "once in a lifetime", "too good to be true", "no risk",
]
OFF_BRAND_BLOCKLIST_PATH = Path.home() / "project_docs" / "howell-forge-off-brand-blocklist.txt"


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
