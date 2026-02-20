#!/usr/bin/env python3
"""
Kaito Stablecoin Engine — Payment URI generation and blockchain monitoring.

Handles the Kaito handshake for every order in the Order Loop:
  1. generate_payment_uri()    → unique URI + kaito_tx_id per order
  2. check_payment_status()    → poll blockchain for "Payment Confirmed"
  3. trigger_status_refresh()  → force refresh a stuck Pending transaction

Config: ~/.config/cursor-kaito-config  (JSON)
  {
    "api_url":         "https://api.kaito.finance/v1",
    "api_key":         "kto_...",
    "wallet_address":  "0x...",
    "network":         "polygon",   // polygon | ethereum | solana
    "dev_mode":        false        // true → simulated responses, no real calls
  }

When config is absent, api_key is empty, or dev_mode=true the engine uses
deterministic simulated responses so development and testing never touch live
blockchain APIs.
"""

import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

KAITO_CONFIG_PATH = Path.home() / ".config" / "cursor-kaito-config"

_DEFAULT_CONFIG: dict = {
    "api_url": "https://api.kaito.finance/v1",
    "api_key": "",
    "wallet_address": "0x0000000000000000000000000000000000000000",
    "network": "polygon",
    "dev_mode": True,
}


# ─── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if KAITO_CONFIG_PATH.exists():
        try:
            raw = json.loads(KAITO_CONFIG_PATH.read_text())
            return {**_DEFAULT_CONFIG, **raw}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_CONFIG)


def _is_dev(cfg: dict) -> bool:
    return cfg.get("dev_mode", True) or not cfg.get("api_key", "")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _kaito_request(
    cfg: dict,
    method: str,
    path: str,
    payload: Optional[dict] = None,
    timeout: int = 15,
) -> dict:
    """
    Perform an authenticated request to the Kaito API.
    Raises KaitoAPIError on HTTP errors or connection failures.
    """
    url = cfg["api_url"].rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload else None
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "Howell-Forge-KaitoEngine/2.0",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise KaitoAPIError(
            e.code,
            f"Kaito API HTTP {e.code} on {path}",
            endpoint=path,
        ) from e
    except urllib.error.URLError as e:
        raise KaitoAPIError(
            0,
            f"Kaito connection error on {path}: {e.reason}",
            endpoint=path,
        ) from e


# ─── Payment URI ──────────────────────────────────────────────────────────────

def generate_payment_uri(
    order_id: str,
    amount_usd: float,
    customer_email: Optional[str] = None,
    memo: Optional[str] = None,
) -> dict:
    """
    Generate a unique Kaito payment URI for an order.

    Returns:
        {
          "payment_uri":  "kaito://...",
          "kaito_tx_id":  "ktx_...",
          "qr_data":      "...",
          "expires_at":   "...",
          "network":      "polygon",
          "dev_mode":     bool
        }

    Raises KaitoAPIError on live-API failures (never raised in dev_mode).
    """
    cfg = _load_config()
    if _is_dev(cfg):
        return _dev_generate_payment_uri(order_id, amount_usd, customer_email, cfg)

    result = _kaito_request(
        cfg,
        "POST",
        "/payments/create",
        {
            "order_id": order_id,
            "amount_usd": amount_usd,
            "customer_email": customer_email or "",
            "wallet": cfg["wallet_address"],
            "network": cfg["network"],
            "memo": memo or f"Howell Forge Order {order_id}",
        },
    )
    result["dev_mode"] = False
    return result


def _dev_generate_payment_uri(
    order_id: str,
    amount_usd: float,
    email: Optional[str],
    cfg: dict,
) -> dict:
    """Deterministic simulated payment URI — safe for dev/testing."""
    seed = f"{order_id}:{amount_usd}:{email or ''}"
    tx_id = "ktx_dev_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    wallet = cfg.get("wallet_address", "0x0000000000000000000000000000000000000000")
    network = cfg.get("network", "polygon")
    uri = (
        f"kaito://{network}/pay"
        f"?to={wallet}"
        f"&amount={amount_usd:.2f}"
        f"&currency=USD"
        f"&order_id={order_id}"
        f"&tx={tx_id}"
    )
    return {
        "payment_uri": uri,
        "kaito_tx_id": tx_id,
        "qr_data": uri,
        "expires_at": "",
        "network": network,
        "dev_mode": True,
    }


# ─── Payment Status ───────────────────────────────────────────────────────────

def check_payment_status(kaito_tx_id: str) -> dict:
    """
    Query the blockchain for a transaction's current status.

    Returns:
        {
          "kaito_tx_id":    "ktx_...",
          "status":         "Confirmed" | "Pending" | "Failed" | "Expired",
          "confirmations":  int,
          "block_hash":     str | None,
          "checked_at":     "...",
          "dev_mode":       bool
        }

    Raises KaitoAPIError on live-API failures.
    """
    cfg = _load_config()
    if _is_dev(cfg) or kaito_tx_id.startswith("ktx_dev_"):
        return _dev_check_status(kaito_tx_id)

    result = _kaito_request(cfg, "GET", f"/payments/{kaito_tx_id}/status")
    result["checked_at"] = _now()
    result["dev_mode"] = False
    return result


def _dev_check_status(kaito_tx_id: str) -> dict:
    """
    Simulated blockchain status check.
    Deterministic: tx_ids whose last hex char is in {0,2,4,6,8,a,c,e}
    are "Confirmed"; the rest are "Pending".
    """
    last_char = kaito_tx_id[-1].lower() if kaito_tx_id else "1"
    confirmed = last_char in "02468ace"
    return {
        "kaito_tx_id": kaito_tx_id,
        "status": "Confirmed" if confirmed else "Pending",
        "confirmations": 6 if confirmed else 0,
        "block_hash": ("0xdevblock_" + kaito_tx_id[-8:]) if confirmed else None,
        "checked_at": _now(),
        "dev_mode": True,
    }


# ─── Status Refresh ───────────────────────────────────────────────────────────

def trigger_status_refresh(kaito_tx_id: str, order_id: Optional[str] = None) -> dict:
    """
    Force a re-check of a stuck Pending transaction on the Kaito engine.
    Called by the Customer Service Agent when an order is stuck in Pending.

    Returns fresh status dict with an extra "refreshed": True field.
    Raises KaitoAPIError on live-API failures.
    """
    cfg = _load_config()
    if _is_dev(cfg) or kaito_tx_id.startswith("ktx_dev_"):
        result = _dev_check_status(kaito_tx_id)
        result["refreshed"] = True
        return result

    result = _kaito_request(
        cfg,
        "POST",
        f"/payments/{kaito_tx_id}/refresh",
        {
            "kaito_tx_id": kaito_tx_id,
            "order_id": order_id or "",
            "force_recheck": True,
        },
        timeout=20,
    )
    result["refreshed"] = True
    result["checked_at"] = _now()
    result["dev_mode"] = False
    return result


# ─── Errors ───────────────────────────────────────────────────────────────────

class KaitoAPIError(Exception):
    """Raised when the Kaito API returns an error or is unreachable."""

    def __init__(self, status_code: int, message: str, endpoint: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.is_auth_error = status_code in (401, 403)

    def __repr__(self) -> str:
        return f"KaitoAPIError(status={self.status_code}, endpoint={self.endpoint})"
