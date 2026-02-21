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
import math
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

# ─── Circuit Breaker ──────────────────────────────────────────────────────────
#
# States:   CLOSED    — normal operation
#           OPEN      — all calls rejected; Telegram alert + fortress log entry
#           HALF_OPEN — probe mode; HALF_OPEN_PROBES successes → CLOSED,
#                       any failure → back to OPEN
#
# Trip on:  429 (rate limit), 500/502/503/504 (server errors), 0 (connection)
# Ignore:   400 (bad request), 401/403 (auth — handled by security handshake),
#           404, 409 (conflict) — these are terminal logic errors, not infra

_CB_TRIP_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_CB_TERMINAL_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 409, 410})

_CB_STATE_FILE    = Path.home() / ".config" / "kaito-cb-state.json"
_CB_FAILURE_THRESHOLD  = 3    # consecutive trippable failures to trip
_CB_RECOVERY_TIMEOUT   = 120  # seconds in OPEN before HALF_OPEN probe
_CB_HALF_OPEN_PROBES   = 3    # consecutive successes needed to re-CLOSE
_CB_FORTRESS_LOG       = Path.home() / "project_docs" / "fortress_errors.log"


def _cb_load() -> dict:
    if _CB_STATE_FILE.exists():
        try:
            return json.loads(_CB_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "state": "CLOSED",
        "consecutive_failures": 0,
        "opened_at": None,
        "probe_successes": 0,
    }


def _cb_save(state: dict) -> None:
    _CB_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CB_STATE_FILE.write_text(json.dumps(state, indent=2))


def _cb_is_open() -> bool:
    """
    Returns True if calls should be blocked.
    Transparently transitions OPEN → HALF_OPEN when recovery timeout elapses.
    """
    s = _cb_load()
    if s["state"] == "OPEN":
        opened_at = s.get("opened_at")
        if opened_at and (time.time() - opened_at) >= _CB_RECOVERY_TIMEOUT:
            s["state"] = "HALF_OPEN"
            s["probe_successes"] = 0
            _cb_save(s)
            print(f"[CB] HALF_OPEN — allowing {_CB_HALF_OPEN_PROBES} probe requests")
            return False
        return True
    return False


def _cb_record_success() -> None:
    s = _cb_load()
    if s["state"] == "HALF_OPEN":
        s["probe_successes"] = s.get("probe_successes", 0) + 1
        if s["probe_successes"] >= _CB_HALF_OPEN_PROBES:
            s.update({"state": "CLOSED", "consecutive_failures": 0,
                       "opened_at": None, "probe_successes": 0})
            print(f"[CB] CLOSED — {_CB_HALF_OPEN_PROBES} probes succeeded")
    elif s["state"] == "CLOSED":
        s["consecutive_failures"] = 0
    _cb_save(s)


def _cb_record_failure(status_code: int) -> None:
    """Update circuit state for a failed request.  Terminal codes are no-ops."""
    if status_code in _CB_TERMINAL_CODES:
        return
    if status_code not in _CB_TRIP_CODES and status_code != 0:
        return

    s = _cb_load()

    if s["state"] == "HALF_OPEN":
        s.update({"state": "OPEN", "opened_at": time.time(), "probe_successes": 0})
        print(f"[CB] OPEN — probe failed (code={status_code}), resetting recovery timer")
        _cb_save(s)
        _cb_on_open(status_code)
        return

    s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1
    if (s["consecutive_failures"] >= _CB_FAILURE_THRESHOLD
            and s["state"] != "OPEN"):
        s.update({"state": "OPEN", "opened_at": time.time()})
        print(
            f"[CB] OPEN — tripped after {s['consecutive_failures']} "
            f"consecutive failures (code={status_code})"
        )
        _cb_save(s)
        _cb_on_open(status_code)
        return

    _cb_save(s)


