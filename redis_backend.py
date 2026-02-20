#!/usr/bin/env python3
"""
Redis Backend — Howell Forge ElizaOS  (Grok-authored, adapted to AbstractDatabaseInterface)

Drop-in replacement for SQLiteBackend.  Activate via eliza-config.json:

    {
      "database": {
        "backend": "redis",
        "redis": { "url": "redis://localhost:6379/0", "max_connections": 50 }
      }
    }

Or at runtime:
    from eliza_db import set_backend
    from redis_backend import RedisBackend
    set_backend(RedisBackend(url="redis://localhost:6379/0"))

Key design:
  - Namespace prefix  "howell:"  prevents collisions with other apps
  - Sorted sets (score = Unix timestamp) for all time-based queries
  - Secondary indexes (sets/sorted sets) for email → orders, pending order list
  - COALESCE semantics on upsert_order: existing fields preserved when None passed
  - Append-only security event log (list) + timeline sorted set for window queries
  - Feature states stored as hashes; membership tracked in "howell:features:all" set
  - Pub/sub: publish_order_event() for real-time CS agent notifications (bonus)

Dependencies:  pip install redis
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    import redis as _redis
    from redis import ConnectionPool
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

from eliza_db import AbstractDatabaseInterface


# ─── Key schema ───────────────────────────────────────────────────────────────
#
#  howell:memory:{id}                  → JSON string  (full memory record)
#  howell:memories:timeline            → sorted set   score=unix_ts, member=memory_id
#  howell:memories:by_agent:{agent}    → set          of memory_ids
#  howell:memories:by_type:{type}      → set          of memory_ids
#
#  howell:order:{order_id}             → hash         (all order fields)
#  howell:orders:by_email:{email}      → sorted set   score=unix_ts, member=order_id
#  howell:orders:pending               → set          of order_ids with status=Pending
#
#  howell:security_event:{id}          → JSON string  (full event record)
#  howell:security_events:timeline     → sorted set   score=unix_ts, member=event_id
#
#  howell:feature:{feature_name}       → hash         (status, description, last_updated)
#  howell:features:all                 → set          of feature names


class RedisBackend(AbstractDatabaseInterface):
    """
    Full Redis implementation of AbstractDatabaseInterface.

    All methods are functional equivalents of SQLiteBackend — agents call
    get_db().remember(...) without knowing which backend is active.
    """

    NAMESPACE = "howell:"

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        max_connections: int = 50,
        socket_timeout: float = 5.0,
    ):
        if not _REDIS_AVAILABLE:
            raise ImportError(
                "redis package not installed. Run: pip install redis"
            )
        self.pool = ConnectionPool.from_url(
            url,
            max_connections=max_connections,
            socket_timeout=socket_timeout,
            decode_responses=True,  # strings, not bytes
        )
        self.client = _redis.Redis(connection_pool=self.pool)
        self._seed_features_if_empty()

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _k(self, category: str, *parts: str) -> str:
        """Build a namespaced Redis key."""
        return self.NAMESPACE + category + (":" + ":".join(str(p) for p in parts) if parts else "")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def _now_ts() -> float:
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _ts_from_str(ts_str: str) -> float:
        """Parse our standard timestamp string to a Unix float."""
        try:
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            return 0.0

    def _seed_features_if_empty(self) -> None:
        """Seed feature_states on first connect if the set is empty."""
        key = self._k("features:all")
        if self.client.scard(key) > 0:
            return
        seeds = [
            ("Kaito Payments",       "DEV",  "Polygon stablecoin payment processing"),
            ("Order Loop",           "BETA", "Async priority queue for concurrent order processing"),
            ("Security Handshake",   "LIVE", "fortress_watcher → security_agent → GitHub PR pipeline"),
            ("Biofeedback Scaling",  "LIVE", "EWMA reward/constraint score driving deployment cadence"),
            ("Customer Service Bot", "BETA", "ElizaOS-native CS agent with PAID gate"),
            ("Herald (Social Post)", "DEV",  "X/social post generation with entity whitelist"),
            ("Shop Manager",         "DEV",  "Order-to-production-to-ship workflow"),
        ]
        for name, status, desc in seeds:
            self.set_feature_status(name, status, desc)

    # ─── Memory ────────────────────────────────────────────────────────────

    def remember(
        self,
        agent: str,
        type_: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> str:
        mem_id = str(uuid.uuid4())
        now    = self._now_iso()
        ts     = self._now_ts()
        record = json.dumps({
            "id":         mem_id,
            "agent":      agent,
            "type":       type_,
            "content":    content,
            "metadata":   json.dumps(metadata or {}),
            "created_at": now,
        })

        pipe = self.client.pipeline()
        pipe.set(self._k("memory", mem_id), record)
        pipe.zadd(self._k("memories:timeline"), {mem_id: ts})
        pipe.sadd(self._k("memories:by_agent", agent), mem_id)
        pipe.sadd(self._k("memories:by_type", type_), mem_id)
        pipe.execute()

        return mem_id

    def recall(
        self,
        agent: Optional[str] = None,
        type_: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Return memories newest-first, optionally filtered by agent and/or type.

        When both filters are set: intersects the two index sets to get
        matching IDs, then fetches records and sorts by created_at.
        """
        if agent and type_:
            ids = self.client.sinter(
                self._k("memories:by_agent", agent),
                self._k("memories:by_type", type_),
            )
        elif agent:
            ids = self.client.smembers(self._k("memories:by_agent", agent))
        elif type_:
            ids = self.client.smembers(self._k("memories:by_type", type_))
        else:
            # All memories — pull newest from timeline sorted set
            ids = self.client.zrevrange(self._k("memories:timeline"), 0, limit - 1)

        if not ids:
            return []

        pipe = self.client.pipeline()
        for mid in ids:
            pipe.get(self._k("memory", mid))
        raw_list = pipe.execute()

        records = []
        for raw in raw_list:
            if raw:
                try:
                    records.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    pass

        # Sort newest first, truncate to limit
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records[:limit]

    # ─── Orders ────────────────────────────────────────────────────────────

    def upsert_order(
        self,
        order_id: str,
        status: str,
        customer_id: Optional[str] = None,
        customer_email: Optional[str] = None,
        amount_usd: Optional[float] = None,
        payment_uri: Optional[str] = None,
        kaito_tx_id: Optional[str] = None,
        raw_data: Optional[dict] = None,
    ) -> None:
        """
        COALESCE semantics: None values do not overwrite existing fields.
        Maintains secondary indexes:
          - howell:orders:by_email:{email}  (sorted set for date queries)
          - howell:orders:pending           (set of Pending order IDs)
        """
        key = self._k("order", order_id)
        now = self._now_iso()
        ts  = self._now_ts()

        existing_raw = self.client.hgetall(key)
        email_lower = customer_email.lower() if customer_email else None

        def _coalesce(field: str, new_val):
            if new_val is not None:
                return new_val
            return existing_raw.get(field)

        mapping = {
            "order_id":       order_id,
            "status":         status,
            "updated_at":     now,
            "customer_id":    _coalesce("customer_id", customer_id) or "",
            "customer_email": _coalesce("customer_email", email_lower) or "",
            "amount_usd":     str(_coalesce("amount_usd", amount_usd) or ""),
            "payment_uri":    _coalesce("payment_uri", payment_uri) or "",
            "kaito_tx_id":    _coalesce("kaito_tx_id", kaito_tx_id) or "",
            "raw_data":       json.dumps(_coalesce("raw_data", raw_data) or {}),
        }
        if not existing_raw:
            mapping["created_at"] = now

        pipe = self.client.pipeline()
        pipe.hset(key, mapping=mapping)

        # Secondary index: email → order
        if mapping["customer_email"]:
            pipe.zadd(
                self._k("orders:by_email", mapping["customer_email"]),
                {order_id: ts},
            )

        # Pending set maintenance
        if status == "Pending":
            pipe.sadd(self._k("orders:pending"), order_id)
        else:
            pipe.srem(self._k("orders:pending"), order_id)

        pipe.execute()

    def get_order(self, order_id: str) -> Optional[dict]:
        data = self.client.hgetall(self._k("order", order_id))
        if not data:
            return None
        return self._deserialise_order(data)

    def find_orders_by_email(self, email: str) -> list[dict]:
        """Newest first via sorted set score (timestamp)."""
        email_lower = email.lower()
        order_ids = self.client.zrevrange(
            self._k("orders:by_email", email_lower), 0, -1
        )
        if not order_ids:
            return []

        pipe = self.client.pipeline()
        for oid in order_ids:
            pipe.hgetall(self._k("order", oid))
        raw_list = pipe.execute()

        return [self._deserialise_order(d) for d in raw_list if d]

    def get_pending_orders(self) -> list[dict]:
        """Oldest first (sorted by created_at after fetch)."""
        order_ids = self.client.smembers(self._k("orders:pending"))
        if not order_ids:
            return []

        pipe = self.client.pipeline()
        for oid in order_ids:
            pipe.hgetall(self._k("order", oid))
        raw_list = pipe.execute()

        orders = [self._deserialise_order(d) for d in raw_list if d]
        orders.sort(key=lambda o: o.get("created_at", ""))
        return orders

    @staticmethod
    def _deserialise_order(data: dict) -> dict:
        result = dict(data)
        if result.get("raw_data"):
            try:
                result["raw_data"] = json.loads(result["raw_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        if result.get("amount_usd"):
            try:
                result["amount_usd"] = float(result["amount_usd"])
            except (ValueError, TypeError):
                pass
        return result

    # ─── Feature States ────────────────────────────────────────────────────

    def get_feature_status(self, feature_name: str) -> Optional[str]:
        return self.client.hget(self._k("feature", feature_name), "status")

    def set_feature_status(
        self,
        feature_name: str,
        status: str,
        description: Optional[str] = None,
    ) -> None:
        key = self._k("feature", feature_name)
        mapping: dict = {"status": status, "last_updated": self._now_iso(),
                         "feature_name": feature_name}
        if description is not None:
            mapping["description"] = description
        self.client.hset(key, mapping=mapping)
        self.client.sadd(self._k("features:all"), feature_name)

    def get_all_features(self) -> list[dict]:
        names = self.client.smembers(self._k("features:all"))
        if not names:
            return []
        pipe = self.client.pipeline()
        for name in names:
            pipe.hgetall(self._k("feature", name))
        raw_list = pipe.execute()
        rows = [dict(r) for r in raw_list if r]
        rows.sort(key=lambda r: r.get("feature_name", ""))
        return rows

    # ─── Security Events ───────────────────────────────────────────────────

    def log_security_event(
        self,
        agent: str,
        event_type: str,
        endpoint: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        now = self._now_iso()
        ts  = self._now_ts()
        record = json.dumps({
            "id":           event_id,
            "agent":        agent,
            "event_type":   event_type,
            "endpoint":     endpoint or "",
            "status_code":  status_code,
            "detail":       detail or "",
            "created_at":   now,
        })

        pipe = self.client.pipeline()
        pipe.set(self._k("security_event", event_id), record)
        pipe.zadd(self._k("security_events:timeline"), {event_id: ts})
        pipe.execute()

        return event_id

    def count_security_events(
        self,
        event_type: Optional[str] = None,
        since_minutes: int = 60,
    ) -> int:
        since_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).timestamp()

        # All event IDs in the time window
        ids = self.client.zrangebyscore(
            self._k("security_events:timeline"), since_ts, "+inf"
        )
        if not ids:
            return 0
        if event_type is None:
            return len(ids)

        # Filter by event_type
        pipe = self.client.pipeline()
        for eid in ids:
            pipe.get(self._k("security_event", eid))
        records = [
            json.loads(r) for r in pipe.execute()
            if r
            and json.loads(r).get("event_type") == event_type
        ]
        return len(records)

    def get_recent_security_events(
        self,
        since_minutes: int = 60,
        limit: int = 20,
    ) -> list[dict]:
        since_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).timestamp()

        # Newest first: ZREVRANGEBYSCORE
        ids = self.client.zrevrangebyscore(
            self._k("security_events:timeline"), "+inf", since_ts, start=0, num=limit
        )
        if not ids:
            return []

        pipe = self.client.pipeline()
        for eid in ids:
            pipe.get(self._k("security_event", eid))
        return [json.loads(r) for r in pipe.execute() if r]

    # ─── Pub/Sub (bonus — Grok) ────────────────────────────────────────────

    def publish_order_event(
        self, event_type: str, order_id: str, data: dict
    ) -> None:
        """
        Publish a real-time order event to the Redis pub/sub channel.
        Customer Service Agent can subscribe to receive instant updates
        without polling the DB.

        Usage (subscriber side):
            pubsub = backend.client.pubsub()
            pubsub.subscribe("howell:order_events")
            for msg in pubsub.listen():
                if msg["type"] == "message":
                    payload = json.loads(msg["data"])
                    # payload = {"type": "paid", "order_id": ..., "data": {...}}
        """
        channel = self._k("order_events")
        payload = json.dumps({
            "type":     event_type,
            "order_id": order_id,
            "data":     data,
            "ts":       self._now_iso(),
        })
        self.client.publish(channel, payload)

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Drain active connections back to the pool, then disconnect."""
        try:
            self.client.close()
        except Exception:
            pass
        try:
            self.pool.disconnect()
        except Exception:
            pass
