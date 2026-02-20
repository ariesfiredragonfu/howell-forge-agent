#!/usr/bin/env python3
"""
ElizaOS Actions — Discrete, logged, retryable steps for the Order Loop.

In ElizaOS an Action is an executable unit with:
  validate()  → bool: can this action run right now?
  handler()   → async: executes the logic, returns ActionResult
  name        → string identifier used in logs and fortress_errors.log

Every Action failure — regardless of cause — is written to fortress_errors.log
in JSONL format.  The Monitor Agent tails that file to detect patterns and
trigger the Security Handshake (Step 3 of the self-healing protocol).

Actions defined here:
  VerifyPaymentAction   → calls Kaito, transitions order to PAID state
  RefreshPaymentAction  → force-refreshes a stuck Pending order (CS Agent)
  ImportOrderAction     → records a Stripe-imported order into Eliza memory

fortress_errors.log format (one JSON object per line):
  {
    "timestamp":  "2026-02-20T16:00:00Z",
    "action":     "VERIFY_PAYMENT",
    "agent":      "SHOP_AGENT",
    "order_id":   "pi_xxx",
    "error_type": "KaitoAPIError",
    "error_code": 401,
    "detail":     "...",
    "endpoint":   "/payments/ktx_xxx/status"
  }
"""

from __future__ import annotations

import json
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import biofeedback
import eliza_memory
import kaito_engine
import security_hooks
from eliza_db import get_db
from eliza_memory import AgentState, get_agent_state
from notifications import send_telegram_alert

# ─── Fortress Error Log ────────────────────────────────────────────────────────

