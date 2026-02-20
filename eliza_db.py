#!/usr/bin/env python3
"""
ElizaOS Database Abstraction Layer — Grok-Gemini 2026 Resilience Stack.

Defines AbstractDatabaseInterface so the rest of the codebase is decoupled from
SQLite.  Swapping to Redis or Postgres is a single-line config change:

    # Default (SQLite):
    from eliza_db import get_db
    db = get_db()

    # Future Redis:
    from eliza_db import set_backend
    from my_redis_backend import RedisBackend
    set_backend(RedisBackend())

All eliza_memory.py public functions delegate to the active backend through
`get_db()`, which makes the migration transparent to every agent that already
imports eliza_memory.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ─── Abstract Interface ───────────────────────────────────────────────────────

class AbstractDatabaseInterface(ABC):
    """
    Contract that every database backend must fulfil.

    Methods map 1-to-1 with the original eliza_memory.py public API so that
    any existing `import eliza_memory; eliza_memory.foo()` call keeps working
    after the refactor — the module just delegates to the active backend.
    """

    # ── Memory ────────────────────────────────────────────────────────────────

    @abstractmethod
    def remember(
        self,
        agent: str,
        type_: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Store a typed memory entry. Returns the new memory id."""
        ...

    @abstractmethod
    def recall(
        self,
        agent: Optional[str] = None,
        type_: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query memories. Newest first."""
        ...

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
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
        """Insert or update an order record (COALESCE semantics on None fields)."""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[dict]:
        """Fetch a single order. Returns None if not found."""
        ...

    @abstractmethod
    def find_orders_by_email(self, email: str) -> list[dict]:
        """Look up all orders for a customer email. Newest first."""
        ...

    @abstractmethod
    def get_pending_orders(self) -> list[dict]:
        """Return all Pending orders. Oldest first."""
        ...

    # ── Security Events ───────────────────────────────────────────────────────

    @abstractmethod
    def log_security_event(
        self,
        agent: str,
        event_type: str,
        endpoint: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> str:
        """Log a security event. Returns the event id."""
        ...

    @abstractmethod
    def count_security_events(
        self,
        event_type: Optional[str] = None,
        since_minutes: int = 60,
    ) -> int:
        """Count security events in the last N minutes."""
        ...

    @abstractmethod
    def get_recent_security_events(
        self,
        since_minutes: int = 60,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch recent security events. Newest first."""
        ...


# ─── SQLite Backend ───────────────────────────────────────────────────────────

_DEFAULT_DB_PATH = Path.home() / ".config" / "howell-forge-eliza.db"


class SQLiteBackend(AbstractDatabaseInterface):
    """
    Default backend — pure stdlib SQLite3, zero external dependencies.

    To point at a different database file:
        backend = SQLiteBackend(db_path=Path("/path/to/other.db"))
        set_backend(backend)
    """

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH):
        self._db_path = db_path
        self._init_db()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          TEXT PRIMARY KEY,
                    agent       TEXT NOT NULL,
                    type        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    metadata    TEXT,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id        TEXT PRIMARY KEY,
                    customer_id     TEXT,
                    customer_email  TEXT,
                    status          TEXT NOT NULL,
                    amount_usd      REAL,
                    payment_uri     TEXT,
                    kaito_tx_id     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    raw_data        TEXT
                );

                CREATE TABLE IF NOT EXISTS security_events (
                    id          TEXT PRIMARY KEY,
                    agent       TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    endpoint    TEXT,
                    status_code INTEGER,
                    detail      TEXT,
                    created_at  TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_email
                    ON orders (customer_email);
                CREATE INDEX IF NOT EXISTS idx_orders_status
                    ON orders (status);
                CREATE INDEX IF NOT EXISTS idx_security_created
                    ON security_events (created_at);
                CREATE INDEX IF NOT EXISTS idx_memories_type
                    ON memories (type, agent);
            """)

    # ── Memory ────────────────────────────────────────────────────────────────

    def remember(
        self,
        agent: str,
        type_: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> str:
        mem_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO memories (id, agent, type, content, metadata, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (mem_id, agent, type_, content, json.dumps(metadata or {}), self._now()),
            )
        return mem_id

    def recall(
        self,
        agent: Optional[str] = None,
        type_: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if type_:
            query += " AND type = ?"
            params.append(type_)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Orders ────────────────────────────────────────────────────────────────

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
        now = self._now()
        email_lower = customer_email.lower() if customer_email else None
        raw_json = json.dumps(raw_data) if raw_data is not None else None

        with self._conn() as conn:
            existing = conn.execute(
                "SELECT order_id FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE orders SET
                        status          = ?,
                        customer_id     = COALESCE(?, customer_id),
                        customer_email  = COALESCE(?, customer_email),
                        amount_usd      = COALESCE(?, amount_usd),
                        payment_uri     = COALESCE(?, payment_uri),
                        kaito_tx_id     = COALESCE(?, kaito_tx_id),
                        updated_at      = ?,
                        raw_data        = COALESCE(?, raw_data)
                    WHERE order_id = ?""",
                    (status, customer_id, email_lower, amount_usd,
                     payment_uri, kaito_tx_id, now, raw_json, order_id),
                )
            else:
                conn.execute(
                    """INSERT INTO orders
                        (order_id, customer_id, customer_email, status, amount_usd,
                         payment_uri, kaito_tx_id, created_at, updated_at, raw_data)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, customer_id, email_lower, status, amount_usd,
                     payment_uri, kaito_tx_id, now, now, raw_json),
                )

    def get_order(self, order_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("raw_data"):
            try:
                result["raw_data"] = json.loads(result["raw_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    def find_orders_by_email(self, email: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE customer_email = ? ORDER BY created_at DESC",
                (email.lower(),),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("raw_data"):
                try:
                    d["raw_data"] = json.loads(d["raw_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def get_pending_orders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'Pending' ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Security Events ───────────────────────────────────────────────────────

    def log_security_event(
        self,
        agent: str,
        event_type: str,
        endpoint: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO security_events
                   (id, agent, event_type, endpoint, status_code, detail, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (event_id, agent, event_type, endpoint, status_code, detail, self._now()),
            )
        return event_id

    def count_security_events(
        self,
        event_type: Optional[str] = None,
        since_minutes: int = 60,
    ) -> int:
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        query = "SELECT COUNT(*) FROM security_events WHERE created_at >= ?"
        params: list = [since]
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        with self._conn() as conn:
            return conn.execute(query, params).fetchone()[0]

    def get_recent_security_events(
        self,
        since_minutes: int = 60,
        limit: int = 20,
    ) -> list[dict]:
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM security_events
                   WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Backend Registry ─────────────────────────────────────────────────────────

_backend: AbstractDatabaseInterface = SQLiteBackend()


def get_db() -> AbstractDatabaseInterface:
    """Return the active database backend (default: SQLiteBackend)."""
    return _backend


def set_backend(backend: AbstractDatabaseInterface) -> None:
    """
    Swap the active backend at runtime.

    Example — switch to a hypothetical Redis backend:
        from eliza_db import set_backend
        from my_redis_backend import RedisBackend
        set_backend(RedisBackend(url="redis://localhost:6379"))
    """
    global _backend
    if not isinstance(backend, AbstractDatabaseInterface):
        raise TypeError(
            f"Backend must be an AbstractDatabaseInterface subclass, "
            f"got {type(backend).__name__}"
        )
    _backend = backend
