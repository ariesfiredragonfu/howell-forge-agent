#!/usr/bin/env python3
"""
telegram_biofeedback_bot.py — Telegram bot that replies to /biofeedback.

Run:
    python3 telegram_biofeedback_bot.py

Stop:
    Ctrl-C  (or kill the process)

Bot token is read from ~/.config/cursor-telegram-bot-token.
Uses Telegram long-polling (getUpdates) — no webhook infrastructure needed.
Read-only: does not modify any biofeedback state.

Responds to:
    /biofeedback   — posts a formatted status snapshot to the same chat
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from biofeedback import get_biofeedback_status
from biofeedback_status import format_status

# ── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN_PATH = Path.home() / ".config" / "cursor-telegram-bot-token"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Telegram API helpers ──────────────────────────────────────────────────────


def _token() -> str:
    """Read the bot token from the config file on every call (supports live rotation)."""
    path = BOT_TOKEN_PATH
    if not path.exists():
        raise FileNotFoundError(f"Bot token not found at {path}")
    return path.read_text().strip()


def _api(method: str, payload: dict | None = None, *, timeout: int = 35) -> dict:
    """
    Make a Telegram Bot API call via HTTPS POST with a JSON body.

    Raises urllib.error.HTTPError / URLError on failure (caller handles retries).
    """
    url  = f"https://api.telegram.org/bot{_token()}/{method}"
    data = json.dumps(payload or {}).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _send(chat_id: int, text: str) -> None:
    """Send a plain-text message to a chat. Best-effort — logs but never raises."""
    try:
        _api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as exc:
        logger.error("Failed to send message to chat_id=%s: %s", chat_id, exc)


def _get_updates(offset: int) -> list[dict]:
    """Long-poll for new updates. Blocks up to 30 s."""
    result = _api(
        "getUpdates",
        {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
        timeout=35,
    )
    return result.get("result", [])


# ── Command handler ───────────────────────────────────────────────────────────


def _handle(msg: dict) -> None:
    """Dispatch incoming messages. Only /biofeedback is handled."""
    text = msg.get("text", "").strip()
    # Accept both "/biofeedback" and "/biofeedback@BotName" (group-chat suffix)
    if not text.split("@")[0] == "/biofeedback":
        return

    chat_id  = msg["chat"]["id"]
    username = msg.get("from", {}).get("username", "?")
    logger.info("/biofeedback requested by @%s in chat %s", username, chat_id)

    try:
        status = get_biofeedback_status()
        reply  = format_status(status, compact=True)
    except Exception as exc:
        logger.exception("Error building status")
        reply = f"Error fetching biofeedback status: {exc}"

    _send(chat_id, reply)


# ── Main loop ─────────────────────────────────────────────────────────────────


def run() -> None:
    logger.info("Biofeedback bot started — polling for /biofeedback commands")
    offset = 0
    retry_delay = 5

    while True:
        try:
            updates = _get_updates(offset)
            retry_delay = 5  # reset back-off on success
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    _handle(update["message"])

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break

        except urllib.error.HTTPError as exc:
            logger.warning("HTTP %s from Telegram API — retrying in %ds", exc.code, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

        except urllib.error.URLError as exc:
            logger.warning("Network error: %s — retrying in %ds", exc.reason, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

        except Exception as exc:
            logger.error("Unexpected error: %s — retrying in %ds", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    run()
