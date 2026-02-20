#!/usr/bin/env python3
"""
Async Order Queue — asyncio-based worker pool for the Order Loop.

Replaces synchronous call chains with a non-blocking queue so the system
can handle multiple simultaneous customers without blocking the "Master Pot"
loop.  No external dependencies (BullMQ is Node.js); this is a pure-Python
asyncio equivalent.

Architecture:
  OrderItem   → OrderQueue.enqueue()
              → asyncio.PriorityQueue
              → Worker pool (N concurrent coroutines)
              → processor(OrderItem)   ← injected at construction

Retry policy: exponential back-off up to RETRY_MAX attempts.
Priority:     HIGH (0) > NORMAL (1) > LOW (2)
"""

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Awaitable, Callable, Optional

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_WORKERS: int = 5          # Concurrent order processor coroutines
RETRY_MAX: int = 3            # Max retry attempts per order before giving up
PAYMENT_POLL_INTERVAL: int = 10   # Seconds between blockchain status polls
PAYMENT_TIMEOUT: int = 3600   # Max seconds to wait for on-chain confirmation (1 h)


# ─── Data Structures ──────────────────────────────────────────────────────────

class OrderPriority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2


@dataclass(order=True)
class OrderItem:
    """
    A single unit of work for the Order Queue.

    `order=True` on the dataclass makes PriorityQueue sort by field order;
    `priority` must be first so HIGH items dequeue first.
    All business fields use `field(compare=False)` to avoid confusing sorts.
    """
    priority: OrderPriority = field(default=OrderPriority.NORMAL)

    # Business fields — not compared
    order_id: str = field(compare=False, default="")
    customer_email: str = field(compare=False, default="")
    amount_usd: float = field(compare=False, default=0.0)
    metadata: dict = field(compare=False, default_factory=dict)
    retry_count: int = field(compare=False, default=0)
    created_at: str = field(
        compare=False,
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    def __repr__(self) -> str:
        return (
            f"OrderItem(id={self.order_id!r}, "
            f"${self.amount_usd:.2f}, "
            f"priority={self.priority.name}, "
            f"retry={self.retry_count})"
        )


# ─── Queue ────────────────────────────────────────────────────────────────────

ProcessorFn = Callable[[OrderItem], Awaitable[None]]


class OrderQueue:
    """
    Asyncio-based priority order queue with a configurable worker pool.

    Usage:
        async def my_processor(item: OrderItem) -> None:
            ...

        q = OrderQueue(processor=my_processor)
        await q.start(num_workers=3)
        await q.enqueue(OrderItem(order_id="ord_123", amount_usd=49.99))
        await q.join()     # wait for all queued items to finish
        await q.stop()
    """

    def __init__(self, processor: ProcessorFn) -> None:
        self._processor = processor
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._workers: list[asyncio.Task] = []
        self._running: bool = False
        self._processed: int = 0
        self._failed: int = 0
        self._retried: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, num_workers: int = MAX_WORKERS) -> None:
        """Spawn the worker coroutines."""
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(
                self._worker(i), name=f"order-worker-{i}"
            )
            for i in range(num_workers)
        ]
        print(f"[OrderQueue] Started {num_workers} workers", flush=True)

    async def stop(self) -> None:
        """
        Gracefully drain the queue and stop all workers.
        Sends a poison-pill None item per worker to unblock blocked gets.
        """
        self._running = False
        for _ in self._workers:
            await self._queue.put((OrderPriority.LOW, None))
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        print(
            f"[OrderQueue] Stopped — "
            f"processed={self._processed}, "
            f"retried={self._retried}, "
            f"failed={self._failed}",
            flush=True,
        )

    async def join(self) -> None:
        """Wait until the queue is empty and all in-flight tasks are done."""
        await self._queue.join()

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(self, item: OrderItem) -> None:
        """Add an order to the queue. Thread-safe via asyncio.PriorityQueue."""
        await self._queue.put((item.priority, item))

    def enqueue_nowait(self, item: OrderItem) -> None:
        """Synchronous enqueue (caller must ensure the event loop is running)."""
        self._queue.put_nowait((item.priority, item))

    # ── Workers ───────────────────────────────────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        """
        Worker coroutine — runs until stop() sends a poison pill.
        On processor error: retries with exponential back-off up to RETRY_MAX,
        then marks the item as permanently failed.
        """
        while True:
            try:
                _, item = await self._queue.get()
            except asyncio.CancelledError:
                break

            if item is None:  # Poison pill from stop()
                self._queue.task_done()
                break

            try:
                await self._processor(item)
                self._processed += 1

            except Exception as exc:
                if item.retry_count < RETRY_MAX:
                    item.retry_count += 1
                    self._retried += 1
                    delay = 2 ** item.retry_count  # 2s, 4s, 8s
                    print(
                        f"[OrderQueue] worker-{worker_id} retry "
                        f"{item.retry_count}/{RETRY_MAX} for {item.order_id} "
                        f"(backoff {delay}s): {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(delay)
                    await self._queue.put((OrderPriority.HIGH, item))
                else:
                    self._failed += 1
                    print(
                        f"[OrderQueue] worker-{worker_id} GAVE UP on "
                        f"{item.order_id} after {RETRY_MAX} retries: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

            finally:
                self._queue.task_done()

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Current number of items waiting in the queue."""
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "processed": self._processed,
            "retried": self._retried,
            "failed": self._failed,
            "workers": len(self._workers),
            "running": self._running,
        }
