# 🚨 Security Fix Proposal — Rotate Kaito API Key

| Field | Value |
|---|---|
| **Severity** | `CRITICAL` |
| **Error type** | `VERIFY_PAYMENT` |
| **Triggered by agent** | `SHOP_AGENT` |
| **Order ID** | `pi_handshake_test_001` |
| **Detected at** | `2026-02-20T17:00:17Z` |
| **Generated at** | `2026-02-20T17:00:21Z` |

## Root Cause

The Kaito Stablecoin Engine returned HTTP **401** on endpoint `/payments/create`.
This indicates the Kaito API key stored in `~/.config/cursor-kaito-config`
is **expired, revoked, or missing**.

Detail from fortress_errors.log: `Kaito API HTTP 401 — invalid or expired API key`

Repeated 401/403 errors from `SHOP_AGENT` indicate this is not a transient
network issue — the credential itself is invalid.


## Files Affected

- `~/.config/cursor-kaito-config`
- `howell-forge-agent/kaito_engine.py`

## Proposed Code Change

```diff
--- a/.config/cursor-kaito-config
+++ b/.config/cursor-kaito-config
@@ -1,6 +1,6 @@
 {
   "api_url": "https://api.kaito.finance/v1",
-  "api_key": "",
+  "api_key": "<YOUR_NEW_KAITO_API_KEY>",
   "wallet_address": "0x...",
   "network": "polygon",
-  "dev_mode": true
+  "dev_mode": false
 }

```

## Remediation Steps

1. Log in to your Kaito Finance dashboard (https://app.kaito.finance)
2. Navigate to **API Keys** → **Rotate Key** or **Create New Key**
3. Copy the new API key
4. Update `~/.config/cursor-kaito-config`: set `"api_key": "<new_key>"`
5. Set `"dev_mode": false` in the same file to re-enable live mode
6. Restart the Order Loop: `python3 run_order_loop.py`
7. Monitor `fortress_errors.log` — auth errors should stop within 2 sync cycles
8. Close this PR once confirmed working

<details>
<summary>Raw fortress_errors.log entry</summary>

```json
{
  "timestamp": "2026-02-20T17:00:17Z",
  "action": "VERIFY_PAYMENT",
  "agent": "SHOP_AGENT",
  "order_id": "pi_handshake_test_001",
  "error_type": "KaitoAPIError",
  "error_code": 401,
  "endpoint": "/payments/create",
  "detail": "Kaito API HTTP 401 \u2014 invalid or expired API key",
  "_severity": "CRITICAL"
}
```

</details>