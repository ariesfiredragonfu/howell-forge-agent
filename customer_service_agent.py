#!/usr/bin/env python3
"""
Customer Service Agent (ElizaOS Edition) — Provider-backed, PAID-gated.

All order state is read through the shared OrderStateProvider, which reads
the same Eliza memory (SQLite) that the Shop Agent writes to.  The CS Agent
never calls Stripe or Kaito directly — it consumes the Provider context.

PAID Gate:
  Only orders with status "PAID" or "Success" unlock delivery / shipping info.
  Orders still in "Pending" trigger a RefreshPaymentAction via the Kaito engine.
  Orders in "Failed" or "Expired" receive an appropriate message.

Commands:
  order <id|email>   — look up order status via Provider
  faq <topic>        — hours | contact | about | payment | shipping
  summary            — recent Order Loop activity from Eliza memory
  pending            — list all Pending orders
  security           — show recent security events (Monitor-facing)
"""

import asyncio
import sys
from typing import Optional

import biofeedback
import eliza_memory
from eliza_actions import RefreshPaymentAction, log_action_error, refresh_payment
from eliza_providers import OrderStateProvider, order_state as provider

AGENT_NAME = "CUSTOMER_SERVICE_AGENT"

FAQ: dict[str, str] = {
    "hours": (
        "Business Hours: Monday – Friday, 9:00 AM – 5:00 PM EST.\n"
        "Weekend inquiries are reviewed the following Monday."
    ),
    "contact": (
        "Email: chrishowell@howell-forge.com\n"
        "Include your order ID and project details for the fastest response."
    ),
    "about": (
        "Howell Forge specialises in high-quality precision metal parts.\n"
        "We combine CNC machining with on-chain payment transparency."
    ),
    "payment": (
        "We accept stablecoin payments via the Kaito engine, processed on-chain.\n"
        "You receive a unique payment URI for each order — no card details required."
    ),
    "shipping": (
        "Orders ship within 3–5 business days after on-chain payment confirmation.\n"
        "You will receive a Telegram notification when your order ships."
    ),
}


# ─── Public API ───────────────────────────────────────────────────────────────

def where_is_my_order(identifier: str) -> None:
    """
    Main 'Where is my order?' handler.

    Accepts:
      - Stripe payment intent ID  (pi_xxx)
      - Any other order_id
      - Customer email            (contains @)
    """
    identifier = identifier.strip()
    state = eliza_memory.get_agent_state()

    if "@" in identifier:
        ctx = provider.get(state, {"email": identifier})
        _handle_email_context(ctx)
    else:
        ctx = provider.get(state, {"order_id": identifier})
        _handle_order_context(ctx)


def show_faq(topic: str) -> None:
    topic = topic.lower().strip()
    if topic in FAQ:
        print(f"\n{FAQ[topic]}")
    else:
        print(f"Available topics: {', '.join(FAQ)}")
        print("Usage:  python3 customer_service_agent.py faq <topic>")


def show_memory_summary() -> None:
    """Print recent Order Loop activity from Eliza memory."""
    memories = eliza_memory.recall(type_="PAYMENT_EVENT", limit=10)
    action_results = eliza_memory.recall(type_="ACTION_RESULT", limit=5)
    if not memories and not action_results:
        print("\nNo events in Eliza memory yet.")
        return
    if memories:
        print(f"\nRecent payment events ({len(memories)}):")
        for m in memories:
            print(f"  [{m['created_at']}] [{m['agent']}] {m['content']}")
    if action_results:
        print(f"\nRecent Action results ({len(action_results)}):")
        for m in action_results:
            print(f"  [{m['created_at']}] [{m['agent']}] {m['content']}")


def show_pending_orders() -> None:
    """List all orders stuck in Pending, using the Provider for enrichment."""
    state = eliza_memory.get_agent_state()
    ctx = provider.get(state)  # queue-level summary
    pending = ctx.get("pending_orders", [])
    if not pending:
        print("\nNo orders currently in Pending status.")
        return
    print(f"\nPending orders ({len(pending)}):")
    for o in pending:
        print(
            f"  ⏳ {o['order_id']} | "
            f"${o.get('amount_usd') or 0:.2f} | "
            f"{o.get('customer_email') or '(no email)'} | "
            f"created {o['created_at']}"
        )
        if o.get("kaito_tx_id"):
            print(f"     Kaito TX: {o['kaito_tx_id']}")


