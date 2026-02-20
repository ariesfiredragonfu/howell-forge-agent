#!/usr/bin/env python3
"""
ElizaOS Memory Layer — Shared AgentState for Howell Forge agents.

Public API is unchanged; all persistence now delegates to eliza_db.get_db()
so the backend can be swapped (SQLite → Redis/Postgres) without touching
any agent code.

ElizaOS concepts mapped here:
  AgentState   → singleton dataclass + database persistence
  Memory       → rows in the `memories` table (typed, timestamped)
  Database     → eliza_db.AbstractDatabaseInterface (default: SQLite)
  Evaluators   → called by security_hooks.py (reads security_events table)
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from eliza_db import get_db

# Keep DB_PATH accessible for any code that references it directly
DB_PATH = Path.home() / ".config" / "howell-forge-eliza.db"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _init_db() -> None:
    """No-op: SQLiteBackend initialises the schema in its __init__."""
    pass


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
    """Store a typed memory entry (ElizaOS: runtime.createMemory). Returns new id."""
    return get_db().remember(agent, type_, content, metadata)


def recall(
    agent: Optional[str] = None,
    type_: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Query memories (ElizaOS: runtime.getMemories). Newest first."""
    return get_db().recall(agent, type_, limit)


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
    Insert or update an order record.
    Existing values are preserved when None is passed (COALESCE semantics).
    Mirrors status into in-process AgentState.
    """
    get_db().upsert_order(
        order_id=order_id, status=status, customer_id=customer_id,
        customer_email=customer_email, amount_usd=amount_usd,
        payment_uri=payment_uri, kaito_tx_id=kaito_tx_id, raw_data=raw_data,
    )
    # Mirror into in-process AgentState
    now = _now()
    state = get_agent_state()
    state.active_orders[order_id] = {"status": status, "updated_at": now}
    state.last_updated = now


def get_order(order_id: str) -> Optional[dict]:
    """Fetch a single order. Returns None if not found."""
    return get_db().get_order(order_id)


def find_orders_by_email(email: str) -> list[dict]:
    """Look up all orders for a customer email. Newest first."""
    return get_db().find_orders_by_email(email)


def get_pending_orders() -> list[dict]:
    """Return all Pending orders. Oldest first."""
    return get_db().get_pending_orders()


def get_all_orders(limit: int = 100) -> list[dict]:
    """Return all orders newest-first, up to limit."""
    return get_db().get_all_orders(limit=limit)


# ─── Security Event API ───────────────────────────────────────────────────────

def log_security_event(
    agent: str,
    event_type: str,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    detail: Optional[str] = None,
) -> str:
    """Log a security event (401/403, failed tx, etc.). Returns event id."""
    return get_db().log_security_event(agent, event_type, endpoint, status_code, detail)


def count_security_events(
    event_type: Optional[str] = None,
    since_minutes: int = 60,
) -> int:
    """Count security events in the last N minutes."""
    return get_db().count_security_events(event_type, since_minutes)


def get_recent_security_events(since_minutes: int = 60, limit: int = 20) -> list[dict]:
    """Fetch recent security events. Newest first."""
    return get_db().get_recent_security_events(since_minutes, limit)


# ─── Pub/Sub ───────────────────────────────────────────────────────────────────

def publish_order_paid(order_id: str, data: dict) -> None:
    """
    Fire a real-time "paid" event for the given order.

    When the active backend is RedisBackend this publishes to the
    "howell:order_events" channel so the Customer Service Agent (or any
    other subscriber) receives an instant push without polling.

    When the active backend is SQLiteBackend the base-class no-op fires —
    no crash, no hasattr() check, no branching required at call sites.
    The PAID state is already persisted in Eliza memory via upsert_order,
    so the CS agent will see it on its next poll regardless.
    """
    get_db().publish_order_event("paid", order_id, data)
