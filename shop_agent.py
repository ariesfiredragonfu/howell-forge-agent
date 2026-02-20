#!/usr/bin/env python3
"""
Shop Agent (ElizaOS Edition) — Kaito + Eliza memory + Action-driven pipeline.

Order Flow (one order):
  ┌──────────────────────────────────────────────────────────────┐
  │  1. Generate Kaito payment URI → unique kaito_tx_id          │
  │  2. Write "Pending" to Eliza memory via Provider             │
  │  3. Loop: call VerifyPaymentAction until PAID / Failed       │
  │     └─ Each call: Kaito status check → Action logs result   │
  │     └─ PAID transition unlocks CS Agent delivery info       │
  │  4. Telegram notification                                    │
  └──────────────────────────────────────────────────────────────┘

All exceptions → eliza_actions.log_action_error() → fortress_errors.log
All auth errors → security_hooks.log_auth_error() → self-healing protocol
"""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
import kaito_engine
import security_hooks
from eliza_actions import (
    VerifyPaymentAction,
    ImportOrderAction,
    log_action_error,
    verify_payment,
    import_order,
)
from eliza_providers import order_state as order_state_provider
from notifications import send_telegram_alert
from order_queue import (
    OrderItem,
    OrderPriority,
    OrderQueue,
    PAYMENT_POLL_INTERVAL,
    PAYMENT_TIMEOUT,
)

AGENT_NAME = "SHOP_AGENT"
STRIPE_KEY_PATH = Path.home() / ".config" / "cursor-stripe-secret-key"
STRIPE_API = "https://api.stripe.com/v1"

# PAID statuses — any of these means delivery is unlocked
PAID_STATUSES = frozenset({"PAID", "Success"})


# ─── Stripe helpers ───────────────────────────────────────────────────────────

def _get_stripe_key() -> Optional[str]:
    if not STRIPE_KEY_PATH.exists():
        return None
    key = STRIPE_KEY_PATH.read_text().strip()
    return key or None


def _stripe_get(endpoint: str, params: Optional[dict] = None) -> dict:
    """
    Authenticated GET against the Stripe API.
    Routes 401/403 through security_hooks and logs to fortress_errors.log.
    """
    key = _get_stripe_key()
    if not key:
        raise ValueError("Stripe key not configured")

    url = f"{STRIPE_API}/{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "Howell-Forge-ShopAgent/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            security_hooks.log_auth_error(
                AGENT_NAME, f"stripe/{endpoint}", e.code, str(e)
            )
            log_action_error(
                "STRIPE_GET", AGENT_NAME, None, e, endpoint=f"stripe/{endpoint}"
            )
        raise


# ─── Core Order Processor ─────────────────────────────────────────────────────

