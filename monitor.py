#!/usr/bin/env python3
"""
Monitor Agent — Layer 1
Checks site health (uptime, latency, Stripe, SSL). On failure, appends to the log file.
"""

import ssl
import socket
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
STRIPE_KEY_PATH = Path.home() / ".config" / "cursor-stripe-secret-key"
BASE_URL = "https://howell-forge.com"
URLS_TO_CHECK = ["/", "/about", "/contact"]
LATENCY_THRESHOLD_SEC = 5.0  # Alert if response takes longer than this
SSL_WARN_DAYS = 14  # Alert if cert expires within this many days


def check_ssl():
    """Returns (ok, message). Checks cert validity and expiration."""
    host = "howell-forge.com"
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=host) as sock:
            sock.settimeout(10)
            sock.connect((host, 443))
            cert = sock.getpeercert()
        if not cert:
            return False, "no cert returned"
        not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        not_after = not_after.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now > not_after:
            return False, f"cert expired {not_after.strftime('%Y-%m-%d')}"
        days_left = (not_after - now).days
        if days_left <= SSL_WARN_DAYS:
            return False, f"cert expires in {days_left} days ({not_after.strftime('%Y-%m-%d')})"
        return True, f"OK (expires {not_after.strftime('%Y-%m-%d')})"
    except ssl.SSLError as e:
        return False, f"SSL error: {e}"
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


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
            headers={"Authorization": f"Bearer {key}", "User-Agent": "Howell-Forge-Monitor/1.0"},
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
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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
    stripe_ok, stripe_msg = check_stripe()
    if not stripe_ok:
        failures.append(f"Stripe: {stripe_msg}")
    ssl_ok, ssl_msg = check_ssl()
    if not ssl_ok:
        failures.append(f"SSL: {ssl_msg}")
    if not failures:
        parts = [f"all {len(URLS_TO_CHECK)} pages"]
        if stripe_msg != "skipped (no key)":
            parts.append(f"Stripe {stripe_msg}")
        parts.append(f"SSL {ssl_msg}")
        print(f"Monitor: {BASE_URL} OK ({', '.join(parts)})")
        return 0
    severity = "EMERGENCY" if any("500" in f or "Connection" in f.lower() for f in failures) else "HIGH"
    msg = "; ".join(failures)
    append_log(severity, f"Site check failed — {msg}")
    print(f"Monitor: ALERT — {msg} (wrote to log)")
    return 1


if __name__ == "__main__":
    exit(main())
