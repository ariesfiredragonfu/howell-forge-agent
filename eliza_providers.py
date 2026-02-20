#!/usr/bin/env python3
"""
ElizaOS Providers — Shared memory bridge for the Order Loop agents.

In ElizaOS, a Provider is a function that injects real-time context into an
agent's runtime state.  Both the Shop Agent and Customer Service Agent import
the same Provider classes, so they always read from the same SQLite source.

This is the "shared memory bridge":
  Shop Agent writes  → eliza_memory (SQLite)
  Provider reads     → eliza_memory (SQLite)
  CS Agent reads     → Provider.get()

Because the Provider reads the DB on every call, the Customer Service Agent
sees a Shop Agent write the millisecond it is committed — no polling, no
external API round-trip.

Providers defined here:
  OrderStateProvider     → full order context for a given order_id or email
  SecurityContextProvider → recent security events for Monitor Agent awareness

Usage:
  from eliza_providers import OrderStateProvider

  provider = OrderStateProvider()
  ctx = provider.get(state, {"order_id": "pi_xxx"})
  if ctx["is_paid"]:
      show_delivery_info(ctx)
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import eliza_memory
from eliza_memory import AgentState, get_agent_state

# ─── Base ─────────────────────────────────────────────────────────────────────


class Provider(ABC):
    """
    ElizaOS Provider interface.

    get() is called at runtime to inject fresh context into an agent.
    Implementations must be stateless — all state lives in eliza_memory.
    """

    name: str = "base_provider"
    description: str = ""

    def get(
        self,
        state: Optional[AgentState] = None,
        context: Optional[dict] = None,
    ) -> dict:
        """
        Return a context snapshot.  Always reads fresh from eliza_memory.
        Never cached — calling twice may return different results if the
        Shop Agent wrote a status update between calls.
        """
        if state is None:
            state = get_agent_state()
        return self._get(state, context or {})

    @abstractmethod
    def _get(self, state: AgentState, context: dict) -> dict:
        ...


# ─── OrderStateProvider ───────────────────────────────────────────────────────


class OrderStateProvider(Provider):
    """
    Shared order-state context for both Shop Agent and Customer Service Agent.

    Returns the full order record plus computed flags that control what each
    agent is allowed to do:

      is_paid            → True when status == "PAID" (VerifyPaymentAction confirmed)
                           or "Success" (Stripe-imported confirmed)
      delivery_unlocked  → alias for is_paid (PAID/Success → show shipping/delivery)
      is_pending         → True when status == "Pending"
      has_payment_uri    → True when a Kaito payment URI exists
      tx_hash            → on-chain hash if PAID, else None

    If context contains "order_id", returns that single order.
    If context contains "email", returns the customer's order list.
    If neither, returns a queue-level summary (pending counts, recent events).
    """

    name = "ORDER_STATE"
    description = "Real-time order state from Eliza memory — shared by Shop and CS agents"

    # Statuses that unlock delivery / shipping information
    PAID_STATUSES: frozenset[str] = frozenset({"PAID", "Success"})
    # Statuses that are still in-flight
    PENDING_STATUSES: frozenset[str] = frozenset({"Pending", "Processing"})

    def _get(self, state: AgentState, context: dict) -> dict:
        order_id: Optional[str] = context.get("order_id")
        email: Optional[str] = context.get("email")

        if order_id:
            return self._order_context(order_id, state)
        if email:
            return self._email_context(email, state)
        return self._queue_summary(state)

    # ── Single-order context ───────────────────────────────────────────────

    def _order_context(self, order_id: str, state: AgentState) -> dict:
        order = eliza_memory.get_order(order_id)
        if order is None:
            return {
                "found": False,
                "order_id": order_id,
                "order": None,
                "is_paid": False,
                "delivery_unlocked": False,
                "is_pending": False,
                "has_payment_uri": False,
                "tx_hash": None,
                "delivery_info": None,
            }

        status: str = order.get("status", "")
        raw: dict = order.get("raw_data") or {}
        tx_hash: Optional[str] = raw.get("tx_hash") or raw.get("block_hash")

        is_paid = status in self.PAID_STATUSES
        is_pending = status in self.PENDING_STATUSES

        return {
            "found": True,
            "order_id": order_id,
            "order": order,
            "status": status,
            "is_paid": is_paid,
            "delivery_unlocked": is_paid,
            "is_pending": is_pending,
            "has_payment_uri": bool(order.get("payment_uri")),
            "tx_hash": tx_hash,
            "delivery_info": _build_delivery_info(order) if is_paid else None,
            # Mirror in AgentState for in-process speed on repeated reads
            "_agent_state_cache": state.active_orders.get(order_id),
        }

    # ── Email (multi-order) context ────────────────────────────────────────

    def _email_context(self, email: str, state: AgentState) -> dict:
        orders = eliza_memory.find_orders_by_email(email)
        enriched = []
        for o in orders:
            status = o.get("status", "")
            raw = o.get("raw_data") or {}
            is_paid = status in self.PAID_STATUSES
            enriched.append({
                **o,
                "is_paid": is_paid,
                "delivery_unlocked": is_paid,
                "is_pending": status in self.PENDING_STATUSES,
                "delivery_info": _build_delivery_info(o) if is_paid else None,
                "tx_hash": raw.get("tx_hash") or raw.get("block_hash"),
            })
        return {
            "email": email,
            "orders": enriched,
            "total": len(enriched),
            "paid_count": sum(1 for o in enriched if o["is_paid"]),
            "pending_count": sum(1 for o in enriched if o["is_pending"]),
        }

    # ── Queue-level summary ────────────────────────────────────────────────

    def _queue_summary(self, state: AgentState) -> dict:
        pending = eliza_memory.get_pending_orders()
        recent_events = eliza_memory.recall(type_="PAYMENT_EVENT", limit=10)
        return {
            "pending_orders": pending,
            "pending_count": len(pending),
            "recent_events": recent_events,
            "agent_state": {
                "session_id": state.session_id,
                "active_orders": len(state.active_orders),
                "last_updated": state.last_updated,
            },
        }


# ─── SecurityContextProvider ──────────────────────────────────────────────────


class SecurityContextProvider(Provider):
    """
    Security event context — consumed by the Monitor Agent and any agent that
    needs to know whether the self-healing protocol has been triggered.

    Returns recent auth errors, failed transactions, and self-healing events
    so agents can self-censor (e.g., stop retrying a dead endpoint).

    context keys accepted:
      since_minutes   (int, default 60)  — rolling window
      event_type      (str, optional)    — filter to specific event type
    """

    name = "SECURITY_CONTEXT"
    description = "Recent security events from Eliza memory — for Monitor Agent awareness"

    def _get(self, state: AgentState, context: dict) -> dict:
        since: int = int(context.get("since_minutes", 60))
        event_type: Optional[str] = context.get("event_type")

        events = eliza_memory.get_recent_security_events(
            since_minutes=since, limit=50
        )
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]

        auth_errors = [e for e in events if e.get("event_type", "").startswith("AUTH_ERROR_")]
        failed_txs = [e for e in events if e.get("event_type") == "FAILED_TRANSACTION"]
        self_healing = eliza_memory.recall(type_="SELF_HEALING_TRIGGERED", limit=5)

        return {
            "since_minutes": since,
            "total_events": len(events),
            "auth_error_count": len(auth_errors),
            "failed_tx_count": len(failed_txs),
            "self_healing_triggered": len(self_healing) > 0,
            "self_healing_events": self_healing,
            "recent_auth_errors": auth_errors[:5],
            "recent_failed_txs": failed_txs[:5],
            "all_events": events,
        }


# ─── Delivery Info Builder ────────────────────────────────────────────────────


def _build_delivery_info(order: dict) -> dict:
    """
    Construct delivery / shipping information for a PAID order.

    In production this would pull from a shipping API (ShipStation, etc.).
    For now it reads any tracking data stored in raw_data by the Shop Agent,
    and falls back to standard Howell Forge lead-time messaging.
    """
    raw: dict = order.get("raw_data") or {}
    kaito: dict = raw.get("kaito") or {}

    tracking_number: Optional[str] = raw.get("tracking_number")
    carrier: str = raw.get("carrier", "USPS/UPS")
    ship_date: Optional[str] = raw.get("ship_date")
    digital_asset_url: Optional[str] = raw.get("digital_asset_url")

    # Estimated ship date: 3 business days from order created_at
    created_str: str = order.get("created_at", "")
    if not ship_date and created_str:
        try:
            created = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S UTC")
            est_days = 3
            ship_date = f"~{est_days} business days from {created.strftime('%b %d, %Y')}"
        except ValueError:
            ship_date = "3–5 business days"

    return {
        "status": "PAID — delivery information unlocked",
        "tx_hash": raw.get("tx_hash") or raw.get("block_hash"),
        "tracking_number": tracking_number,
        "carrier": carrier if tracking_number else None,
        "estimated_ship_date": ship_date or "3–5 business days",
        "digital_asset_url": digital_asset_url,
        "contact_for_updates": "chrishowell@howell-forge.com",
        "note": (
            "Your on-chain payment has been confirmed by the Kaito engine. "
            "Howell Forge will begin production immediately. "
            "You will receive a Telegram notification when your order ships."
        ),
    }


# ─── Convenience singleton accessors ──────────────────────────────────────────

# Module-level singletons — import and call .get() directly
order_state = OrderStateProvider()
security_context = SecurityContextProvider()