async def process_order(item: OrderItem) -> None:
    """
    Async worker callback — injected into OrderQueue.

    Drives one order through the complete Kaito payment flow using
    ElizaOS Actions.  The VerifyPaymentAction owns all status transitions;
    this function owns the retry loop timing and Telegram notifications.
    """
    order_id = item.order_id
    state = eliza_memory.get_agent_state()
    _log(f"Processing order {order_id} (${item.amount_usd:.2f})")

    # ── Step 1: Generate Kaito payment URI ────────────────────────────────────
    try:
        payment_info = kaito_engine.generate_payment_uri(
            order_id=order_id,
            amount_usd=item.amount_usd,
            customer_email=item.customer_email or None,
            memo=f"Howell Forge Order {order_id}",
        )
    except kaito_engine.KaitoAPIError as exc:
        if exc.is_auth_error:
            security_hooks.log_auth_error(
                AGENT_NAME, exc.endpoint or "kaito/generate", exc.status_code, str(exc)
            )
        log_action_error("GENERATE_URI", AGENT_NAME, order_id, exc)
        security_hooks.log_failed_transaction(AGENT_NAME, order_id, str(exc))
        _record_failure(order_id, item, f"Kaito URI generation failed: {exc}")
        send_telegram_alert(
            f"❌ [SHOP] Order {order_id} failed at payment URI generation: {exc}"
        )
        return

    kaito_tx_id: str = payment_info["kaito_tx_id"]
    payment_uri: str = payment_info["payment_uri"]
    dev_mode: bool = payment_info.get("dev_mode", False)
    dev_tag = " [DEV]" if dev_mode else ""

    # ── Step 2: Write "Pending" to Eliza memory ───────────────────────────────
    eliza_memory.upsert_order(
        order_id=order_id,
        status="Pending",
        customer_id=item.metadata.get("customer_id"),
        customer_email=item.customer_email or None,
        amount_usd=item.amount_usd,
        payment_uri=payment_uri,
        kaito_tx_id=kaito_tx_id,
        raw_data={"kaito": payment_info, "meta": item.metadata},
    )
    eliza_memory.remember(
        AGENT_NAME,
        "PAYMENT_EVENT",
        f"Order {order_id} Pending — Kaito tx {kaito_tx_id}{dev_tag}",
        {"order_id": order_id, "status": "Pending", "kaito_tx_id": kaito_tx_id},
    )
    _log(f"{dev_tag} Order {order_id} pending — tx_id={kaito_tx_id}")

    # ── Step 3: Poll via VerifyPaymentAction until PAID / terminal ────────────
    action_context = {
        "order_id": order_id,
        "agent": AGENT_NAME,
    }
    deadline = time.monotonic() + PAYMENT_TIMEOUT
    final_status = "Pending"

    while time.monotonic() < deadline:
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)

        # Guard: re-read from Provider so we catch any external status change
        ctx = order_state_provider.get(state, {"order_id": order_id})
        if ctx.get("is_paid"):
            final_status = "PAID"
            break
        if not ctx.get("is_pending"):
            # Status changed externally to something terminal (Failed/Expired)
            final_status = ctx.get("status", "Failed")
            break

        # Validate that the Action can still run (order still Pending + has tx_id)
        if not verify_payment.validate(state, action_context):
            final_status = eliza_memory.get_order(order_id).get("status", "Failed")
            break

        # ── Invoke VerifyPaymentAction ─────────────────────────────────────
        try:
            result = await verify_payment.handler(state, action_context)
        except kaito_engine.KaitoAPIError as exc:
            # Auth errors already handled inside Action.handler(); keep polling
            _log(
                f"VerifyPaymentAction KaitoAPIError on {order_id} "
                f"(code={exc.status_code}) — will retry",
                err=True,
            )
            continue
        except Exception as exc:
            # Unexpected error — let OrderQueue retry logic handle
            _log(f"VerifyPaymentAction unexpected error on {order_id}: {exc}", err=True)
            raise

        final_status = result.status

        if result.success and result.status == "PAID":
            _log(f"Order {order_id} → PAID{dev_tag} (hash={result.tx_hash})")
            break

        if result.status in ("Failed", "Expired"):
            _log(f"Order {order_id} → {result.status}{dev_tag}")
            break

        # Still Pending — log confirmations and keep looping
        confirmations = result.data.get("confirmations", 0)
        _log(f"Order {order_id} still Pending ({confirmations} confirmations){dev_tag}")

    else:
        _log(f"Order {order_id} timed out after {PAYMENT_TIMEOUT}s — left as Pending")

    # ── Step 4: Final Telegram notification ───────────────────────────────────
    # Note: VerifyPaymentAction already sends a Telegram on PAID transition.
    # We only send additional messages for non-PAID terminal states.
    if final_status in ("Failed", "Expired"):
        send_telegram_alert(
            f"❌ [SHOP] Order {order_id} {final_status} "
            f"(${item.amount_usd:.2f}){dev_tag}"
        )
        _log(f"Order {order_id} {final_status.upper()} ✗")
    elif final_status == "PAID":
        _log(f"Order {order_id} SUCCESS — delivery info now unlocked ✓")
    else:
        _log(f"Order {order_id} left as Pending (will retry on next sync)")


