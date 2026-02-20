#!/usr/bin/env python3
"""
ElizaOS Memory Layer — Shared AgentState for Howell Forge agents.

Simulates the ElizaOS memory/database interface using SQLite3 (stdlib only).
All Order Loop agents read/write through this module for order state,
payment events, and security events.

ElizaOS concepts mapped here:
  AgentState   → singleton dataclass + SQLite persistence
  Memory       → rows in the `memories` table (typed, timestamped)
  Database     → ~/.config/howell-forge-eliza.db (SQLite)
  Evaluators   → called by security_hooks.py (reads security_events table)
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".config" / "howell-forge-eliza.db"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_conn() as conn:
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


# ─── AgentState ───────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    """
    Shared mutable state for the ElizaOS Order Loop.
    Lives in-process; persisted copy lives in SQLite (orders + memories tables).
    """
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    active_orders: dict = field(default_factory=dict)    # order_id → {status, updated_at}
    pending_refresh: list = field(default_factory=list)  # order_ids awaiting refresh
    error_counts: dict = field(default_factory=dict)     # endpoint → cumulative error count
    last_updated: str = field(default_factory=_now)


_AGENT_STATE: Optional[AgentState] = None


def get_agent_state() -> AgentState:
    """Return (or lazily create) the singleton AgentState for this process."""
    global _AGENT_STATE
    if _AGENT_STATE is None:
        _AGENT_STATE = AgentState()
    return _AGENT_STATE


# ─── Memory API ───────────────────────────────────────────────────────────────

def remember(
    agent: str,
    type_: str,
    content: str,
    metadata: Optional[dict] = None,
) -> str:
    """
    Store a typed memory entry (ElizaOS: runtime.createMemory).
    Returns the new memory id.
    """
    _init_db()
    mem_id = str(uuid.uuid4())
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO memories (id, agent, type, content, metadata, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (mem_id, agent, type_, content, json.dumps(metadata or {}), _now()),
        )
    return mem_id


def recall(
    agent: Optional[str] = None,
    type_: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Query memories (ElizaOS: runtime.getMemories).
    Optional filter by agent or type. Newest first.
    """
    _init_db()
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
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─── Order State API ──────────────────────────────────────────────────────────

def upsert_order(
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
    Insert or update an order record in Eliza memory.
    All fields except order_id + status are optional; existing values are preserved
    on update when None is passed (COALESCE semantics).
    """
    _init_db()
    now = _now()
    email_lower = customer_email.lower() if customer_email else None

    with _get_conn() as conn:
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
                (
                    status, customer_id, email_lower, amount_usd,
                    payment_uri, kaito_tx_id, now,
                    json.dumps(raw_data) if raw_data is not None else None,
                    order_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO orders
                    (order_id, customer_id, customer_email, status, amount_usd,
                     payment_uri, kaito_tx_id, created_at, updated_at, raw_data)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id, customer_id, email_lower, status, amount_usd,
                    payment_uri, kaito_tx_id, now, now,
                    json.dumps(raw_data) if raw_data is not None else None,
                ),
            )

    # Mirror into in-process AgentState
    state = get_agent_state()
    state.active_orders[order_id] = {"status": status, "updated_at": now}
    state.last_updated = now


def get_order(order_id: str) -> Optional[dict]:
    """Fetch a single order from Eliza memory. Returns None if not found."""
    _init_db()
    with _get_conn() as conn:
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


def find_orders_by_email(email: str) -> list[dict]:
    """Look up all orders for a customer email. Newest first."""
    _init_db()
    with _get_conn() as conn:
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


def get_pending_orders() -> list[dict]:
    """Return all orders with status = 'Pending'. Oldest first (process in order)."""
    _init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'Pending' ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Security Event API ───────────────────────────────────────────────────────

def log_security_event(
    agent: str,
    event_type: str,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    detail: Optional[str] = None,
) -> str:
    """
    Log a security event (401/403, failed transaction, etc.).
    Returns the event id for reference.
    """
    _init_db()
    event_id = str(uuid.uuid4())
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO security_events
               (id, agent, event_type, endpoint, status_code, detail, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (event_id, agent, event_type, endpoint, status_code, detail, _now()),
        )
    return event_id


def count_security_events(
    event_type: Optional[str] = None,
    since_minutes: int = 60,
) -> int:
    """Count security events in the last N minutes. Optional filter by event_type."""
    _init_db()
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    query = "SELECT COUNT(*) FROM security_events WHERE created_at >= ?"
    params: list = [since]
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    with _get_conn() as conn:
        return conn.execute(query, params).fetchone()[0]


def get_recent_security_events(since_minutes: int = 60, limit: int = 20) -> list[dict]:
    """Fetch recent security events for Monitor Agent review."""
    _init_db()
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM security_events
               WHERE created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]