def show_security_context() -> None:
    """Show recent security events — for Monitor Agent visibility."""
    from eliza_providers import security_context as sc_provider
    state = eliza_memory.get_agent_state()
    ctx = sc_provider.get(state, {"since_minutes": 60})
    print(f"\nSecurity Context (last 60 min):")
    print(f"  Auth errors      : {ctx['auth_error_count']}")
    print(f"  Failed txs       : {ctx['failed_tx_count']}")
    print(f"  Self-healing     : {'⚠️  TRIGGERED' if ctx['self_healing_triggered'] else 'Not triggered'}")
    if ctx["recent_auth_errors"]:
        print(f"\n  Recent auth errors:")
        for e in ctx["recent_auth_errors"]:
            print(f"    [{e['created_at']}] {e['agent']} → {e['endpoint']} ({e['status_code']})")


# ─── Order Context Handlers ───────────────────────────────────────────────────

def _handle_order_context(ctx: dict) -> None:
    """Process a single-order Provider context dict."""
    if not ctx.get("found"):
        order_id = ctx.get("order_id", "?")
        print(
            f"\nOrder '{order_id}' not found in Eliza memory.\n"
            "Note: Only orders processed through the ElizaOS Order Loop are tracked.\n"
            "If the order is very recent, the Shop Agent may still be syncing — "
            "try again in a moment."
        )
        return

    _print_order_from_ctx(ctx)

    if ctx.get("delivery_unlocked") and ctx.get("delivery_info"):
        _show_delivery_info(ctx["delivery_info"])

    elif ctx.get("is_pending"):
        _handle_pending_via_action(ctx)

    elif ctx["status"] in ("Failed", "Expired"):
        print(
            f"\n  ⚠️  This order's payment was not completed.\n"
            "  Please contact us at chrishowell@howell-forge.com to arrange a new payment."
        )


def _handle_email_context(ctx: dict) -> None:
    """Process a multi-order Provider context dict (email lookup)."""
    if ctx.get("total", 0) == 0:
        email = ctx.get("email", "?")
        print(
            f"\nNo orders found in Eliza memory for: {email}\n"
            "Tip: Only orders processed through the ElizaOS Order Loop appear here."
        )
        return

    email = ctx.get("email", "?")
    total = ctx["total"]
    paid = ctx.get("paid_count", 0)
    pending = ctx.get("pending_count", 0)
    print(f"\nOrders for {email} — {total} found ({paid} paid, {pending} pending):")

    for order_ctx in ctx.get("orders", []):
        single_ctx = {
            "found": True,
            "order_id": order_ctx.get("order_id"),
            "order": order_ctx,
            "status": order_ctx.get("status"),
            "is_paid": order_ctx.get("is_paid", False),
            "delivery_unlocked": order_ctx.get("delivery_unlocked", False),
            "is_pending": order_ctx.get("is_pending", False),
            "has_payment_uri": bool(order_ctx.get("payment_uri")),
            "tx_hash": order_ctx.get("tx_hash"),
            "delivery_info": order_ctx.get("delivery_info"),
        }
        _print_order_from_ctx(single_ctx)
        if single_ctx["delivery_unlocked"] and single_ctx["delivery_info"]:
            _show_delivery_info(single_ctx["delivery_info"])
        elif single_ctx["is_pending"]:
            _handle_pending_via_action(single_ctx)


# ─── Display Helpers ──────────────────────────────────────────────────────────

def _print_order_from_ctx(ctx: dict) -> None:
    """Print an order record from a Provider context dict."""
    order: dict = ctx.get("order") or {}
    status: str = ctx.get("status", order.get("status", "?"))

    icons = {
        "PAID": "💚", "Success": "✅", "Pending": "⏳",
        "Failed": "❌", "Expired": "⚠️", "Processing": "🔄",
    }
    icon = icons.get(status, "❓")
    amount = (
        f"${order.get('amount_usd', 0):.2f}"
        if order.get("amount_usd") is not None else "(unknown)"
    )

    print(f"\n  {icon} Order : {ctx.get('order_id')}")
    print(f"     Status : {status}", end="")
    if ctx.get("is_paid"):
        print("  ← delivery unlocked", end="")
    print()
    print(f"     Amount : {amount}")
    if order.get("customer_email"):
        print(f"     Customer: {order['customer_email']}")
    if order.get("payment_uri") and not ctx.get("is_paid"):
        # Only show payment URI if not yet paid (no need to pay again)
        print(f"     Pay URI : {order['payment_uri']}")
    if ctx.get("tx_hash"):
        print(f"     On-chain: {ctx['tx_hash']}")
    elif order.get("kaito_tx_id") and not ctx.get("is_paid"):
        print(f"     Kaito TX: {order['kaito_tx_id']}")
    print(f"     Created : {order.get('created_at', '-')}")
    print(f"     Updated : {order.get('updated_at', '-')}")


