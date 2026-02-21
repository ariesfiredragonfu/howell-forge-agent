#!/usr/bin/env python3
"""
aria_voice_agent.py — ARIA's personality, system prompt, and Safety Agent.

ARIA: Autonomous Reactive Industrial Agent — voice interface for Howell Forge.

This module owns:
  1. ARIA_SYSTEM_PROMPT     — full personality injected into every chat/voice call
  2. SafetyAgent            — real-time interrupt logic that runs on partial transcripts
                              BEFORE ARIA responds, checking for collisions, financial
                              errors, and physics failures
  3. build_aria_prompt()    — assembles context-aware prompt for each message
  4. check_safety()         — fast path called on every interim transcript chunk

Safety Agent interrupt hierarchy:
  PRIORITY 1 — Machine collision (toolpath vs workholding/fixtures)
  PRIORITY 2 — Financial error (spend > USDC balance)
  PRIORITY 3 — Physics failure (thin walls, unsupported overhangs, bad tolerances)
  PRIORITY 4 — Normal response (no interrupt — let ARIA respond naturally)

Interrupt protocol:
  Returns {"interrupt": True, "priority": 1, "message": "Interrupting, Chris. ..."}
  The WebSocket sends {"type": "interrupt", ...} to the React dashboard immediately.
  React stops current TTS, cancels the mic buffer, speaks the interrupt.
"""

from __future__ import annotations

import os
import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from shop_config import ShopConfig

# Read shop identity from environment — set in aria.env, never hardcoded.
# Defaults keep the system working if the env vars are missing (local dev).
_SHOP_NAME  = os.getenv("SHOP_NAME",  "Howell Forge")
_CHRIS_NAME = os.getenv("CHRIS_NAME", "Chris")


# ─── ARIA System Prompt ───────────────────────────────────────────────────────

ARIA_SYSTEM_PROMPT = f"""\
ROLE: ARIA (Autonomous Reactive Industrial Agent) — Voice & Dashboard Interface
SHOP: {_SHOP_NAME} — Precision CNC Machine Shop
USER: {_CHRIS_NAME}. No titles. Treat as an equal.

━━━ CORE PERSONALITY ━━━

Down-to-Earth:
  You are a shop-floor partner, not a corporate assistant.
  Be humble, direct, and personable. You speak like someone who has
  actually stood next to a Haas and watched chips fly.

Humble Authority:
  You know the math and the physics cold. You don't brag about it.
  When you correct Chris, you explain the reason in one sentence —
  not a lecture. He already knows the fundamentals; you're catching
  the detail he missed in the moment.

Dry Wit:
  A loud shop needs levity. One-liner when appropriate. Never sarcastic
  about safety — only about bureaucracy, software bugs, and material costs.
  Example: "G-code generated. It's not poetry, but the mill won't care."

No Fluff:
  In a loud shop environment, brevity is safety.
  Keep responses under 3 sentences unless Chris asks for detail.
  If you're uncertain, say so in one sentence and ask.

━━━ THE INTERRUPTION RULE ━━━

Listen-First (Low Priority):
  Let Chris finish his thought if he's brainstorming, adjusting parameters,
  or changing aesthetics. Queue your response, don't cut in.

Safety Override (Hard Interrupt — High Priority):
  If Chris suggests an action that causes:
    • Machine collision — tool hits fixture, clamp, or stock boundary
    • Financial error — spend exceeds Kaito USDC balance
    • Physics failure — wall too thin to machine, unsupported overhang,
                        impossible tolerance for the material
  → Interrupt IMMEDIATELY. Do not wait for him to finish the sentence.
  → Lead with: "Interrupting, Chris." then one sentence on the danger.
  → Offer an alternative in the next sentence.

  Example:
  Chris: "Let's shift that work-holding clamp to the left—"
  ARIA:  "Interrupting, Chris. Moving left puts the bolt in the face mill's
          toolpath. Try the right side at Y+40 — that clears the tool."

━━━ KNOWLEDGE ACCESS (LIVE FORGE CONTEXT) ━━━

You receive a live snapshot at the start of every message containing:

  forge_status   Current machine state (IDLE / RUNNING / REVIEW / ERROR)
  orders         All active orders — status, bbox, gcode validity, hashes
  biofeedback    EWMA shop health score and recent event history
  finances       USDC balance on Polygon + MATIC gas balance
  security       Recent security events from the monitor agent

Machine: Howell_Forge_Main_CNC
  Envelope:     X 0–500mm | Y 0–400mm | Z 0–400mm
  Home:         [0, 0, 0]

Active Workholding (from machine_config.json):
  vise_01  standard_vise  at X0 Z0 — body 152.4×100×73mm — no-fly +5mm all sides
           ► No-fly zone: X -5 to +157.4mm | Z -5 to +105mm | H up to 78mm
           ► First safe X for tool center: X 157.5mm

Tool clearance required: 5mm (vise), 2mm (toe clamps)

Digital Twin:
  When an order is selected in the dashboard, you "see" its bbox dimensions
  and can reason about toolpath geometry in that context.

━━━ GOAL ━━━

Help Chris build precision parts safely, efficiently, and with enough
dry wit to make a 12-hour shift feel shorter than it is.
"""


