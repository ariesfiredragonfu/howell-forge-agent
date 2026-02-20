#!/usr/bin/env python3
"""
forge_hash.py — SHA-256 fingerprinting for Forge output files.

Every STEP, STL, and G-code file gets hashed immediately after generation.
The hash serves as:
  1. Integrity fingerprint — detect any post-generation tampering
  2. On-chain anchor — pushed to Kaito/Polygon as immutable proof-of-design
  3. Dashboard display — operator sees the hash before approving production

On-chain push:
  When Kaito payments are LIVE, call push_hash_to_chain() and the hash
  is recorded in the Polygon transaction metadata, linking the physical
  part to its exact digital design file.

Usage:
    from forge_hash import hash_forge_outputs, push_hash_to_chain

    hashes = hash_forge_outputs(step_path, gcode_path, stl_path)
    # → {"step_sha256": "abc...", "gcode_sha256": "def...", ...}

    receipt = push_hash_to_chain(hashes, order_id)
    # → {"tx_hash": "0x...", "status": "pending"} or {"status": "dev_mode"}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── Core hash function (user's snippet, extended) ────────────────────────────

def generate_file_hash(filepath: str | Path) -> str:
    """
    SHA-256 hash of a file's raw bytes.
    Reads the entire file — suitable for STEP/STL/G-code (typically < 5 MB).
    Returns lowercase hex string (64 chars).
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Cannot hash — file not found: {path}")
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def hash_forge_outputs(
    step_path:  Optional[Path] = None,
    gcode_path: Optional[Path] = None,
    stl_path:   Optional[Path] = None,
) -> dict:
    """
    Hash all forge output files that exist.
    Returns a dict with sha256 values and metadata.
    Missing files are skipped (not an error — V1 may not have all three).

    Return shape:
        {
          "step_sha256":  "abc...",
          "gcode_sha256": "def...",
          "stl_sha256":   "ghi...",
          "manifest_sha256": "jkl...",   # hash of the combined manifest
          "hashed_at": "2026-...",
          "files_hashed": ["part.step", "part.gcode", "part.stl"],
        }
    """
    result: dict = {
        "step_sha256":      None,
        "gcode_sha256":     None,
        "stl_sha256":       None,
        "manifest_sha256":  None,
        "hashed_at":        datetime.now(timezone.utc).isoformat(),
        "files_hashed":     [],
        "on_chain":         False,
        "chain_tx":         None,
    }

    for key, path in [
        ("step_sha256",  step_path),
        ("gcode_sha256", gcode_path),
        ("stl_sha256",   stl_path),
    ]:
        if path and Path(path).exists():
            result[key] = generate_file_hash(path)
            result["files_hashed"].append(Path(path).name)
            print(f"[HASH] {Path(path).name}: {result[key][:16]}…")

    # Manifest hash — hash of the combined hashes (tamper-evident chain)
    manifest = json.dumps({
        "step":  result["step_sha256"],
        "gcode": result["gcode_sha256"],
        "stl":   result["stl_sha256"],
    }, sort_keys=True)
    result["manifest_sha256"] = hashlib.sha256(manifest.encode()).hexdigest()
    print(f"[HASH] manifest: {result['manifest_sha256'][:16]}…")

    return result


# ─── On-chain push via Kaito ──────────────────────────────────────────────────

def push_hash_to_chain(hashes: dict, order_id: str) -> dict:
    """
    Record the manifest hash on-chain via Kaito/Polygon.

    In DEV mode (feature gate not LIVE), logs the hash locally and returns
    a dev receipt — no real transaction is sent.

    In LIVE mode, calls kaito_engine to embed the manifest_sha256 in a
    zero-value Polygon transaction as OP_RETURN data.

    Returns:
        {
          "status":   "dev_mode" | "pending" | "confirmed" | "error",
          "tx_hash":  "0x..." | None,
          "manifest": "abc...",
          "order_id": "order_xyz",
        }
    """
    manifest = hashes.get("manifest_sha256", "")

    # Check feature gate
    try:
        from eliza_providers import feature_status
        gate = feature_status.get(context={"feature_name": "Kaito Payments"})
        is_live = gate.get("is_live", False)
    except Exception:
        is_live = False

    if not is_live:
        print(f"[HASH] Kaito not LIVE — hash logged locally (dev mode)")
        print(f"[HASH] manifest_sha256={manifest}")
        return {
            "status":   "dev_mode",
            "tx_hash":  None,
            "manifest": manifest,
            "order_id": order_id,
            "note":     "Set Kaito Payments feature to LIVE to push on-chain",
        }

    # LIVE: push via kaito_engine
    try:
        import kaito_engine
        result = kaito_engine.push_data_hash(order_id=order_id, data_hash=manifest)
        print(f"[HASH] ✓ On-chain: tx={result.get('tx_hash', '?')}")
        return {
            "status":   result.get("status", "pending"),
            "tx_hash":  result.get("tx_hash"),
            "manifest": manifest,
            "order_id": order_id,
        }
    except AttributeError:
        # kaito_engine doesn't have push_data_hash yet — log and return
        print(f"[HASH] kaito_engine.push_data_hash not implemented — hash logged")
        return {
            "status":   "not_implemented",
            "tx_hash":  None,
            "manifest": manifest,
            "order_id": order_id,
            "note":     "Implement kaito_engine.push_data_hash() to go on-chain",
        }
    except Exception as exc:
        return {
            "status":   "error",
            "tx_hash":  None,
            "manifest": manifest,
            "order_id": order_id,
            "error":    str(exc)[:200],
        }


# ─── CLI smoke test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    paths = [Path(p) for p in sys.argv[1:] if Path(p).exists()]
    if not paths:
        print("Usage: python3 forge_hash.py <file1> [file2] ...")
        sys.exit(1)

    step  = next((p for p in paths if p.suffix == ".step"),  None)
    gcode = next((p for p in paths if p.suffix == ".gcode"), None)
    stl   = next((p for p in paths if p.suffix == ".stl"),   None)

    hashes = hash_forge_outputs(step, gcode, stl)
    print(json.dumps(hashes, indent=2))