FORTRESS_LOG_PATH = Path.home() / "project_docs" / "fortress_errors.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_action_error(
    action_name: str,
    agent: str,
    order_id: Optional[str],
    exc: Exception,
    endpoint: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """
    Write one JSONL breadcrumb to fortress_errors.log.

    The Monitor Agent polls this file to detect:
      - Repeated KaitoAPIError 401/403  → credential rotation trigger
      - Repeated VERIFY_PAYMENT failures → Kaito engine health alert
      - Any exception cluster            → self-healing protocol activation
    """
    FORTRESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    error_code: Optional[int] = None
    if isinstance(exc, kaito_engine.KaitoAPIError):
        error_code = exc.status_code
        endpoint = endpoint or exc.endpoint

    entry: dict = {
        "timestamp": _now_iso(),
        "action": action_name,
        "agent": agent,
        "order_id": order_id,
        "error_type": type(exc).__name__,
        "error_code": error_code,
        "endpoint": endpoint,
        "detail": str(exc),
        **(extra or {}),
    }

    try:
        with open(FORTRESS_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as write_err:
        # Never let logging failure cascade — just warn to stderr
        print(
            f"[FORTRESS-LOG] Could not write error log: {write_err}",
            file=sys.stderr,
        )

    print(
        f"[FORTRESS-LOG] {action_name} error logged "
        f"(order={order_id}, type={type(exc).__name__}, code={error_code})",
        file=sys.stderr,
        flush=True,
    )


def tail_fortress_log(lines: int = 20) -> list[dict]:
    """
    Read the last N entries from fortress_errors.log.
    Returns a list of parsed dicts (empty list if file absent).
    Used by the Monitor Agent for pattern detection.
    """
    if not FORTRESS_LOG_PATH.exists():
        return []
    raw_lines = FORTRESS_LOG_PATH.read_text().splitlines()
    result = []
    for line in reversed(raw_lines[-lines:]):
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


def count_fortress_errors(
    action_name: Optional[str] = None,
    error_code: Optional[int] = None,
    since_minutes: int = 60,
) -> int:
    """
    Count fortress_errors.log entries in the last N minutes.
    Optionally filter by action name or HTTP error code.
    Used by Monitor Agent to decide whether to trigger self-healing.
    """
    if not FORTRESS_LOG_PATH.exists():
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - (since_minutes * 60)
    count = 0

    for entry in tail_fortress_log(lines=500):
        try:
            ts = datetime.strptime(
                entry.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if action_name and entry.get("action") != action_name:
            continue
        if error_code is not None and entry.get("error_code") != error_code:
            continue
        count += 1

    return count


# ─── ActionResult ─────────────────────────────────────────────────────────────


@dataclass
class ActionResult:
    """
    Structured return value from every Action.handler() call.

    success      → True only when the action fully completed its goal
    status       → Eliza order status after the action: "PAID", "Pending", "Failed", etc.
    tx_hash      → On-chain confirmation hash (set when status == "PAID")
    message      → Human-readable summary for logs and Telegram
    data         → Raw response from Kaito or other upstream source
    dev_mode     → True when running against simulated Kaito responses
    """
    success: bool
    status: str
    message: str
    tx_hash: Optional[str] = None
    data: dict = field(default_factory=dict)
    dev_mode: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ─── Base Action ──────────────────────────────────────────────────────────────


class Action(ABC):
    """
    ElizaOS Action base class.

    Subclasses implement _validate() and _handler().
    The public validate() / handler() wrappers add:
      - Fortress error logging on any exception
      - Biofeedback constraint on failure
      - AgentState last_updated sync
    """

    name: str = "base_action"
    description: str = ""

    def validate(
        self,
        state: Optional[AgentState] = None,
        context: Optional[dict] = None,
    ) -> bool:
        """Return True if this action can run given the current state."""
        try:
            return self._validate(state or get_agent_state(), context or {})
        except Exception:
            return False

    async def handler(
        self,
        state: Optional[AgentState] = None,
        context: Optional[dict] = None,
        options: Optional[dict] = None,
    ) -> ActionResult:
        """
        Execute the action.  All exceptions are caught, logged to fortress_errors.log,
        and re-raised so the OrderQueue retry logic can handle them.
        """
        _state = state or get_agent_state()
        _context = context or {}
        order_id: Optional[str] = _context.get("order_id")

        try:
            result = await self._handler(_state, _context, options or {})
            _state.last_updated = _now_iso()
            return result

        except kaito_engine.KaitoAPIError as exc:
            log_action_error(self.name, _context.get("agent", "UNKNOWN"), order_id, exc)
            if exc.is_auth_error:
                security_hooks.log_auth_error(
                    self.name,
                    exc.endpoint or f"{self.name}/unknown",
                    exc.status_code,
                    str(exc),
                )
            biofeedback.append_constraint(
                self.name,
                f"Action {self.name} failed (KaitoAPIError {exc.status_code}): {exc}",
            )
            raise

        except Exception as exc:
            log_action_error(self.name, _context.get("agent", "UNKNOWN"), order_id, exc)
            biofeedback.append_constraint(
                self.name,
                f"Action {self.name} failed ({type(exc).__name__}): {exc}",
            )
            raise

    @abstractmethod
    def _validate(self, state: AgentState, context: dict) -> bool: ...

    @abstractmethod
    async def _handler(
        self, state: AgentState, context: dict, options: dict
    ) -> ActionResult: ...


# ─── VerifyPaymentAction ──────────────────────────────────────────────────────


class VerifyPaymentAction(Action):
    """
    Core payment verification Action for the ElizaOS Order Loop.

    Triggered by the Shop Agent after a Kaito payment URI has been generated.
    Polls the Kaito engine once for the current on-chain status and transitions
    the order to PAID when Kaito returns a confirmation hash.

    The PAID status is the gate that unlocks the Customer Service Agent's
    ability to show shipping / delivery information.

    State transitions:
      Pending  ──[Confirmed]──► PAID        (delivery unlocked)
      Pending  ──[Failed]─────► Failed      (logged to fortress_errors.log)
      Pending  ──[Expired]────► Expired     (logged to fortress_errors.log)
      Pending  ──[Pending]────► Pending     (caller should retry)
      *        ──[APIError]───► raise       (fortress_errors.log + retry)
    """

    name = "VERIFY_PAYMENT"
    description = (
        "Query Kaito for a transaction's on-chain status. "
        "Transitions order to PAID when Kaito confirms the payment hash."
    )

    VERIFIABLE_STATUSES: frozenset[str] = frozenset({"Pending", "Processing"})

    def _validate(self, state: AgentState, context: dict) -> bool:
        order_id = context.get("order_id")
        if not order_id:
            return False
        order = eliza_memory.get_order(order_id)
        if order is None:
            return False
        if not order.get("kaito_tx_id"):
            return False
        return order.get("status", "") in self.VERIFIABLE_STATUSES

    async def _handler(
        self, state: AgentState, context: dict, options: dict
    ) -> ActionResult:
        order_id: str = context["order_id"]
        agent: str = context.get("agent", "SHOP_AGENT")

        order = eliza_memory.get_order(order_id)
        kaito_tx_id: str = order["kaito_tx_id"]

        # ── Call Kaito ────────────────────────────────────────────────────────
        status_info = kaito_engine.check_payment_status(kaito_tx_id)
        kaito_status: str = status_info.get("status", "Pending")
        dev_mode: bool = status_info.get("dev_mode", False)
        dev_tag = " [DEV]" if dev_mode else ""

        # ── PAID transition ───────────────────────────────────────────────────
        if kaito_status == "Confirmed":
            tx_hash: Optional[str] = status_info.get("block_hash")
            confirmations: int = status_info.get("confirmations", 0)

            # Write PAID to Eliza memory — this is the unlock signal
            eliza_memory.upsert_order(
                order_id=order_id,
                status="PAID",
                kaito_tx_id=kaito_tx_id,
                raw_data={
                    "tx_hash": tx_hash,
                    "block_hash": tx_hash,
                    "confirmations": confirmations,
                    "verified_by": self.name,
                    "dev_mode": dev_mode,
                },
            )
            eliza_memory.remember(
                self.name,
                "ACTION_RESULT",
                f"Order {order_id} → PAID{dev_tag} "
                f"(tx_hash={tx_hash}, confirmations={confirmations})",
                {
                    "order_id": order_id,
                    "status": "PAID",
                    "tx_hash": tx_hash,
                    "action": self.name,
                },
            )
            eliza_memory.publish_order_paid(
                order_id,
                {"tx_hash": tx_hash, "confirmations": confirmations, "dev_mode": dev_mode},
            )
            biofeedback.append_reward(
                agent,
                f"Order {order_id} verified PAID via Kaito{dev_tag}",
                kpi="payment_verified_paid",
            )

            msg = (
                f"✅ [VERIFY_PAYMENT] Order {order_id} → PAID{dev_tag} "
                f"(hash={tx_hash or 'N/A'}, confirmations={confirmations})"
            )
            send_telegram_alert(msg)

            return ActionResult(
                success=True,
                status="PAID",
                tx_hash=tx_hash,
                message=msg,
                data=status_info,
                dev_mode=dev_mode,
            )

        # ── Terminal failures ─────────────────────────────────────────────────
        if kaito_status in ("Failed", "Expired"):
            eliza_memory.upsert_order(order_id=order_id, status=kaito_status)
            eliza_memory.remember(
                self.name,
                "ACTION_RESULT",
                f"Order {order_id} terminal: {kaito_status}{dev_tag}",
                {"order_id": order_id, "status": kaito_status, "action": self.name},
            )
            security_hooks.log_failed_transaction(
                agent,
                order_id,
                f"Kaito tx {kaito_tx_id}: {kaito_status}",
            )
            # Also write to fortress_errors.log as a breadcrumb
            log_action_error(
                self.name,
                agent,
                order_id,
                Exception(f"Kaito payment {kaito_status} for tx {kaito_tx_id}"),
                extra={"kaito_status": kaito_status, "kaito_tx_id": kaito_tx_id},
            )
            send_telegram_alert(
                f"❌ [VERIFY_PAYMENT] Order {order_id} {kaito_status}{dev_tag}"
            )
            return ActionResult(
                success=False,
                status=kaito_status,
                message=f"Payment {kaito_status}",
                data=status_info,
                dev_mode=dev_mode,
            )

        # ── Still Pending — caller will retry ─────────────────────────────────
        confirmations = status_info.get("confirmations", 0)
        return ActionResult(
            success=False,
            status="Pending",
            message=f"Still Pending ({confirmations} confirmations){dev_tag}",
            data=status_info,
            dev_mode=dev_mode,
        )


# ─── RefreshPaymentAction ─────────────────────────────────────────────────────


class RefreshPaymentAction(Action):
    """
    Force-refresh a stuck Pending order via the Kaito engine.
    Triggered by the Customer Service Agent when a customer asks "where is my order?"
    and the order is stuck in Pending.

    On Confirmed → transitions to PAID (same as VerifyPaymentAction).
    On still-Pending → returns gracefully (CS Agent shows "check back later").
    """

    name = "REFRESH_PAYMENT"
    description = (
        "Force-refresh a stuck Pending payment via Kaito. "
        "Used by the Customer Service Agent in response to customer enquiries."
    )

    def _validate(self, state: AgentState, context: dict) -> bool:
        order_id = context.get("order_id")
        if not order_id:
            return False
        order = eliza_memory.get_order(order_id)
        return (
            order is not None
            and order.get("status") == "Pending"
            and bool(order.get("kaito_tx_id"))
        )

    async def _handler(
        self, state: AgentState, context: dict, options: dict
    ) -> ActionResult:
        order_id: str = context["order_id"]
        agent: str = context.get("agent", "CUSTOMER_SERVICE_AGENT")

        order = eliza_memory.get_order(order_id)
        kaito_tx_id: str = order["kaito_tx_id"]

        result = kaito_engine.trigger_status_refresh(kaito_tx_id, order_id=order_id)
        kaito_status: str = result.get("status", "Pending")
        dev_mode: bool = result.get("dev_mode", False)
        dev_tag = " [DEV]" if dev_mode else ""

        if kaito_status == "Confirmed":
            tx_hash: Optional[str] = result.get("block_hash")
            eliza_memory.upsert_order(
                order_id=order_id,
                status="PAID",
                kaito_tx_id=kaito_tx_id,
                raw_data={"tx_hash": tx_hash, "block_hash": tx_hash, "refreshed": True},
            )
            eliza_memory.remember(
                self.name,
                "ACTION_RESULT",
                f"CS refresh → Order {order_id} PAID{dev_tag} (tx_hash={tx_hash})",
                {"order_id": order_id, "status": "PAID", "refreshed": True},
            )
            eliza_memory.publish_order_paid(
                order_id,
                {"tx_hash": tx_hash, "refreshed": True, "dev_mode": dev_mode},
            )
            biofeedback.append_reward(
                agent,
                f"CS refresh unlocked PAID for order {order_id}{dev_tag}",
                kpi="cs_refresh_paid",
            )
            return ActionResult(
                success=True,
                status="PAID",
                tx_hash=tx_hash,
                message=f"Refresh confirmed — order {order_id} is now PAID{dev_tag}",
                data=result,
                dev_mode=dev_mode,
            )

        if kaito_status in ("Failed", "Expired"):
            eliza_memory.upsert_order(order_id=order_id, status=kaito_status)
            security_hooks.log_failed_transaction(
                agent, order_id, f"CS refresh: Kaito {kaito_status}"
            )
            log_action_error(
                self.name,
                agent,
                order_id,
                Exception(f"CS refresh: Kaito tx {kaito_tx_id} → {kaito_status}"),
                extra={"kaito_status": kaito_status},
            )
            return ActionResult(
                success=False,
                status=kaito_status,
                message=f"Payment {kaito_status} confirmed by Kaito refresh",
                data=result,
                dev_mode=dev_mode,
            )

        return ActionResult(
            success=False,
            status="Pending",
            message=f"Still Pending on-chain{dev_tag} — check back later",
            data=result,
            dev_mode=dev_mode,
        )


# ─── ImportOrderAction ────────────────────────────────────────────────────────


class ImportOrderAction(Action):
    """
    Record a Stripe-succeeded order into Eliza memory as PAID.
    Used by sync_stripe_orders() when importing already-confirmed payments.
    """

    name = "IMPORT_ORDER"
    description = (
        "Import a Stripe-confirmed order into Eliza memory as PAID. "
        "Used during Stripe sync for orders that were confirmed before the Order Loop ran."
    )

    def _validate(self, state: AgentState, context: dict) -> bool:
        return bool(context.get("order_id")) and bool(context.get("stripe_pi"))

    async def _handler(
        self, state: AgentState, context: dict, options: dict
    ) -> ActionResult:
        order_id: str = context["order_id"]
        pi: dict = context["stripe_pi"]
        agent: str = context.get("agent", "SHOP_AGENT")

        amount_usd = pi.get("amount", 0) / 100
        customer_id = pi.get("customer")

        eliza_memory.upsert_order(
            order_id=order_id,
            status="PAID",
            customer_id=customer_id,
            amount_usd=amount_usd,
            raw_data={"stripe": pi, "source": "stripe_import", "tx_hash": None},
        )
        eliza_memory.remember(
            self.name,
            "ACTION_RESULT",
            f"Stripe order {order_id} imported as PAID (${amount_usd:.2f})",
            {"order_id": order_id, "status": "PAID", "source": "stripe_import"},
        )

        return ActionResult(
            success=True,
            status="PAID",
            message=f"Stripe order {order_id} imported as PAID",
            data={"amount_usd": amount_usd},
        )


# ─── ValidationError ──────────────────────────────────────────────────────────


class ValidationError(Exception):
    """
    Raised by ValidateFeatureAction when a feature is not LIVE or the
    proposed post text fails the entity whitelist check.

    Attributes:
        reason        short machine-readable reason code
        feature_name  the feature that was queried
        status        current feature status (DEV/BETA/LIVE/DEPRECATED)
    """

    def __init__(
        self,
        message: str,
        reason: str = "unknown",
        feature_name: Optional[str] = None,
        status: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.feature_name = feature_name
        self.status = status


# ─── ValidateFeatureAction ────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "eliza-config.json"


def _load_entity_whitelist() -> list[str]:
    """
    Load the entity whitelist from eliza-config.json herald.entity_whitelist.
    Falls back to an empty list so the action passes when no config exists.

    No spacy/nltk required — the whitelist is a list of brand / product terms
    that must appear in the proposed text (≥80% match by count).  When spacy
    becomes a project dependency this function can be upgraded to extract named
    entities from the text and match them against the whitelist.
    """
    try:
        cfg = json.loads(_CONFIG_PATH.read_text())
        return cfg.get("herald", {}).get("entity_whitelist", [])
    except (OSError, json.JSONDecodeError):
        return []


def _entity_match_score(text: str, whitelist: list[str]) -> float:
    """
    Fraction of whitelist terms that appear in the text (case-insensitive).
    A term matches if it appears as a substring of the lowercased text.
    Returns 1.0 when whitelist is empty (no constraint to enforce).
    """
    if not whitelist:
        return 1.0
    text_lower = text.lower()
    matches = sum(1 for term in whitelist if term.lower() in text_lower)
    return matches / len(whitelist)


class ValidateFeatureAction(Action):
    """
    Gate any X / social post on:
      1. Feature status = LIVE  (rejects DEV/BETA/DEPRECATED)
      2. Entity whitelist match ≥ 80%  (guards brand safety / Drift fix)

    On rejection:
      - Raises ValidationError (caller must catch)
      - Logs a constraint to biofeedback (Marketing: Validation Denied)
      - Writes a breadcrumb to fortress_errors.log

    context keys required:
      feature_name        str   feature to gate on (must be LIVE)
      proposed_post_text  str   draft post copy to validate
    """

    name = "VALIDATE_FEATURE"
    description = (
        "Gate Herald posts on feature status (must be LIVE) "
        "and entity whitelist match (≥80%). "
        "Raises ValidationError on any failure."
    )

    ENTITY_MATCH_THRESHOLD: float = 0.80

    def _validate(self, state: AgentState, context: dict) -> bool:
        return bool(context.get("feature_name")) and bool(context.get("proposed_post_text"))

    async def _handler(
        self, state: AgentState, context: dict, options: dict
    ) -> ActionResult:
        feature_name: str = context["feature_name"]
        proposed_text: str = context["proposed_post_text"]
        agent: str = context.get("agent", "HERALD_AGENT")

        # ── 1. Feature status gate ─────────────────────────────────────────
        status: Optional[str] = get_db().get_feature_status(feature_name)

        if status is None:
            msg = f"Feature '{feature_name}' not found in feature_states table"
            biofeedback.append_constraint(
                "MARKETING",
                f"Validation Denied: {feature_name} — unknown feature",
                event_type="marketing_fail",
            )
            log_action_error(
                self.name, agent, None,
                ValidationError(msg, reason="feature_unknown", feature_name=feature_name),
                extra={"feature_name": feature_name, "proposed_status": None},
            )
            raise ValidationError(msg, reason="feature_unknown", feature_name=feature_name)

        if status != "LIVE":
            msg = (
                f"Feature '{feature_name}' is {status} — "
                f"posts only allowed when status = LIVE"
            )
            biofeedback.append_constraint(
                "MARKETING",
                f"Validation Denied: {feature_name} is {status} (not LIVE)",
                event_type="marketing_fail",
            )
            log_action_error(
                self.name, agent, None,
                ValidationError(msg, reason="feature_not_live",
                                feature_name=feature_name, status=status),
                extra={"feature_name": feature_name, "current_status": status},
            )
            raise ValidationError(
                msg, reason="feature_not_live",
                feature_name=feature_name, status=status,
            )

        # ── 2. Entity whitelist check ──────────────────────────────────────
        whitelist = _load_entity_whitelist()
        score = _entity_match_score(proposed_text, whitelist)

        if score < self.ENTITY_MATCH_THRESHOLD:
            pct = int(score * 100)
            threshold_pct = int(self.ENTITY_MATCH_THRESHOLD * 100)
            missing = [t for t in whitelist if t.lower() not in proposed_text.lower()]
            msg = (
                f"Entity density {pct}% < {threshold_pct}% threshold. "
                f"Missing terms: {missing}"
            )
            biofeedback.append_constraint(
                "MARKETING",
                f"Validation Denied: entity density {pct}% < {threshold_pct}% for '{feature_name}'",
                event_type="marketing_fail",
            )
            log_action_error(
                self.name, agent, None,
                ValidationError(msg, reason="entity_density_low",
                                feature_name=feature_name, status=status),
                extra={
                    "feature_name": feature_name,
                    "entity_match_pct": pct,
                    "threshold_pct": threshold_pct,
                    "missing_entities": missing,
                },
            )
            raise ValidationError(
                msg, reason="entity_density_low",
                feature_name=feature_name, status=status,
            )

        # ── Pass ──────────────────────────────────────────────────────────
        return ActionResult(
            success=True,
            status="VALIDATED",
            message=(
                f"Feature '{feature_name}' is LIVE. "
                f"Entity match: {int(score * 100)}%. Post approved."
            ),
            data={
                "feature_name": feature_name,
                "feature_status": status,
                "entity_match_pct": int(score * 100),
                "whitelist_used": whitelist,
            },
        )


# ─── Module-level singletons ──────────────────────────────────────────────────

verify_payment = VerifyPaymentAction()
refresh_payment = RefreshPaymentAction()
import_order = ImportOrderAction()
validate_feature = ValidateFeatureAction()