def _record_failure(order_id: str, item: OrderItem, detail: str) -> None:
    """Write Failed record to Eliza memory and emit a biofeedback constraint."""
    eliza_memory.upsert_order(
        order_id=order_id,
        status="Failed",
        customer_email=item.customer_email or None,
        amount_usd=item.amount_usd,
        raw_data={"error": detail},
    )
    eliza_memory.remember(
        AGENT_NAME,
        "PAYMENT_EVENT",
        f"Order {order_id} FAILED: {detail}",
        {"order_id": order_id, "status": "Failed"},
    )
    biofeedback.append_constraint(AGENT_NAME, f"Order {order_id} failed: {detail}")


# ─── Stripe Sync ──────────────────────────────────────────────────────────────

async def sync_stripe_orders(queue: OrderQueue, limit: int = 20) -> int:
    """
    Pull recent Stripe payment intents and enqueue any not yet in Eliza memory.
    Stripe-succeeded orders use ImportOrderAction to enter as PAID immediately.
    Returns count of newly enqueued (Kaito-flow) orders.
    """
    if not _get_stripe_key():
        _log("Stripe key not found — skipping sync", err=True)
        return 0

    try:
        data = _stripe_get("payment_intents", {"limit": str(limit)})
    except (urllib.error.URLError, ValueError) as exc:
        log_action_error("STRIPE_SYNC", AGENT_NAME, None, exc)
        _log(f"Stripe sync failed: {exc}", err=True)
        return 0

    state = eliza_memory.get_agent_state()
    enqueued = 0

    for pi in data.get("data", []):
        order_id: str = pi["id"]

        if eliza_memory.get_order(order_id):
            continue  # Already tracked — skip

        amount_usd: float = pi.get("amount", 0) / 100
        stripe_status: str = pi.get("status", "unknown")

        if stripe_status == "succeeded":
            # Use ImportOrderAction to record as PAID with full audit trail
            try:
                await import_order.handler(
                    state,
                    {
                        "order_id": order_id,
                        "stripe_pi": pi,
                        "agent": AGENT_NAME,
                    },
                )
            except Exception as exc:
                log_action_error("IMPORT_ORDER", AGENT_NAME, order_id, exc)
                _log(f"ImportOrderAction failed for {order_id}: {exc}", err=True)

        elif stripe_status in (
            "requires_payment_method",
            "requires_confirmation",
            "processing",
        ):
            # Needs Kaito payment flow
            item = OrderItem(
                priority=OrderPriority.NORMAL,
                order_id=order_id,
                customer_email="",
                amount_usd=amount_usd,
                metadata={
                    "customer_id": pi.get("customer"),
                    "stripe_status": stripe_status,
                },
            )
            await queue.enqueue(item)
            enqueued += 1

        elif stripe_status == "canceled":
            eliza_memory.upsert_order(
                order_id=order_id,
                status="Failed",
                customer_id=pi.get("customer"),
                amount_usd=amount_usd,
                raw_data={"stripe": pi},
            )

    return enqueued


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str, err: bool = False) -> None:
    target = sys.stderr if err else sys.stdout
    print(f"[{AGENT_NAME}] {msg}", file=target, flush=True)


# ─── CLI entry point ──────────────────────────────────────────────────────────

async def _cli_main() -> None:
    queue = OrderQueue(processor=process_order)
    await queue.start(num_workers=3)
    enqueued = await sync_stripe_orders(queue, limit=20)
    print(f"[{AGENT_NAME}] Enqueued {enqueued} new orders")
    await queue.join()
    await queue.stop()


def main() -> None:
    asyncio.run(_cli_main())


if __name__ == "__main__":
    main()