def _cb_on_open(status_code: int) -> None:
    """Side-effects when the circuit trips OPEN: alert + fortress log entry."""
    msg = (
        f"🔴 [CIRCUIT BREAKER OPEN] Kaito API unreachable. "
        f"{_CB_FAILURE_THRESHOLD} consecutive failures (last code={status_code}). "
        f"Recovery probe in {_CB_RECOVERY_TIMEOUT}s. "
        f"A Fix Proposal PR will be raised for human review — do NOT auto-switch rails."
    )
    try:
        from notifications import send_telegram_alert
        send_telegram_alert(msg)
    except Exception:
        pass

    # Write directly to fortress_errors.log so fortress_watcher picks it up.
    # Avoids importing eliza_actions (which imports kaito_engine → circular).
    entry = json.dumps({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": "CIRCUIT_BREAKER",
        "agent": "KAITO_ENGINE",
        "order_id": None,
        "error_type": "CircuitBreakerOpen",
        "error_code": status_code,
        "endpoint": "/kaito/circuit-breaker",
        "detail": msg,
    })
    try:
        _CB_FORTRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_CB_FORTRESS_LOG, "a") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def get_circuit_state() -> dict:
    """Return the current circuit breaker state (for health checks / status pages)."""
    return _cb_load()


# ─── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """
    Load Kaito config. Resolution order (later entries override earlier):
      1. Built-in defaults (_DEFAULT_CONFIG)
      2. ~/.config/cursor-kaito-config JSON file
      3. Environment variables — KAITO_API_KEY, WALLET_ADDRESS, POLYGON_RPC_URL
         These win over the file so aria.env is always authoritative.
    """
    import os
    cfg = dict(_DEFAULT_CONFIG)

    if KAITO_CONFIG_PATH.exists():
        try:
            raw = json.loads(KAITO_CONFIG_PATH.read_text())
            cfg.update(raw)
        except (json.JSONDecodeError, OSError):
            pass

    # Environment overrides — aria.env values always win
    env_key    = os.environ.get("KAITO_API_KEY",    "").strip()
    env_wallet = os.environ.get("WALLET_ADDRESS",   "").strip()
    env_rpc    = (os.environ.get("POLYGON_RPC_URL", "")
                  or os.environ.get("ALCHEMY_RPC_URL", "")).strip()

    if env_key:    cfg["api_key"]        = env_key
    if env_wallet: cfg["wallet_address"] = env_wallet
    if env_rpc:    cfg["rpc_url"]        = env_rpc

    # If a real key is present via env, disable dev mode automatically
    if env_key and cfg.get("dev_mode", True):
        cfg["dev_mode"] = False

    return cfg


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

    Checks the circuit breaker before every call:
      - OPEN      → raises KaitoAPIError immediately (no network call)
      - HALF_OPEN → allows probe call through
      - CLOSED    → normal operation

    Updates the circuit breaker on success or failure. Only 429 and 5xx
    responses trip the breaker; 400/409 (terminal logic errors) and
    401/403 (auth errors handled by the Security Handshake) are ignored.

    Raises KaitoAPIError on HTTP errors, connection failures, or open circuit.
    """
    if _cb_is_open():
        raise KaitoAPIError(
            503,
            f"Kaito circuit breaker OPEN — calls suspended for {_CB_RECOVERY_TIMEOUT}s",
            endpoint=path,
        )

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
            result = json.loads(resp.read().decode())
            _cb_record_success()
            return result
    except urllib.error.HTTPError as e:
        _cb_record_failure(e.code)
        raise KaitoAPIError(
            e.code,
            f"Kaito API HTTP {e.code} on {path}",
            endpoint=path,
        ) from e
    except urllib.error.URLError as e:
        _cb_record_failure(0)
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
    """Raised when the Kaito API returns an error, is unreachable, or the circuit is open."""

    def __init__(self, status_code: int, message: str, endpoint: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.is_auth_error = status_code in (401, 403)
        self.is_circuit_open = status_code == 503 and "circuit breaker" in message.lower()
        self.is_rate_limit = status_code == 429
        self.is_server_error = status_code in (500, 502, 503, 504) and not self.is_circuit_open

    def __repr__(self) -> str:
        return f"KaitoAPIError(status={self.status_code}, endpoint={self.endpoint})"