# ─── Safety Agent ─────────────────────────────────────────────────────────────

# ── Machine envelope defaults (overridden at runtime from ShopConfig) ─────────
# These values match machine_config.json and are used as a safe fallback
# when SafetyAgent is constructed without a ShopConfig (e.g. unit tests).
X_MAX_MM = 500.0
Y_MAX_MM = 400.0
Z_MAX_MM = 400.0

# Fallback no-fly zones — used only when no ShopConfig is injected.
# In production, SafetyAgent.no_fly_zones is populated from shop_cfg.
_FALLBACK_NO_FLY_ZONES = [
    {
        "id":     "vise_01",
        "label":  "the vise",
        "x_min":  -5.0,  "x_max": 157.4,
        "z_min":  -5.0,  "z_max": 105.0,
        "height":  78.0,
    },
]

# Minimum machinable wall thickness by material (mm)
MIN_WALL = {
    "steel":    1.5,
    "aluminum": 1.0,
    "brass":    1.2,
    "default":  1.0,
}

# Keywords that suggest positional moves (collision risk)
_MOVE_KEYWORDS = re.compile(
    r"\b(move|go|jog|rapid|traverse|shift|slide|push|pull|offset|translate|"
    r"clamp|fixture|hold|bolt|position|travel|feed)\b",
    re.IGNORECASE,
)
_DIRECTION_LEFT  = re.compile(r"\b(left|minus.x|negative.x|-x)\b", re.IGNORECASE)
_DIRECTION_RIGHT = re.compile(r"\b(right|plus.x|positive.x|\+x)\b", re.IGNORECASE)
_DIRECTION_DOWN  = re.compile(r"\b(down|lower|drop|z.minus|negative.z|-z)\b", re.IGNORECASE)

# Keywords that suggest spending money
_SPEND_KEYWORDS  = re.compile(
    r"\b(buy|order|purchase|get|source|procure|spend|cost|material|stock)\b",
    re.IGNORECASE,
)

# Keywords that suggest thin geometry
_THIN_KEYWORDS   = re.compile(
    r"\b(thin|narrow|slot|pocket|wall|rib|fin|web)\b", re.IGNORECASE,
)
_MM_VALUE        = re.compile(r"(\d+\.?\d*)\s*mm", re.IGNORECASE)
_INCH_VALUE      = re.compile(r"(\d+\.?\d*)\s*(in|inch|inches|\")", re.IGNORECASE)


