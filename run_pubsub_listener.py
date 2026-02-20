#!/usr/bin/env python3
"""
run_pubsub_listener.py — Real-time order-event subscriber (Redis pub/sub)

Subscribes to the "howell:order_events" channel published by RedisBackend.
On a "paid" event the CS agent reacts instantly — no polling required.

Usage
-----
  Standalone:
      python3 run_pubsub_listener.py

  As a background task inside an async orchestrator:
      from run_pubsub_listener import start_listener_task
      task = await start_listener_task()   # returns asyncio.Task
      # On shutdown:
      task.cancel(); await task

  Via run_agents.py:
      python3 run_agents.py --pubsub-listener

Architecture note
-----------------
This module uses redis.asyncio (non-blocking) for subscribing.  The
sync RedisBackend (redis_backend.py) uses the standard redis client for
all writes.  The two share the same Redis server but different client
instances, which is the correct pattern — pub/sub connections must not
be reused for commands and vice-versa.

Reconnect strategy: exponential back-off with a 64-second cap.  If Redis
is unreachable on startup the listener waits and retries indefinitely,
logging each attempt.  This keeps the process alive through deploys and
transient network issues without spinning.

Shutdown: asyncio.CancelledError is caught, the pubsub and client are
closed cleanly, and the error is re-raised so the caller's await returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pubsub] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pubsub_listener")

# ─── Constants ────────────────────────────────────────────────────────────────

REDIS_URL          = "redis://localhost:6379/0"
_RECONNECT_BASE    = 2    # seconds — first retry wait
_RECONNECT_CAP     = 64   # seconds — max back-off ceiling
_SOCKET_TIMEOUT    = 5.0  # seconds

# Use the class attribute directly — never instantiate a backend just to
# read a constant.  (Grok's draft used RedisBackend().namespace which
# created a throw-away sync connection.)
try:
    from redis_backend import RedisBackend as _RB
    _CHANNEL = f"{_RB.NAMESPACE}order_events"   # "howell:order_events"
    _REDIS_PKG_OK = True
except ImportError:
    _CHANNEL    = "howell:order_events"          # fallback literal
    _REDIS_PKG_OK = False

# ─── CS agent reaction ────────────────────────────────────────────────────────

import eliza_memory
from alerts import send_telegram_alert          # thin wrapper already in repo


def _react_to_paid(order_id: str, data: dict, timestamp: str) -> None:
    """
    Synchronous CS-agent reaction to a confirmed "paid" event.

    Runs inside asyncio via loop.run_in_executor so it never blocks
    the event loop during DB writes or Telegram calls.

    Steps:
      1. Verify the order is actually PAID in Eliza memory (idempotency guard —
         the same event may arrive twice if the publisher retries).
      2. Record an ACTION_RESULT memory entry for audit.
      3. Send a Telegram alert so the shop owner knows immediately.
    """
    order = eliza_memory.get_order(order_id)
    if not order:
        log.warning("pub/sub: received paid event for unknown order %s", order_id)
        return

    if order.get("status") != "PAID":
        # Race condition: DB not yet consistent.  Log and let the next poll catch it.
        log.info("pub/sub: order %s status=%s (not yet PAID) — skipping", order_id, order.get("status"))
        return

    tx_hash = data.get("tx_hash", "N/A")
    dev_tag = " [DEV]" if data.get("dev_mode") else ""

    eliza_memory.remember(
        agent   = "CS_PUBSUB_LISTENER",
        type_   = "PAID_NOTIFICATION",
        content = f"Real-time PAID signal received for order {order_id}{dev_tag} (tx={tx_hash})",
        metadata= {
            "order_id":  order_id,
            "tx_hash":   tx_hash,
            "source":    "redis_pubsub",
            "event_ts":  timestamp,
            "dev_mode":  data.get("dev_mode", False),
        },
    )

    alert_msg = (
        f"⚡ [PAID — real-time] Order {order_id}{dev_tag}\n"
        f"   tx_hash: {tx_hash}\n"
        f"   Kaito confirmed at {timestamp}"
    )
    send_telegram_alert(alert_msg)
    log.info("Reacted to PAID for order %s%s", order_id, dev_tag)


# ─── Core listener ────────────────────────────────────────────────────────────

async def _listen_once(redis_url: str) -> None:
    """
    Connect, subscribe, and process messages until cancelled or disconnected.
    Raises redis.exceptions.ConnectionError / asyncio.CancelledError to the
    caller so the reconnect wrapper can decide what to do.
    """
    from redis.asyncio import Redis                     # lazy — only when needed

    client: Optional[Redis] = None
    pubsub = None
    loop = asyncio.get_running_loop()

    try:
        client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT,
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(_CHANNEL)
        log.info("Subscribed to %s", _CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue                               # skip subscribe confirmations

            try:
                payload = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError):
                log.warning("Malformed message (not JSON): %s", message["data"])
                continue

            event_type = payload.get("type")
            order_id   = payload.get("order_id", "")
            data       = payload.get("data", {})
            timestamp  = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

            log.info("Event: type=%s order=%s", event_type, order_id)

            if event_type == "paid" and order_id:
                # Run sync DB/alert work in a thread so the event loop stays free
                await loop.run_in_executor(
                    None, _react_to_paid, order_id, data, timestamp
                )
            # Future event types (e.g. "refunded", "failed") go here

    except asyncio.CancelledError:
        log.info("Listener cancelled — shutting down cleanly")
        raise                                          # propagate; do not swallow
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(_CHANNEL)
                await pubsub.aclose()
            except Exception:
                pass
        if client:
            try:
                await client.aclose()
            except Exception:
                pass


async def _listener_with_reconnect(redis_url: str = REDIS_URL) -> None:
    """
    Wraps _listen_once with exponential back-off reconnect.

    Exits only when cancelled.  Every other exception (connection refused,
    timeout, protocol error) triggers a logged wait then a fresh attempt.
    """
    delay = _RECONNECT_BASE

    while True:
        try:
            await _listen_once(redis_url)
            # _listen_once returns normally only if the message loop exits
            # without error (e.g. server closed the connection gracefully).
            log.warning("Pub/sub loop exited — reconnecting in %ds", delay)

        except asyncio.CancelledError:
            return                                     # clean shutdown requested

        except Exception as exc:
            log.error("Pub/sub error (%s: %s) — reconnecting in %ds",
                      type(exc).__name__, exc, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, _RECONNECT_CAP)        # exponential back-off, capped


# ─── Public API ───────────────────────────────────────────────────────────────

async def start_listener_task(redis_url: str = REDIS_URL) -> asyncio.Task:
    """
    Launch the listener as a background asyncio Task.

    The task runs until cancelled.  A done-callback logs any unexpected
    exit so it is never a silent failure.

    Usage in an async orchestrator:
        task = await start_listener_task()
        # ... run other coroutines ...
        task.cancel()
        await task
    """
    if not _REDIS_PKG_OK:
        raise ImportError("redis package not installed. Run: pip install redis")

    task = asyncio.create_task(
        _listener_with_reconnect(redis_url),
        name="order_paid_listener",
    )

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.critical("order_paid_listener task died unexpectedly: %s", exc)

    task.add_done_callback(_on_done)
    return task


# ─── Standalone entry point ───────────────────────────────────────────────────

async def _main_async() -> None:
    if not _REDIS_PKG_OK:
        log.error("redis[asyncio] package not installed. Run: pip install redis")
        sys.exit(1)

    log.info("Starting Howell Forge pub/sub listener (channel: %s)", _CHANNEL)
    try:
        await _listener_with_reconnect(REDIS_URL)
    except KeyboardInterrupt:
        log.info("Interrupted — exiting")


if __name__ == "__main__":
    asyncio.run(_main_async())
