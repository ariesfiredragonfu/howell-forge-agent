#!/usr/bin/env python3
"""
Vault Client — WireGuard-aware local secret vault for the Security Handshake.

Provides self-healing "Environment Sync" logic:
  1. Check local vault directory (~/.config/howell-forge-vault/)
  2. Fall back to ~/.config/cursor-* files
  3. If WireGuard is active → attempt remote vault endpoint (VPN-internal)
  4. If all sources fail → return None so the Security Agent escalates to human

Known "Environment Sync" secret keys:
  kaito_api_key       → ~/.config/cursor-kaito-config  (json field: api_key)
  stripe_secret_key   → ~/.config/cursor-stripe-secret-key
  telegram_webhook    → ~/.config/cursor-zapier-telegram-webhook
  github_token        → ~/.config/cursor-github-mcp-token

WireGuard remote vault:
  If wg0/wg1 interface is up, the vault endpoint is readable from VPN-internal IP.
  Configure: ~/.config/howell-forge-vault/remote.json
    { "endpoint": "http://10.0.0.1:8200/v1/secret/data/howell-forge", "token": "..." }
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

VAULT_DIR = Path.home() / ".config" / "howell-forge-vault"
REMOTE_CONFIG = VAULT_DIR / "remote.json"

# Mapping: logical secret name → local cursor config file/key
_LOCAL_CONFIG_MAP: dict[str, tuple[Path, Optional[str]]] = {
    "kaito_api_key": (
        Path.home() / ".config" / "cursor-kaito-config",
        "api_key",         # JSON key within the file
    ),
    "kaito_wallet_address": (
        Path.home() / ".config" / "cursor-kaito-config",
        "wallet_address",
    ),
    "stripe_secret_key": (
        Path.home() / ".config" / "cursor-stripe-secret-key",
        None,              # None = read file as plain text
    ),
    "telegram_webhook": (
        Path.home() / ".config" / "cursor-zapier-telegram-webhook",
        None,
    ),
    "github_token": (
        Path.home() / ".config" / "cursor-github-mcp-token",
        None,
    ),
    "stripe_webhook_secret": (
        Path.home() / ".config" / "cursor-stripe-webhook-secret",
        None,
    ),
}


# ─── WireGuard detection ──────────────────────────────────────────────────────

def is_wireguard_active() -> bool:
    """
    Return True if any WireGuard interface is active.
    Checks via `ip link show type wireguard` (no root needed on Linux).
    """
    try:
        result = subprocess.run(
            ["ip", "link", "show", "type", "wireguard"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: check /proc/net/dev for wg* interfaces
    try:
        proc = Path("/proc/net/dev").read_text()
        return any(line.strip().startswith("wg") for line in proc.splitlines())
    except OSError:
        return False


# ─── Local vault ──────────────────────────────────────────────────────────────

def _read_local_vault_file(secret_name: str) -> Optional[str]:
    """Read a secret from ~/.config/howell-forge-vault/{secret_name}."""
    vault_file = VAULT_DIR / secret_name
    if vault_file.exists():
        value = vault_file.read_text().strip()
        return value or None
    return None


def _read_cursor_config(secret_name: str) -> Optional[str]:
    """Read from the ~/.config/cursor-* mapping for this secret."""
    mapping = _LOCAL_CONFIG_MAP.get(secret_name)
    if not mapping:
        return None
    config_path, json_key = mapping
    if not config_path.exists():
        return None
    raw = config_path.read_text().strip()
    if not raw:
        return None
    if json_key is not None:
        try:
            data = json.loads(raw)
            value = data.get(json_key, "")
            return value if value else None
        except json.JSONDecodeError:
            return None
    return raw


# ─── Remote vault (WireGuard VPN internal) ───────────────────────────────────

def _read_remote_vault(secret_name: str) -> Optional[str]:
    """
    Attempt to read a secret from the WireGuard-protected remote vault.

    Requires:
      - WireGuard interface active (is_wireguard_active() == True)
      - ~/.config/howell-forge-vault/remote.json with endpoint + token

    Returns None on any failure (network, auth, config absent).
    """
    if not is_wireguard_active():
        return None
    if not REMOTE_CONFIG.exists():
        return None
    try:
        remote_cfg = json.loads(REMOTE_CONFIG.read_text())
        endpoint: str = remote_cfg.get("endpoint", "").rstrip("/")
        token: str = remote_cfg.get("token", "")
        if not endpoint or not token:
            return None
        url = f"{endpoint}/{secret_name}"
        req = urllib.request.Request(
            url,
            headers={
                "X-Vault-Token": token,
                "User-Agent": "Howell-Forge-VaultClient/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            # HashiCorp Vault KV v2 response shape
            value = (
                data.get("data", {}).get("data", {}).get("value")
                or data.get("data", {}).get(secret_name)
                or data.get(secret_name)
            )
            return str(value).strip() if value else None
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


# ─── Public API ───────────────────────────────────────────────────────────────

class VaultResult:
    """Result of a vault lookup with provenance information."""

    def __init__(
        self,
        secret_name: str,
        value: Optional[str],
        source: str,          # "local_vault" | "cursor_config" | "remote_vault" | "not_found"
    ):
        self.secret_name = secret_name
        self.value = value
        self.source = source
        self.found = value is not None
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def __repr__(self) -> str:
        preview = f"{self.value[:4]}…" if self.value else "None"
        return f"VaultResult(secret={self.secret_name!r}, source={self.source!r}, value={preview})"


def fetch_secret(secret_name: str) -> VaultResult:
    """
    Attempt to retrieve a secret using the three-tier lookup:
      1. Local vault directory
      2. Cursor config files
      3. Remote WireGuard vault (if VPN is active)

    Returns a VaultResult — caller decides whether to escalate if not found.
    """
    # Tier 1: local vault directory
    value = _read_local_vault_file(secret_name)
    if value:
        return VaultResult(secret_name, value, "local_vault")

    # Tier 2: cursor config files
    value = _read_cursor_config(secret_name)
    if value:
        return VaultResult(secret_name, value, "cursor_config")

    # Tier 3: WireGuard remote vault
    value = _read_remote_vault(secret_name)
    if value:
        return VaultResult(secret_name, value, "remote_vault")

    return VaultResult(secret_name, None, "not_found")


def write_secret_to_cursor_config(secret_name: str, value: str) -> bool:
    """
    Write a recovered secret back to the appropriate cursor config file.
    Used by the self-healing logic after a successful vault lookup.
    Returns True if write succeeded.
    """
    mapping = _LOCAL_CONFIG_MAP.get(secret_name)
    if not mapping:
        # Write to generic local vault file
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        (VAULT_DIR / secret_name).write_text(value)
        return True

    config_path, json_key = mapping
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if json_key is None:
        # Plain text file
        config_path.write_text(value)
        return True

    # JSON file — update just the target key
    try:
        existing = json.loads(config_path.read_text()) if config_path.exists() else {}
    except json.JSONDecodeError:
        existing = {}
    existing[json_key] = value
    config_path.write_text(json.dumps(existing, indent=2))
    return True


def diagnose_environment() -> dict:
    """
    Check all known secrets and return a health report.
    Used by the Security Agent to build the Fix Proposal context.
    """
    report: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wireguard_active": is_wireguard_active(),
        "vault_dir_exists": VAULT_DIR.exists(),
        "remote_vault_configured": REMOTE_CONFIG.exists(),
        "secrets": {},
    }
    for name in _LOCAL_CONFIG_MAP:
        result = fetch_secret(name)
        report["secrets"][name] = {
            "found": result.found,
            "source": result.source,
            "preview": f"{result.value[:4]}…" if result.value else None,
        }
    return report
