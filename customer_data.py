#!/usr/bin/env python3
"""
Customer Data Agent — Layer 1
Fetches customer and order data from Stripe. Provides a simple CLI for the crew.
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

STRIPE_KEY_PATH = Path.home() / ".config" / "cursor-stripe-secret-key"
STRIPE_API = "https://api.stripe.com/v1"


def get_key():
    """Load Stripe secret key."""
    if not STRIPE_KEY_PATH.exists():
        print("Error: Stripe key not found at", STRIPE_KEY_PATH, file=sys.stderr)
        sys.exit(1)
    return STRIPE_KEY_PATH.read_text().strip()


def stripe_get(endpoint: str, params: dict = None) -> dict:
    """GET request to Stripe API. Returns parsed JSON."""
    key = get_key()
    url = f"{STRIPE_API}/{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "Howell-Forge-CustomerData/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def list_customers(limit: int = 10):
    """Fetch and print customers from Stripe."""
    data = stripe_get("customers", {"limit": str(limit)})
    customers = data.get("data", [])
    if not customers:
        print("No customers found.")
        return
    print(f"Customers (up to {limit}):")
    for c in customers:
        name = c.get("name") or "(no name)"
        email = c.get("email") or "(no email)"
        print(f"  {c['id']} | {name} | {email}")


def list_orders(limit: int = 10):
    """Fetch and print payment intents (orders) from Stripe."""
    data = stripe_get("payment_intents", {"limit": str(limit)})
    intents = data.get("data", [])
    if not intents:
        print("No payment intents found.")
        return
    print(f"Payment intents (up to {limit}):")
    for p in intents:
        amt = p.get("amount", 0) / 100
        curr = p.get("currency", "usd").upper()
        status = p.get("status", "?")
        cid = p.get("customer") or "(guest)"
        print(f"  {p['id']} | {amt} {curr} | {status} | customer: {cid}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 customer_data.py <customers|orders> [limit]")
        print("  customers  — list Stripe customers")
        print("  orders     — list Stripe payment intents")
        sys.exit(1)
    cmd = sys.argv[1].lower()
    limit = 10
    if len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            print("Note: invalid limit, using 10", file=sys.stderr)
    limit = min(max(limit, 1), 100)
    if cmd == "customers":
        list_customers(limit)
    elif cmd == "orders":
        list_orders(limit)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
