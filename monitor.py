#!/usr/bin/env python3
"""
Monitor Agent — Layer 1
Checks site health. On failure, appends to the log file.
"""

import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"
BASE_URL = "https://howell-forge.com"
URLS_TO_CHECK = ["/", "/about", "/contact"]


def check_site(url: str):
    """Returns (ok, message)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Howell-Forge-Monitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
            if 200 <= code < 300:
                return True, f"OK ({code})"
            return False, f"HTTP {code}"
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
    # Insert after "Live Log Entries" / "*Agents append below*", before the ---
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
        ok, message = check_site(url)
        if not ok:
            failures.append(f"{path or '/'}: {message}")
    if not failures:
        print(f"Monitor: {BASE_URL} OK (all {len(URLS_TO_CHECK)} pages)")
        return 0
    severity = "EMERGENCY" if any("500" in f or "Connection" in f.lower() for f in failures) else "HIGH"
    msg = "; ".join(failures)
    append_log(severity, f"Site check failed — {msg}")
    print(f"Monitor: ALERT — {msg} (wrote to log)")
    return 1


if __name__ == "__main__":
    exit(main())
