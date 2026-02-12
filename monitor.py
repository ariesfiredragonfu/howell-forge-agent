#!/usr/bin/env python3
"""
Monitor Agent — Layer 1
Checks site health (uptime, latency, Stripe). On failure, appends to the log file.
"""

import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
STRIPE_KEY_PATH = Path.home() / ".config" / "cursor-stripe-secret-key"
BASE_URL = "https://howell-forge.com"
URLS_TO_CHECK = ["/", "/about", "/contact"]
LATENCY_THRESHOLD_SEC = 5.0  # Alert if response takes longer than this


def check_site(url: str):
    """Returns (ok, message, latency_sec). Latency is None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Howell-Forge-Monitor/1.0"})
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=15) as resp:
            latency = time.perf_counter() - start
            code = resp.getcode()
            if 200 <= code < 300:
                return True, f"OK ({code})", latency
            return False, f"HTTP {code}", latency
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", None
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}", None
    except Exception as e:
        return False, str(e), None


def check_stripe():
    """Returns (ok, message). Skips if key file missing."""
    if not STRIPE_KEY_PATH.exists():
        return True, "skipped (no key)"
    try:
        key = STRIPE_KEY_PATH.read_text().strip()
        if not key:
            return False, "key file empty"
        req = urllib.request.Request(
            "https://api.stripe.com/v1/balance",
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "Howell-Forge-Monitor/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.getcode() < 300:
                return True, "OK"
            return False, f"HTTP {resp.getcode()}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, str(e)


def append_log(severity: str, message: str) -> None:
    """Append an entry to the log file (newest at top)."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [MONITOR] [{severity}]\n{message}\n"
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
    failures = []
    for path in URLS_TO_CHECK:
        url = BASE_URL.rstrip("/") + path
        ok, message, latency = check_site(url)
        if not ok:
            failures.append(f"{path or '/'}: {message}")
        elif latency is not None and latency > LATENCY_THRESHOLD_SEC:
            failures.append(f"{path or '/'}: slow ({latency:.1f}s > {LATENCY_THRESHOLD_SEC}s)")
    ok, msg = check_stripe()
    if not ok:
        failures.append(f"Stripe: {msg}")
    if not failures:
        stripe_status = f", Stripe {msg}" if msg != "skipped (no key)" else ""
        print(f"Monitor: {BASE_URL} OK (all {len(URLS_TO_CHECK)} pages{stripe_status})")
        return 0
    severity = "EMERGENCY" if any("500" in f or "Connection" in f.lower() for f in failures) else "HIGH"
    msg = "; ".join(failures)
    append_log(severity, f"Site check failed — {msg}")
    print(f"Monitor: ALERT — {msg} (wrote to log)")
    return 1


if __name__ == "__main__":
    exit(main())
