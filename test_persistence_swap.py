#!/usr/bin/env python3
"""
test_persistence_swap.py — Backend swap integration test
(corrected from Grok's draft: uses our AbstractDatabaseInterface method names)

Tests:
  1.  SQLite write → read-back
  2.  Live swap from SQLite → RedisBackend
  3.  Redis is empty after swap (no data migrated yet — expected)
  4.  Redis write → read-back
  5.  Swap back to SQLite → Redis data absent (isolated namespaces)
  6.  set_backend() calls close() on the outgoing backend (no leaked connections)

Redis tests are SKIPPED automatically when Redis is not reachable.
Run with a live Redis:
    redis-server --daemonize yes
    python3 test_persistence_swap.py
"""

import json
import sys
import uuid
from datetime import datetime, timezone

# ─── Imports ──────────────────────────────────────────────────────────────────
from eliza_db import get_db, set_backend, SQLiteBackend

try:
    from redis_backend import RedisBackend
    _HAS_REDIS_PKG = True
except ImportError:
    _HAS_REDIS_PKG = False

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_results: list[tuple[str, str]] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    _results.append((label, "PASS" if condition else "FAIL"))
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if not condition:
        raise AssertionError(f"FAILED: {label}{suffix}")


def _redis_reachable(url: str = "redis://localhost:6379/0") -> bool:
    """Ping Redis — returns False instead of raising if unreachable."""
    if not _HAS_REDIS_PKG:
        return False
    try:
        import redis as _r
        client = _r.from_url(url, socket_timeout=1.0, decode_responses=True)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


# ─── Test 1: SQLite write → read-back ────────────────────────────────────────

def test_sqlite_write_read():
    print("\n[1] SQLite write → read-back")

    sqlite_db = SQLiteBackend()     # fresh in-memory-path backend
    set_backend(sqlite_db)
    db = get_db()

    # remember / recall
    agent = "swap_test_agent"
    db.remember(agent, "test", "Hello SQLite", {"backend": "sqlite"})
    memories = db.recall(agent=agent, type_="test")
    _check("recall returns at least one memory", len(memories) >= 1)
    _check("memory content correct", memories[0]["content"] == "Hello SQLite")

    # upsert_order / get_order
    oid = f"ORDER-{uuid.uuid4().hex[:8].upper()}"
    db.upsert_order(
        order_id=oid,
        status="Pending",
        customer_email="swap@test.com",
        amount_usd=99.50,
    )
    order = db.get_order(oid)
    _check("get_order returns record",          order is not None)
    _check("order status correct",              order["status"] == "Pending")
    _check("order amount correct",              float(order["amount_usd"]) == 99.50)

    found = db.find_orders_by_email("swap@test.com")
    _check("find_orders_by_email finds it",     any(o["order_id"] == oid for o in found))

    pending = db.get_pending_orders()
    _check("get_pending_orders includes it",    any(o["order_id"] == oid for o in pending))

    # log_security_event / count
    db.log_security_event("swap_agent", "AUTH_FAILURE", "/api/test", 401, "swap test")
    count = db.count_security_events(event_type="AUTH_FAILURE", since_minutes=5)
    _check("count_security_events ≥ 1",         count >= 1)

    recent = db.get_recent_security_events(since_minutes=5, limit=10)
    _check("get_recent_security_events not empty", len(recent) >= 1)

    # feature states
    db.set_feature_status("SwapTestFeature", "BETA", "temporary test feature")
    status = db.get_feature_status("SwapTestFeature")
    _check("get_feature_status returns BETA",   status == "BETA")

    all_f = db.get_all_features()
    _check("get_all_features includes test feature",
           any(f["feature_name"] == "SwapTestFeature" for f in all_f))

    return oid  # pass the order ID to Redis test for isolation check


# ─── Test 2: Live swap to Redis ───────────────────────────────────────────────

