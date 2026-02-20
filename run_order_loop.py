#!/usr/bin/env python3
"""
Order Loop — Main orchestrator for the ElizaOS Order Loop (Master Pot).

Runs the async Shop Agent worker pool continuously, pulling from Stripe every
SYNC_INTERVAL seconds and dispatching orders to the Kaito payment flow via the
OrderQueue.  The Customer Service Agent reads the resulting Eliza memory state.

┌──────────────────────────────────────────────────────────────────┐
│                     ElizaOS Order Loop                           │
│                                                                  │
│   Stripe ──► sync_stripe_orders()                               │
│                    │                                             │
│                    ▼                                             │
│             OrderQueue (asyncio)                                 │
│             ┌────┬────┬────┐                                     │
│             │ W1 │ W2 │ W3 │  ← worker coroutines               │
│             └──┬─┴──┬─┴──┬─┘                                    │
│                │    │    │                                       │
│                ▼    ▼    ▼                                       │
│           process_order() × N                                    │
│             │          │                                         │
│    Kaito URI gen    Kaito status poll                            │
│             │          │                                         │
│             ▼          ▼                                         │
│        eliza_memory (SQLite) ◄── Customer Service Agent reads   │
│             │                                                    │
│    security_hooks ──────────────► Monitor Agent log             │
└──────────────────────────────────────────────────────────────────┘

Usage:
  python3 run_order_loop.py              # daemon (loops indefinitely)
  python3 run_order_loop.py --once       # single sync + drain pass
  python3 run_order_loop.py --status     # print Eliza memory state and exit
  python3 run_order_loop.py --workers N  # daemon with N workers (default 3)
"""

import asyncio
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
import shop_agent
from notifications import send_telegram_alert
from order_queue import OrderQueue

# ─── Constants ────────────────────────────────────────────────────────────────

AGENT_NAME = "ORDER_LOOP"
SYNC_INTERVAL_SEC: int = 60      # Stripe sync cadence
DEFAULT_WORKERS: int = 3         # Concurrent order-processor coroutines
LOG_PATH = Path.home() / "project_docs" / "howell-forge-website-log.md"


# ─── Logging ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _append_log(severity: str, message: str) -> None:
    """Write a structured entry to the Monitor Agent's log file."""
    timestamp = _now()
    entry = f"\n## [{timestamp}] [ORDER-LOOP] [{severity}]\n{message}\n"
    if LOG_PATH.exists():
        content = LOG_PATH.read_text()
        marker = "*Agents append below. Newest at top.*"
        if marker in content:
            before, after = content.split(marker, 1)
            LOG_PATH.write_text(before + marker + entry + "\n" + after)
            return
    with open(LOG_PATH, "a") as f:
        f.write(entry)


def _log(msg: str, err: bool = False) -> None:
    target = sys.stderr if err else sys.stdout
    print(f"[{AGENT_NAME}] {msg}", file=target, flush=True)


# ─── Status Printer ───────────────────────────────────────────────────────────

def print_status() -> None:
    """Print current Eliza memory state to stdout."""
    state = eliza_memory.get_agent_state()
    pending = eliza_memory.get_pending_orders()
    memories = eliza_memory.recall(type_="PAYMENT_EVENT", limit=5)
    auth_errors = len([
        e for e in eliza_memory.get_recent_security_events(since_minutes=60)
        if e.get("event_type", "").startswith("AUTH_ERROR_")
    ])
    failed_txs = len([
        e for e in eliza_memory.get_recent_security_events(since_minutes=60)
        if e.get("event_type") == "FAILED_TRANSACTION"
    ])

    print("\n╔══════════════════════════════════════════════════╗")
    print("║           Eliza Memory — Order Loop State        ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Session  : {state.session_id[:16]}…")
    print(f"║  Updated  : {state.last_updated}")
    print(f"║  Tracked  : {len(state.active_orders)} orders in AgentState")
    print(f"║  Pending  : {len(pending)} orders awaiting confirmation")
    print(f"║  Auth errs: {auth_errors} (last 60 min)")
    print(f"║  Failed TX: {failed_txs} (last 60 min)")
    print("╠══════════════════════════════════════════════════╣")
    print("║  Recent Payment Events:                          ║")
    if memories:
        for m in memories:
            ts = m["created_at"][5:16]  # MM-DD HH:MM
            content = m["content"][:48]
            print(f"║    [{ts}] {content}")
    else:
        print("║    (none yet)")
    print("╠══════════════════════════════════════════════════╣")
    print("║  Pending Orders:                                 ║")
    if pending:
        for o in pending:
            print(
                f"║    ⏳ {o['order_id'][:24]:<24} | "
                f"${o.get('amount_usd') or 0:>7.2f}"
            )
    else:
        print("║    (none)")
    print("╚══════════════════════════════════════════════════╝\n")