def _show_delivery_info(info: dict) -> None:
    """
    Display shipping / delivery information.
    Only called when order status is PAID — the PAID gate.
    """
    print("\n  ─── Delivery Information (PAID) ───────────────────────")
    print(f"  {info.get('status', 'PAID — delivery unlocked')}")
    if info.get("tx_hash"):
        print(f"  On-chain hash : {info['tx_hash']}")
    if info.get("tracking_number"):
        print(f"  Tracking      : {info['tracking_number']} ({info.get('carrier', '')})")
    else:
        print(f"  Tracking      : Not yet assigned")
    print(f"  Est. ship date: {info.get('estimated_ship_date', '3–5 business days')}")
    if info.get("digital_asset_url"):
        print(f"  Digital asset : {info['digital_asset_url']}")
    print(f"  Questions?      {info.get('contact_for_updates', 'chrishowell@howell-forge.com')}")
    print(f"  Note: {info.get('note', '')}")
    print("  " + "─" * 52)


def _handle_pending_via_action(ctx: dict) -> None:
    """
    Trigger a RefreshPaymentAction for a stuck Pending order.
    Runs the async action in a nested event loop (CLI context).
    """
    order: dict = ctx.get("order") or {}
    order_id: str = ctx.get("order_id") or order.get("order_id", "")

    if not order_id or not order.get("kaito_tx_id"):
        print("\n  ⏳ Order is Pending — no Kaito TX ID on record. Contact support.")
        return

    print(f"\n  ⚡ Order {order_id} is Pending — triggering Kaito status refresh…")

    state = eliza_memory.get_agent_state()
    action_context = {"order_id": order_id, "agent": AGENT_NAME}

    if not refresh_payment.validate(state, action_context):
        print("  ⏳ Order is not in a refreshable state right now.")
        return

    try:
        result = asyncio.run(refresh_payment.handler(state, action_context))
    except Exception as exc:
        log_action_error(
            "REFRESH_PAYMENT", AGENT_NAME, order_id, exc
        )
        print(f"  ⚠️  Refresh failed: {exc}", file=sys.stderr)
        return

    dev_tag = " [DEV]" if result.dev_mode else ""

    if result.success and result.status == "PAID":
        # Re-read Provider to get fresh delivery info
        fresh_ctx = provider.get(state, {"order_id": order_id})
        print(f"  ✅ Payment confirmed{dev_tag}! Order {order_id} is now PAID.")
        if fresh_ctx.get("delivery_info"):
            _show_delivery_info(fresh_ctx["delivery_info"])

    elif result.status in ("Failed", "Expired"):
        print(f"  ❌ Kaito refresh confirmed payment is {result.status}{dev_tag}.")
        print("  Please contact us at chrishowell@howell-forge.com.")

    else:
        print(f"  ⏳ Order {order_id} still Pending on-chain{dev_tag}. Check back later.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python3 customer_service_agent.py <command> [arg]\n"
            "\n"
            "Commands:\n"
            "  order <id|email>  — look up order status (Provider + PAID gate)\n"
            "  faq <topic>       — hours | contact | about | payment | shipping\n"
            "  summary           — recent Order Loop + Action activity\n"
            "  pending           — list all Pending orders\n"
            "  security          — show recent security events\n"
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "order":
        if not arg:
            print(
                "Usage: python3 customer_service_agent.py order <order_id|email>",
                file=sys.stderr,
            )
            sys.exit(1)
        where_is_my_order(arg)

    elif cmd == "faq":
        show_faq(arg or "hours")

    elif cmd == "summary":
        show_memory_summary()

    elif cmd == "pending":
        show_pending_orders()

    elif cmd == "security":
        show_security_context()

    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
