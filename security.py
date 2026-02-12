#!/usr/bin/env python3
"""
Security Agent — Layer 1
Checks security posture (HTTPS redirect, headers). On failure, appends to the log file.
"""

import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
BASE_URL = "https://howell-forge.com"
HOST = "howell-forge.com"


def append_log(severity: str, message: str) -> None:
    """Append an entry to the log file (newest at top)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"\n## [{timestamp}] [SECURITY] [{severity}]\n{message}\n"
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


def check_https_redirect():
    """Returns (ok, message). HTTP should redirect to HTTPS."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        req = urllib.request.Request(
            f"http://{HOST}/",
            headers={"User-Agent": "Howell-Forge-Security/1.0"},
        )
        opener.open(req, timeout=10)
        return False, "HTTP does not redirect to HTTPS (serves over plain HTTP)"
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 307, 308):
            location = e.headers.get("Location", "")
            if location.startswith("https://"):
                return True, "OK (redirects to HTTPS)"
            return False, f"Redirects to non-HTTPS: {location}"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def check_security_headers():
    """Returns (ok, message). Checks for recommended security headers."""
    try:
        req = urllib.request.Request(
            BASE_URL,
            headers={"User-Agent": "Howell-Forge-Security/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
        missing = []
        if "x-content-type-options" not in headers:
            missing.append("X-Content-Type-Options")
        if "x-frame-options" not in headers and "content-security-policy" not in headers:
            missing.append("X-Frame-Options or Content-Security-Policy")
        if missing:
            return False, f"Missing headers: {', '.join(missing)}"
        return True, "OK"
    except Exception as e:
        return False, str(e)


def main() -> int:
    failures = []
    ok, msg = check_https_redirect()
    if not ok:
        failures.append(f"HTTPS redirect: {msg}")
    ok, msg = check_security_headers()
    if not ok:
        failures.append(f"Headers: {msg}")
    if not failures:
        print(f"Security: {BASE_URL} OK (HTTPS redirect, headers)")
        return 0
    severity = "HIGH"
    full_msg = "; ".join(failures)
    append_log(severity, f"Security check failed — {full_msg}")
    print(f"Security: ALERT — {full_msg} (wrote to log)")
    return 1


if __name__ == "__main__":
    exit(main())
