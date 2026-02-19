#!/usr/bin/env python3
"""
Shared notification helpers — sends messages to the owner via Telegram (Zapier webhook).
Used by: Shop Manager, Monitor, Security.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

WEBHOOK_CONFIG = Path.home() / ".config" / "cursor-zapier-telegram-webhook"


def send_telegram_alert(message: str) -> bool:
    """
    Send a message to the owner via Zapier → Telegram.
    Requires a Zap: Webhooks (Catch Hook) → Telegram (Send Message).
    Webhook URL stored in ~/.config/cursor-zapier-telegram-webhook.
    Returns True if sent, False otherwise (logs to stderr, does not raise).
    """
    if not WEBHOOK_CONFIG.exists():
        return False
    url = WEBHOOK_CONFIG.read_text().strip()
    if not url:
        return False
    payload = {"message": message}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        return False
    except urllib.error.URLError:
        return False
    except Exception:
        return False
