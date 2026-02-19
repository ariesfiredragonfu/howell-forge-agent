#!/usr/bin/env python3
"""
Customer Service Agent — Layer 1
FAQs, order lookup by email. Uses Stripe (Customer Data source).
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

STRIPE_KEY_PATH = Path.home() / ".config" / "cursor-stripe-secret-key"
STRIPE_API = "https://api.stripe.com/v1"

FAQ = {
    "hours": "Business Hours: Monday - Friday, 9:00 AM - 5:00 PM EST.",
    "contact": "Email: chrishowell@howell-forge.com. Include project details for a faster response.",
    "about": "Howell Forge specializes in high-quality metal parts production with precision and reliability.",
}


def get_key():
    if not STRIPE_KEY_PATH.exists():
        print("Error: Stripe key not found at", STRIPE_KEY_PATH, file=sys.stderr)
        sys.exit(1)
    return STRIPE_KEY_PATH.read_text().strip()


def stripe_get(endpoint: str, params: dict = None) -> dict:
    key = get_key()
    url = f"{STRIPE_API}/{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "Howell-Forge-CustomerService/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def lookup_email(email: str):
    """Find customer and their orders by email."""
    data = stripe_get("customers", {"email": email, "limit": "10"})
    customers = data.get("data", [])
    if not customers:
        print(f"No customer found for: {email}")
        return
    for c in customers:
        name = c.get("name") or "(no name)"
        print(f"\nCustomer: {name} ({c['email']})")
        data = stripe_get("payment_intents", {"customer": c["id"], "limit": "20"})
        intents = data.get("data", [])
        if not intents:
            print("  No orders found.")
            continue
        print(f"  Orders ({len(intents)}):")
        for p in intents:
            amt = p.get("amount", 0) / 100
            curr = p.get("currency", "usd").upper()
            status = p.get("status", "?")
            print(f"    {p['id']} — {amt} {curr} — {status}")


def show_faq(topic: str):
    topic = topic.lower().strip()
    if topic in FAQ:
        print(FAQ[topic])
    else:
        print("Available: hours, contact, about")
        print("Usage: python3 customer_service.py faq <topic>")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 customer_service.py <lookup|faq> [arg]")
        print("  lookup <email>  — find customer and their orders")
        print("  faq <topic>     — hours, contact, about")
        sys.exit(1)
    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd == "lookup":
        if not arg:
            print("Usage: python3 customer_service.py lookup <email>", file=sys.stderr)
            sys.exit(1)
        lookup_email(arg)
    elif cmd == "faq":
        show_faq(arg or "hours")
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
