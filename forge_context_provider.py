#!/usr/bin/env python3
"""
ForgeContextProvider — Live shop-floor snapshot for the ARIA dashboard agent.

Aggregates data from every subsystem into a single JSON object:
  - Orders     : SQLite via eliza_memory (all active + recent completed)
  - Biofeedback: EWMA score + recent event stream from Redis
  - Finances   : USDC + MATIC balance on Polygon via Web3 (Alchemy RPC)
  - Forge state: forge:status Redis key (IDLE / RUNNING / REVIEW / ERROR)
  - Forge runs : Most recent forge_manager output per order (renders, gcode)
  - Security   : Recent security events from SecurityContextProvider
  - System     : Timestamp, uptime marker, provider health

Polygon/Web3 config (set in eliza-config.json or environment):
  ALCHEMY_KEY          Alchemy API key for Polygon mainnet RPC
  KAITO_WALLET         Wallet address to check USDC balance
  USDC_POLYGON         USDC contract on Polygon (default: well-known address)

Designed to be called every 2 seconds by the FastAPI WebSocket broadcaster.
Each call is fully synchronous — no async, no caching between calls.

Usage:
    from forge_context_provider import ForgeContextProvider
    ctx = ForgeContextProvider().snapshot()   # returns dict
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
from eliza_providers import OrderStateProvider, SecurityContextProvider

# ─── Config ───────────────────────────────────────────────────────────────────

_default_orders  = Path.home() / "Hardware_Factory" / "forge_orders"
FORGE_ORDERS_DIR = Path(os.getenv("FORGE_ORDERS_DIR", str(_default_orders)))
FORGE_ORDERS_DIR.mkdir(parents=True, exist_ok=True)
_order_provider    = OrderStateProvider()
_security_provider = SecurityContextProvider()

# Polygon / Web3 config — set these in environment or eliza-config.json
# USDC on Polygon PoS mainnet (official Circle contract)
USDC_POLYGON_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS        = 6   # USDC uses 6 decimals on Polygon

# Minimal ERC-20 ABI — only balanceOf needed
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

# Redis key written by forge_manager_v1 to broadcast machine state
FORGE_STATUS_KEY = "forge:status"


# ─── forge:status Redis helpers ───────────────────────────────────────────────

def set_forge_status(status: str, detail: str = "") -> None:
    """
    Write the current forge machine state to Redis.
    Called by forge_manager_v1 at each workflow step.
    Status values: IDLE | RUNNING | REVIEW | ERROR
    """
    try:
        r = biofeedback._get_redis()
        if r:
            r.set(FORGE_STATUS_KEY, json.dumps({
                "status": status,
                "detail": detail,
                "ts":     datetime.now(timezone.utc).isoformat(),
            }))
    except Exception:
        pass   # forge status is best-effort, never block the workflow


def get_forge_status() -> dict:
    """Read the current forge machine state from Redis."""
    try:
        r = biofeedback._get_redis()
        if r:
            raw = r.get(FORGE_STATUS_KEY)
            if raw:
                return json.loads(raw)
    except Exception:
        pass
    return {"status": "IDLE", "detail": "", "ts": None}


# ─── Polygon / Web3 finance snapshot ─────────────────────────────────────────

def _get_web3() -> "Web3 | None":
    """
    Build a Web3 instance from the configured RPC URL.

    Resolution order (first non-empty value wins):
      1. POLYGON_RPC_URL   — full RPC URL (new, preferred — set in aria.env)
      2. ALCHEMY_RPC_URL   — alias accepted for backward compatibility
      3. ALCHEMY_KEY        — legacy bare key → URL built from it
      4. eliza-config.json polygon.alchemy_key
    """
    try:
        from web3 import Web3

        # 1. Full URL (new style)
        rpc = (os.environ.get("POLYGON_RPC_URL", "")
               or os.environ.get("ALCHEMY_RPC_URL", "")).strip()

        # 2. Bare key (legacy style)
        if not rpc:
            key = os.environ.get("ALCHEMY_KEY", "").strip()
            if not key:
                cfg_path = Path(__file__).parent / "eliza-config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    key = cfg.get("polygon", {}).get("alchemy_key", "")
            if key:
                rpc = f"https://polygon-mainnet.g.alchemy.com/v2/{key}"

        if not rpc:
            return None

        return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
    except ImportError:
        return None
    except Exception:
        return None


def _finances_snapshot() -> dict:
    """
    Query Polygon for MATIC balance and USDC balance of the Kaito wallet.

    - MATIC: native gas token → web3.eth.get_balance() (18 decimals)
    - USDC:  ERC-20 stablecoin → balanceOf() on the USDC contract (6 decimals)

    Note: Gemini's snippet used get_balance() for USDC — that returns MATIC,
    not USDC. USDC requires an ERC-20 contract call. This is the correct version.

    Falls back gracefully if Alchemy key not set or web3 unavailable.
    """
    try:
        import kaito_engine
        cfg = kaito_engine._load_config()
        wallet = cfg.get("wallet_address", "").strip()
    except Exception:
        wallet = os.environ.get("KAITO_WALLET", "").strip()

    if not wallet or wallet == "0x0000000000000000000000000000000000000000":
        return {
            "usdc":    None,
            "matic":   None,
            "wallet":  wallet or None,
            "network": "polygon",
            "note":    "Wallet address not configured",
        }

    w3 = _get_web3()
    if not w3:
        return {
            "usdc":    None,
            "matic":   None,
            "wallet":  wallet,
            "network": "polygon",
            "note":    "Alchemy key not set (ALCHEMY_KEY env or eliza-config.json)",
        }

    from web3 import Web3

    result: dict = {
        "usdc":    None,
        "matic":   None,
        "wallet":  wallet,
        "network": "polygon",
        "note":    None,
    }

    # MATIC balance (native — 18 decimals)
    try:
        matic_wei   = w3.eth.get_balance(Web3.to_checksum_address(wallet))
        result["matic"] = round(matic_wei / 10 ** 18, 6)
    except Exception as exc:
        result["matic_error"] = str(exc)[:80]

    # USDC balance (ERC-20 — 6 decimals)
    try:
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_POLYGON_ADDRESS),
            abi=_ERC20_ABI,
        )
        raw_balance     = usdc.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call()
        result["usdc"]  = round(raw_balance / 10 ** USDC_DECIMALS, 2)
    except Exception as exc:
        result["usdc_error"] = str(exc)[:80]

    return result

# ─── Kaito payment helper ─────────────────────────────────────────────────────

def _fetch_kaito_status(kaito_tx_id: str) -> dict:
    """
    Query kaito_engine for on-chain payment status.
    Returns a safe dict — never raises, circuit-breaker aware.
    """
    try:
        import kaito_engine
        result = kaito_engine.check_payment_status(kaito_tx_id)
        return {
            "tx_id":       kaito_tx_id,
            "status":      result.get("status", "unknown"),
            "confirmed":   result.get("confirmed", False),
            "block_hash":  result.get("block_hash"),
            "network":     result.get("network", "polygon"),
            "error":       None,
        }
    except Exception as exc:
        return {
            "tx_id":   kaito_tx_id,
            "status":  "unavailable",
            "confirmed": False,
            "block_hash": None,
            "network":  "polygon",
            "error":   str(exc)[:120],
        }


# ─── Forge run artifacts helper ───────────────────────────────────────────────

def _forge_run_info(order_id: str) -> dict:
    """
    Check Hardware_Factory/forge_orders/<order_id>/ for run artifacts.
    Returns paths, bbox, gcode validation result, and render presence.
    """
    order_dir = FORGE_ORDERS_DIR / order_id
    if not order_dir.exists():
        return {"run_exists": False}

    forge_log: dict = {}
    log_path = order_dir / "forge_log.json"
    if log_path.exists():
        try:
            forge_log = json.loads(log_path.read_text())
        except Exception:
            pass

    hashes: dict = {}
    hashes_path = order_dir / "hashes.json"
    if hashes_path.exists():
        try:
            hashes = json.loads(hashes_path.read_text())
        except Exception:
            pass

    renders = sorted(order_dir.glob("preview_*.png"))

    return {
        "run_exists":      True,
        "order_dir":       str(order_dir),
        "has_step":        (order_dir / "part.step").exists(),
        "has_stl":         (order_dir / "part.stl").exists(),
        "has_gcode":       (order_dir / "part.gcode").exists(),
        "render_count":    len(renders),
        "render_paths":    [str(p) for p in renders],
        "bbox_mm":         forge_log.get("bbox_mm"),
        "gcode_valid":     forge_log.get("gcode_validation", {}).get("ok"),
        "approved":        forge_log.get("approved"),
        "completed_at":    forge_log.get("completed_at"),
        "description":     forge_log.get("description"),
        "hashes": {
            "step_sha256":      hashes.get("step_sha256"),
            "gcode_sha256":     hashes.get("gcode_sha256"),
            "stl_sha256":       hashes.get("stl_sha256"),
            "manifest_sha256":  hashes.get("manifest_sha256"),
            "on_chain":         hashes.get("on_chain", False),
            "chain_tx":         hashes.get("chain", {}).get("tx_hash"),
        },
    }


# ─── Biofeedback snapshot ─────────────────────────────────────────────────────

def _biofeedback_snapshot() -> dict:
    """
    Pull EWMA score + last 20 events from the Redis stream.
    Gracefully degrades if Redis is down.
    """
    score = biofeedback.get_score()

    # Health band based on eliza-config thresholds (high: 3.0, throttle: -2.0)
    if score >= 3.0:
        health = "HIGH"
        color  = "green"
    elif score >= 0.0:
        health = "STABLE"
        color  = "blue"
    elif score >= -2.0:
        health = "DEGRADED"
        color  = "yellow"
    else:
        health = "THROTTLED"
        color  = "red"

    recent_events: list[dict] = []
    try:
        r = biofeedback._get_redis()
        if r:
            raw = r.xrevrange(biofeedback._STREAM_KEY, count=20)
            for _id, fields in raw:
                recent_events.append({
                    "ts":     fields.get("ts", ""),
                    "type":   fields.get("type", ""),
                    "agent":  fields.get("agent", ""),
                    "weight": fields.get("weight", ""),
                })
    except Exception:
        pass

    return {
        "score":          round(score, 4),
        "health":         health,
        "color":          color,
        "recent_events":  recent_events,
    }


# ─── Orders snapshot ──────────────────────────────────────────────────────────

def _orders_snapshot() -> dict:
    """
    Pull all orders from SQLite, enrich with Kaito status and forge run info.
    """
    all_orders = eliza_memory.get_all_orders() if hasattr(eliza_memory, "get_all_orders") else []

    # Fallback: use pending + recent memories if get_all_orders not defined
    if not all_orders:
        all_orders = eliza_memory.get_pending_orders() or []

    enriched = []
    kaito_cache: dict[str, dict] = {}

    for order in all_orders:
        order_id  = order.get("order_id", "")
        tx_id     = order.get("kaito_tx_id")

        # Kaito blockchain status (deduplicated by tx_id)
        kaito_status = None
        if tx_id:
            if tx_id not in kaito_cache:
                kaito_cache[tx_id] = _fetch_kaito_status(tx_id)
            kaito_status = kaito_cache[tx_id]

        enriched.append({
            **order,
            "kaito_status":  kaito_status,
            "forge_run":     _forge_run_info(order_id),
        })

    paid     = [o for o in enriched if o.get("status") in ("PAID", "Success")]
    pending  = [o for o in enriched if o.get("status") in ("Pending", "Processing")]
    prod     = [o for o in enriched if o.get("status") == "in_production"]

    return {
        "total":        len(enriched),
        "paid_count":   len(paid),
        "pending_count": len(pending),
        "in_production_count": len(prod),
        "orders":       enriched,
    }


# ─── ForgeContextProvider ─────────────────────────────────────────────────────

class ForgeContextProvider:
    """
    Assembles a full shop-floor snapshot every call.

    snapshot() → dict, suitable for JSON serialisation.

    Fields:
      timestamp     ISO-8601 UTC
      biofeedback   EWMA score, health band, recent events
      orders        All orders enriched with Kaito + forge run info
      security      Recent security events (last 60 min)
      system        Provider health flags
    """

    def snapshot(self) -> dict:
        ts = datetime.now(timezone.utc).isoformat()

        # Run each subsystem — isolate failures so one bad subsystem
        # doesn't kill the entire context pulse.
        bf_data       = self._safe("biofeedback", _biofeedback_snapshot)
        orders_data   = self._safe("orders",      _orders_snapshot)
        security_data = self._safe("security",    lambda: _security_provider.get().copy())
        finances_data = self._safe("finances",    _finances_snapshot)
        forge_status  = self._safe("forge_status", get_forge_status)

        return {
            "timestamp":    ts,
            "biofeedback":  bf_data,
            "orders":       orders_data,
            "finances":     finances_data,
            "forge_status": forge_status,
            "security":     security_data,
            "system": {
                "provider":   "ForgeContextProvider",
                "version":    "1.1.0",
                "forge_dir":  str(FORGE_ORDERS_DIR),
                "subsystems": {
                    "biofeedback_ok": "provider failed" not in str(bf_data),
                    "orders_ok":      "provider failed" not in str(orders_data),
                    "security_ok":    "provider failed" not in str(security_data),
                    "finances_ok":    "provider failed" not in str(finances_data),
                },
            },
        }

    @staticmethod
    def _safe(name: str, fn) -> dict:
        try:
            return fn()
        except Exception as exc:
            return {"error": f"{name} provider failed: {exc!s}"[:200]}


# ─── CLI smoke test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("ForgeContextProvider — live snapshot:")
    ctx = ForgeContextProvider().snapshot()
    print(json.dumps(ctx, indent=2, default=str))