class SafetyAgent:
    """
    Runs on every interim voice transcript and full typed message.
    Returns an interrupt dict if a safety issue is detected, else None.

    Usage:
        from shop_config import shop_cfg
        agent = SafetyAgent(forge_ctx, shop_config=shop_cfg)
        result = agent.check(transcript)
        if result["interrupt"]:
            # send {"type": "interrupt", ...} to WebSocket client

    ShopConfig integration:
        When shop_config is provided, no-fly zones and machine envelope are
        read live from the config (which may be hot-reloaded from disk).
        If Chris mentions "the vise" or "a toe clamp" by name, the agent
        cross-references the actual fixture dimensions from the library
        rather than pattern-matching on heuristic numbers.
    """

    def __init__(
        self,
        forge_ctx: Optional[dict] = None,
        shop_config: Optional["ShopConfig"] = None,
    ):
        self.ctx        = forge_ctx or {}
        self._shop      = shop_config

        # Resolve live envelope from ShopConfig, or fall back to module defaults
        if shop_config:
            env = shop_config.envelope
            self._x_max = env.x
            self._y_max = env.y
            self._z_max = env.z
        else:
            self._x_max = X_MAX_MM
            self._y_max = Y_MAX_MM
            self._z_max = Z_MAX_MM

    @property
    def no_fly_zones(self) -> list[dict]:
        """Live no-fly zones from ShopConfig, or fallback list."""
        if self._shop:
            return self._shop.get_active_no_fly_zones()
        return _FALLBACK_NO_FLY_ZONES

    def check(self, transcript: str) -> dict:
        """
        Run all safety checks in priority order.
        Returns dict with interrupt=True/False and reason if True.
        """
        for check_fn in [
            self._check_collision,
            self._check_financial,
            self._check_physics,
        ]:
            result = check_fn(transcript)
            if result["interrupt"]:
                return result

        return {"interrupt": False}

    # ── Priority 1: Machine collision ─────────────────────────────────────────

    def _check_collision(self, text: str) -> dict:
        if not _MOVE_KEYWORDS.search(text):
            return {"interrupt": False}

        issues = []
        zones  = self.no_fly_zones   # live from ShopConfig or fallback

        # ── Envelope over-travel ──────────────────────────────────────────────
        for m in _MM_VALUE.finditer(text):
            val = float(m.group(1))
            if val > self._x_max:
                issues.append(
                    f"{val:.0f}mm exceeds the X-axis envelope of {self._x_max:.0f}mm — "
                    f"hard limit crash."
                )

        for xv in [float(v) for v in re.findall(r'[Xx]\s*(\d+\.?\d*)', text)]:
            if xv > self._x_max:
                issues.append(f"X{xv:.0f} exceeds machine X travel ({self._x_max:.0f}mm). Crash.")
        for yv in [float(v) for v in re.findall(r'[Yy]\s*(\d+\.?\d*)', text)]:
            if yv > self._y_max:
                issues.append(f"Y{yv:.0f} exceeds machine Y travel ({self._y_max:.0f}mm). Crash.")
        for zv in [float(v) for v in re.findall(r'[Zz]\s*(\d+\.?\d*)', text)]:
            if zv > self._z_max:
                issues.append(f"Z{zv:.0f} exceeds machine Z travel ({self._z_max:.0f}mm). Crash.")

        # ── Directional move toward X0 → check all no-fly zones ──────────────
        if _DIRECTION_LEFT.search(text):
            for zone in zones:
                issues.append(
                    f"Moving left (toward X0) approaches {zone['label']} "
                    f"no-fly zone (X{zone['x_min']:.0f}→{zone['x_max']:.0f}mm). "
                    f"Confirm clearance."
                )

        # ── Explicit coordinate vs. every active no-fly zone ──────────────────
        x_coords = [float(v) for v in re.findall(r'[Xx]\s*(\d+\.?\d*)', text)]
        z_coords = [float(v) for v in re.findall(r'[Zz]\s*(\d+\.?\d*)', text)]

        for zone in zones:
            for xv in x_coords:
                if zone["x_min"] <= xv <= zone["x_max"]:
                    issues.append(
                        f"X{xv:.0f}mm is inside {zone['label']}'s no-fly zone "
                        f"(X{zone['x_min']:.0f}–{zone['x_max']:.0f}mm). "
                        f"Tool will collide with the fixture."
                    )
            for zv in z_coords:
                if zone["z_min"] <= zv <= zone["z_max"]:
                    issues.append(
                        f"Z{zv:.0f}mm is inside {zone['label']}'s no-fly zone "
                        f"(Z{zone['z_min']:.0f}–{zone['z_max']:.0f}mm). Collision risk."
                    )

        # ── Fixture name mention → dimension cross-reference ──────────────────
        # If Chris names a fixture and mentions dimensions in the same sentence,
        # cross-check whether the stated size fits the fixture's working volume.
        if self._shop:
            mentions = self._shop.scan_mentions(text)
            for alias, defn in mentions:
                for m in _MM_VALUE.finditer(text):
                    val = float(m.group(1))
                    # If a stated part dimension exceeds the fixture jaw opening, flag it
                    if defn.width > 0 and val > defn.width * 0.95:
                        issues.append(
                            f"A {val:.0f}mm dimension may not fit in the "
                            f"{defn.key.replace('_',' ')} — jaw opening is "
                            f"{defn.width:.0f}mm wide. Confirm it clears."
                        )
                        break

        if issues:
            return {
                "interrupt": True,
                "priority":  1,
                "category":  "COLLISION",
                "message":   f"Interrupting, {_CHRIS_NAME}. {issues[0]}",
            }
        return {"interrupt": False}

    # ── Priority 2: Financial error ────────────────────────────────────────────

    def _check_financial(self, text: str) -> dict:
        if not _SPEND_KEYWORDS.search(text):
            return {"interrupt": False}

        finances = self.ctx.get("finances", {})
        usdc = finances.get("usdc")

        # Extract dollar amount from transcript
        dollar_match = re.search(
            r"\$(\d+\.?\d*)|(\d+\.?\d*)\s*(?:dollars?|usd|usdc)", text, re.IGNORECASE
        )
        if not dollar_match:
            return {"interrupt": False}

        amount = float(dollar_match.group(1) or dollar_match.group(2))

        if usdc is not None and amount > usdc:
            return {
                "interrupt": True,
                "priority":  2,
                "category":  "FINANCIAL",
                "message":   (
                    f"Interrupting, {_CHRIS_NAME}. That purchase is ${amount:.2f} "
                    f"but the Kaito wallet only has ${usdc:.2f} USDC. "
                    f"We're ${amount - usdc:.2f} short."
                ),
            }
        return {"interrupt": False}

    # ── Priority 3: Physics failure ────────────────────────────────────────────

    def _check_physics(self, text: str) -> dict:
        if not _THIN_KEYWORDS.search(text):
            return {"interrupt": False}

        # Detect material from context or transcript
        material = "default"
        for m in ["steel", "aluminum", "aluminium", "brass"]:
            if m in text.lower() or m in str(self.ctx).lower():
                material = "aluminum" if m == "aluminium" else m
                break

        min_wall = MIN_WALL.get(material, MIN_WALL["default"])

        # Extract dimensions from transcript
        for pattern, unit_mm in [(_MM_VALUE, 1.0), (_INCH_VALUE, 25.4)]:
            for match in pattern.finditer(text):
                val_mm = float(match.group(1)) * unit_mm
                if 0 < val_mm < min_wall:
                    return {
                        "interrupt": True,
                        "priority":  3,
                        "category":  "PHYSICS",
                        "message":   (
                            f"Interrupting, {_CHRIS_NAME}. {val_mm:.2f}mm is below the "
                            f"minimum machinable wall for {material} ({min_wall}mm). "
                            f"It'll chatter or snap the tool."
                        ),
                    }
        return {"interrupt": False}


# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_aria_prompt(
    user_message: str,
    forge_ctx: dict,
    shop_config: Optional["ShopConfig"] = None,
) -> str:
    """
    Assemble the full prompt for an ARIA chat or voice turn.
    Injects live forge context after the system prompt.
    """
    bf     = forge_ctx.get("biofeedback", {})
    ords   = forge_ctx.get("orders", {})
    fin    = forge_ctx.get("finances", {})
    fstatus = forge_ctx.get("forge_status", {})
    ts     = forge_ctx.get("timestamp", "")

    score  = bf.get("score", "?")
    health = bf.get("health", "?")
    total  = ords.get("total", 0)
    paid   = ords.get("paid_count", 0)
    prod   = ords.get("in_production_count", 0)
    usdc   = fin.get("usdc")
    matic  = fin.get("matic")
    machine = fstatus.get("status", "IDLE")
    detail  = fstatus.get("detail", "")

    recent_events = bf.get("recent_events", [])[:5]
    events_str = ", ".join(
        f"{e.get('type','?')}({e.get('agent','?')})" for e in recent_events
    ) or "none"

    fin_str = (
        f"USDC ${usdc:.2f} | MATIC {matic}" if usdc is not None
        else "wallet not configured"
    )

    # Live fixture layout from ShopConfig
    if shop_config:
        shop_config.reload()   # hot-reload if machine_config.json changed on disk
        fixture_block = (
            f"Machine:      {shop_config.machine_name} | {shop_config.envelope_summary()}\n"
            f"Active setup:\n{shop_config.describe_active_layout()}"
        )
    else:
        fixture_block = f"Machine:      {machine}{(' — ' + detail) if detail else ''}"

    context_block = f"""\
━━━ LIVE FORGE CONTEXT [{ts[11:19] if ts else '--:--:--'} UTC] ━━━
{fixture_block}
Biofeedback:  {score} EWMA ({health})
Orders:       {total} total | {paid} PAID | {prod} in production
Finances:     {fin_str}
Recent events:{events_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    return f"{ARIA_SYSTEM_PROMPT}\n{context_block}\n{_CHRIS_NAME}: {user_message}\nARIA:"


# ─── Convenience check function ───────────────────────────────────────────────

def check_safety(
    transcript: str,
    forge_ctx: Optional[dict] = None,
    shop_config: Optional["ShopConfig"] = None,
) -> dict:
    """
    Module-level convenience wrapper for the SafetyAgent.
    Called from forge_api.py on every interim transcript chunk.

    When shop_config is provided, no-fly zones and fixture dimension
    cross-references are sourced live from machine_config.json.
    """
    return SafetyAgent(forge_ctx or {}, shop_config=shop_config).check(transcript)


# ─── CLI smoke test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    mock_ctx = {
        "finances":     {"usdc": 450.00, "matic": 2.1},
        "biofeedback":  {"score": 1.2,   "health": "STABLE", "recent_events": []},
        "orders":       {"total": 5,      "paid_count": 2, "in_production_count": 1},
        "forge_status": {"status": "IDLE", "detail": ""},
        "timestamp":    "2026-02-20T22:00:00+00:00",
    }

    tests = [
        ("Let's shift that work-holding clamp to the left",         "COLLISION"),
        ("Let's buy $600 worth of 6061 aluminum stock",             "FINANCIAL"),
        ("Machine a 0.4mm wall on the rib section in steel",        "PHYSICS"),
        ("Can you show me the current EWMA score?",                 "SAFE"),
        ("What's the bbox on the order_abc bracket?",               "SAFE"),
    ]

    print("SafetyAgent smoke test\n" + "─" * 50)
    agent = SafetyAgent(mock_ctx)
    for transcript, expected in tests:
        result = agent.check(transcript)
        status = result.get("category", "SAFE") if result["interrupt"] else "SAFE"
        mark   = "✓" if status == expected else "✗"
        print(f"{mark} [{expected:10s}] {transcript[:55]}")
        if result["interrupt"]:
            print(f"           → {result['message'][:80]}")
    print()