# ─── Daemon Loop ──────────────────────────────────────────────────────────────

async def daemon_loop(num_workers: int = DEFAULT_WORKERS) -> None:
    """
    Main daemon — runs indefinitely until SIGINT/SIGTERM.

    Cycle:
      1. sync_stripe_orders() — enqueue new orders
      2. Wait SYNC_INTERVAL_SEC (orders are processed concurrently by workers)
      3. Repeat
    """
    queue = OrderQueue(processor=shop_agent.process_order)
    await queue.start(num_workers=num_workers)

    _append_log("INFO", f"Order Loop daemon started (workers={num_workers})")
    send_telegram_alert(
        f"🟢 [ORDER-LOOP] Daemon started — "
        f"ElizaOS + Kaito integration active ({num_workers} workers)"
    )
    biofeedback.append_reward(AGENT_NAME, "Order Loop daemon started", kpi="loop_start")

    _log(
        f"Daemon running. "
        f"Workers: {num_workers} | "
        f"Sync interval: {SYNC_INTERVAL_SEC}s | "
        f"Press Ctrl+C to stop."
    )

    shutdown = asyncio.Event()

    def _handle_signal(sig, _frame) -> None:
        _log(f"Signal {sig} received — shutting down gracefully…")
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    cycle = 0
    try:
        while not shutdown.is_set():
            cycle += 1
            _log(f"Sync cycle #{cycle}")

            try:
                enqueued = await shop_agent.sync_stripe_orders(queue, limit=20)
                if enqueued:
                    _log(f"Enqueued {enqueued} new order(s) from Stripe")
                    _append_log("INFO", f"Cycle #{cycle}: enqueued {enqueued} orders")
            except Exception as exc:
                _log(f"Sync error on cycle #{cycle}: {exc}", err=True)
                _append_log("WARNING", f"Cycle #{cycle} sync error: {exc}")

            # Sleep SYNC_INTERVAL_SEC; wake early if shutdown requested
            try:
                await asyncio.wait_for(
                    asyncio.shield(shutdown.wait()),
                    timeout=SYNC_INTERVAL_SEC,
                )
            except asyncio.TimeoutError:
                pass  # Normal — timeout means next sync cycle

    finally:
        _log(f"Draining queue before exit (stats: {queue.stats})…")
        await queue.stop()
        _append_log(
            "INFO",
            f"Order Loop stopped after {cycle} cycles. "
            f"Stats: {queue.stats}",
        )
        send_telegram_alert(
            f"🔴 [ORDER-LOOP] Daemon stopped. "
            f"Cycles: {cycle} | Processed: {queue.stats['processed']} | "
            f"Failed: {queue.stats['failed']}"
        )


# ─── One-Shot ─────────────────────────────────────────────────────────────────

async def run_once(num_workers: int = DEFAULT_WORKERS) -> int:
    """Single sync + drain pass. Returns count of processed orders."""
    queue = OrderQueue(processor=shop_agent.process_order)
    await queue.start(num_workers=num_workers)
    enqueued = await shop_agent.sync_stripe_orders(queue, limit=20)
    _log(f"One-shot: enqueued {enqueued} order(s)")
    await queue.join()
    await queue.stop()
    return enqueued


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_workers(args: list[str]) -> int:
    try:
        idx = args.index("--workers")
        return max(1, int(args[idx + 1]))
    except (ValueError, IndexError):
        return DEFAULT_WORKERS


def main() -> int:
    args = sys.argv[1:]

    if "--status" in args:
        print_status()
        return 0

    workers = _parse_workers(args)

    if "--once" in args:
        n = asyncio.run(run_once(num_workers=workers))
        print(f"[{AGENT_NAME}] Done. Enqueued {n} order(s).")
        return 0

    # Default: daemon
    asyncio.run(daemon_loop(num_workers=workers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