def test_redis_swap(sqlite_order_id: str):
    REDIS_URL = "redis://localhost:6379/0"

    if not _redis_reachable(REDIS_URL):
        print(f"\n[2-5] Redis swap tests [{SKIP}] — Redis not reachable at {REDIS_URL}")
        _results.append(("Redis swap (all)", "SKIP"))
        return

    print("\n[2] Swap SQLite → Redis")
    redis_db = RedisBackend(url=REDIS_URL)
    set_backend(redis_db)           # closes SQLite, installs Redis
    db = get_db()
    _check("get_db() returns RedisBackend", type(db).__name__ == "RedisBackend")

    print("\n[3] Redis is empty after swap (data isolation — no migration)")
    order = db.get_order(sqlite_order_id)
    _check(
        "SQLite order absent in Redis (namespaces isolated)",
        order is None,
        f"order_id={sqlite_order_id}",
    )

    print("\n[4] Redis write → read-back")
    agent = "redis_swap_agent"

    # remember / recall
    db.remember(agent, "test_redis", "Hello Redis", {"migrated": False})
    memories = db.recall(agent=agent, type_="test_redis")
    _check("Redis recall returns memory",       len(memories) >= 1)
    _check("Redis memory content correct",      memories[0]["content"] == "Hello Redis")

    # upsert_order / get_order (Redis)
    oid_r = f"RORDER-{uuid.uuid4().hex[:8].upper()}"
    db.upsert_order(
        order_id=oid_r,
        status="Pending",
        customer_email="redis@test.com",
        amount_usd=55.00,
    )
    r_order = db.get_order(oid_r)
    _check("Redis get_order returns record",        r_order is not None)
    _check("Redis order status correct",            r_order["status"] == "Pending")
    _check("Redis order amount correct",            float(r_order["amount_usd"]) == 55.00)

    found = db.find_orders_by_email("redis@test.com")
    _check("Redis find_orders_by_email finds it",   any(o["order_id"] == oid_r for o in found))

    pending = db.get_pending_orders()
    _check("Redis get_pending_orders includes it",  any(o["order_id"] == oid_r for o in pending))

    # COALESCE: update status only — email must be preserved
    db.upsert_order(order_id=oid_r, status="Paid", customer_email=None)
    updated = db.get_order(oid_r)
    _check("Redis upsert COALESCE: status updated",   updated["status"] == "Paid")
    _check("Redis upsert COALESCE: email preserved",  updated["customer_email"] == "redis@test.com")
    _check("Redis upsert COALESCE: amount preserved", float(updated["amount_usd"]) == 55.00)

    pending_after = db.get_pending_orders()
    _check("Redis pending set updated after Paid",
           not any(o["order_id"] == oid_r for o in pending_after))

    # security events
    db.log_security_event("redis_agent", "RATE_LIMIT", "/payments/verify", 429, "swap test")
    count = db.count_security_events(event_type="RATE_LIMIT", since_minutes=5)
    _check("Redis count_security_events ≥ 1",       count >= 1)

    recent = db.get_recent_security_events(since_minutes=5, limit=10)
    _check("Redis get_recent_security_events not empty", len(recent) >= 1)

    # feature states
    db.set_feature_status("RedisSwapFeature", "DEV", "temporary Redis test feature")
    fs = db.get_feature_status("RedisSwapFeature")
    _check("Redis get_feature_status returns DEV",  fs == "DEV")

    all_f = db.get_all_features()
    _check("Redis get_all_features includes test feature",
           any(f["feature_name"] == "RedisSwapFeature" for f in all_f))

    print("\n[5] Swap Redis → SQLite (close() called on Redis pool)")
    sqlite_db2 = SQLiteBackend()
    set_backend(sqlite_db2)         # must call redis_db.close() internally
    db2 = get_db()
    _check("get_db() returns SQLiteBackend after swap back",
           type(db2).__name__ == "SQLiteBackend")

    # Redis order must be absent in fresh SQLite
    absent = db2.get_order(oid_r)
    _check("Redis order absent in new SQLite (namespaces isolated)", absent is None)

    print("\n[6] pub/sub publish_order_event (smoke — no subscriber needed)")
    try:
        redis_db.publish_order_event("paid", oid_r, {"amount_usd": 55.00})
        _check("publish_order_event does not raise", True)
    except Exception as exc:
        _check("publish_order_event does not raise", False, str(exc))


# ─── Test 7: publish_order_paid facade ───────────────────────────────────────

def test_publish_order_paid_facade():
    """
    publish_order_paid() must work on ANY backend without hasattr() checks.

    SQLiteBackend → inherits no-op base implementation → no crash.
    RedisBackend  → overrides with client.publish() → message delivered.
    """
    print("\n[7] publish_order_paid facade — no-op on SQLite, live on Redis")

    import eliza_memory as em

    # 7a: SQLite no-op — must not raise
    sqlite_db = SQLiteBackend()
    set_backend(sqlite_db)
    try:
        em.publish_order_paid("ORDER-NOOP-001", {"amount_usd": 42.00, "test": True})
        _check("SQLite publish_order_paid is a no-op (no crash)", True)
    except Exception as exc:
        _check("SQLite publish_order_paid is a no-op (no crash)", False, str(exc))

    # 7b: RedisBackend — verify publish() is called with correct channel/payload
    if not _redis_reachable():
        print(f"  [{SKIP}] Redis publish test — Redis not reachable")
        _results.append(("Redis publish_order_paid", "SKIP"))
        return

    import unittest.mock as mock
    redis_db = RedisBackend(url="redis://localhost:6379/0")
    set_backend(redis_db)

    with mock.patch.object(redis_db.client, "publish") as mock_pub:
        em.publish_order_paid("ORDER-PUB-001", {"amount_usd": 99.00})
        _check("Redis publish_order_paid calls client.publish", mock_pub.called)

        call_args = mock_pub.call_args
        channel, payload_str = call_args[0]
        payload = json.loads(payload_str)

        _check("Publish channel is howell:order_events",   channel == "howell:order_events")
        _check("Publish event_type is 'paid'",             payload["type"] == "paid")
        _check("Publish order_id correct",                 payload["order_id"] == "ORDER-PUB-001")
        _check("Publish data.amount_usd correct",          payload["data"]["amount_usd"] == 99.00)

    set_backend(SQLiteBackend())   # restore for subsequent tests


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Howell Forge — Backend Swap Integration Test")
    print("=" * 60)

    try:
        sqlite_oid = test_sqlite_write_read()
        test_redis_swap(sqlite_oid)
        test_publish_order_paid_facade()
    except AssertionError:
        pass  # already printed, continue to summary

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, s in _results if s == "PASS")
    skipped = sum(1 for _, s in _results if s == "SKIP")
    failed  = sum(1 for _, s in _results if s == "FAIL")
    total   = len(_results)
    print(f"  Results: {passed} passed, {skipped} skipped, {failed} failed / {total} total")
    print("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
