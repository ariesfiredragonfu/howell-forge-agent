#!/usr/bin/env python3
"""
crew_logic.py — Howell Forge CrewAI Agent Definitions

Hardware profile (Feb 21, 2026):
  T1200 Laptop GPU: 4 GB VRAM → HYBRID strategy
  - Quartermaster (logistics)  : local phi3:mini via Ollama (CPU, ~2.3 GB RAM)
  - CAD Reasoning              : groq/llama-3.3-70b-versatile (cloud, free tier)

Agents defined here:
  quartermaster   — monitors material stock vs active CAD requirements,
                    drafts Kaito purchase orders when below threshold
  cad_consultant  — reviews FreeCAD geometry for manufacturability (future)

To run:
  pip install crewai crewai-tools
  ollama pull phi3:mini          # one-time, ~2.3 GB download
  python agents/crew_logic.py

Environment variables needed:
  GROQ_API_KEY    — free at console.groq.com
  OPENAI_API_KEY  — already set in aria.env (fallback)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ── Model selection (matches hardware_scout.py strategy) ─────────────────────
# 4 GB VRAM, 32 GB RAM → CPU-local for lightweight agents, Groq for heavy ones
_LOCAL_MODEL = os.getenv("FORGE_LOCAL_MODEL",  "ollama/phi3:mini")
_CLOUD_MODEL = os.getenv("FORGE_CLOUD_MODEL",  "groq/llama-3.3-70b-versatile")

# ── Shop paths ────────────────────────────────────────────────────────────────
_REPO_ROOT       = Path(__file__).parent.parent
_MACHINE_CONFIG  = _REPO_ROOT / "machine_config.json"
_ORDERS_DIR      = Path(os.getenv("FORGE_ORDERS_DIR",
                         str(Path.home() / "Hardware_Factory" / "forge_orders")))

# ── Material inventory (extend this as the shop grows) ───────────────────────
# Format: { "material": {"on_hand_lbs": float, "reorder_threshold_lbs": float} }
INVENTORY = {
    "6061_aluminum": {"on_hand_lbs": 12.0, "reorder_threshold_lbs": 5.0},
    "carbide_endmill_1_4": {"on_hand_units": 3, "reorder_threshold_units": 2},
    "carbide_endmill_1_2": {"on_hand_units": 2, "reorder_threshold_units": 1},
}


def _load_active_cad_requirements() -> dict:
    """
    Scan open orders for material requirements.
    Returns a dict of {material: quantity_needed}.
    Currently stub — reads order descriptions and infers materials.
    """
    reqs: dict = {}
    if not _ORDERS_DIR.exists():
        return reqs
    for order_dir in _ORDERS_DIR.iterdir():
        desc_file = order_dir / "description.txt"
        if desc_file.exists():
            desc = desc_file.read_text().lower()
            if "aluminum" in desc or "6061" in desc:
                reqs["6061_aluminum"] = reqs.get("6061_aluminum", 0) + 2.0
            if "endmill" in desc or "carbide" in desc:
                reqs["carbide_endmill_1_4"] = reqs.get("carbide_endmill_1_4", 0) + 1
    return reqs


def _check_stock_vs_requirements(inventory: dict, requirements: dict) -> list[dict]:
    """
    Compare on-hand stock against requirements + reorder thresholds.
    Returns list of items that need ordering.
    """
    shortfalls = []
    for item, req_qty in requirements.items():
        stock = inventory.get(item, {})
        on_hand = stock.get("on_hand_lbs") or stock.get("on_hand_units", 0)
        threshold = stock.get("reorder_threshold_lbs") or stock.get("reorder_threshold_units", 0)
        if on_hand - req_qty < threshold:
            shortfalls.append({
                "item": item,
                "on_hand": on_hand,
                "required": req_qty,
                "shortfall": max(0, req_qty - on_hand + threshold),
            })
    return shortfalls


# ── CrewAI Agent Definitions ──────────────────────────────────────────────────

def build_crew():
    """
    Build and return the Howell Forge CrewAI crew.
    Import is deferred so the file can be imported without crewai installed.
    """
    try:
        from crewai import Agent, Task, Crew, Process
    except ImportError:
        raise ImportError(
            "crewai not installed. Run: pip install crewai crewai-tools"
        )

    # ── Quartermaster — lightweight logistics agent (runs local) ──────────────
    quartermaster = Agent(
        role="Quartermaster",
        goal=(
            "Ensure Howell Forge never runs out of 6061 Aluminum stock or "
            "Carbide Endmills. Monitor Kaito prices and shop inventory. "
            "Draft purchase orders when stock falls below threshold."
        ),
        backstory=(
            "A logistics expert embedded in the shop floor. You know the machine "
            "specs (500×400×400 mm build volume), the typical material removal rates "
            "for aluminum, and the reorder lead times from suppliers. "
            "You are frugal but never let a job stop for lack of material."
        ),
        verbose=True,
        allow_delegation=False,
        llm=_LOCAL_MODEL,      # CPU-local: phi3:mini (~2.3 GB RAM, no GPU needed)
    )

    # ── CAD Consultant — heavy reasoning agent (runs on Groq cloud) ───────────
    cad_consultant = Agent(
        role="CAD Consultant",
        goal=(
            "Review FreeCAD geometry descriptions for manufacturability on the "
            "Howell_Forge_Main_CNC (500×400×400 mm). Flag thin walls, impossible "
            "tolerances, or fixture conflicts before G-code is generated."
        ),
        backstory=(
            "A senior machinist and CAD engineer with 20 years of CNC experience. "
            "You think in terms of setup time, tool reach, and material behaviour. "
            "You are direct and never sugar-coat a bad design."
        ),
        verbose=True,
        allow_delegation=False,
        llm=_CLOUD_MODEL,      # Groq 70B: heavy reasoning, free tier
    )

    # ── Tasks ─────────────────────────────────────────────────────────────────
    reqs = _load_active_cad_requirements()
    shortfalls = _check_stock_vs_requirements(INVENTORY, reqs)
    shortfall_str = json.dumps(shortfalls, indent=2) if shortfalls else "None — stock is sufficient."

    monitor_stock = Task(
        description=(
            f"Current inventory:\n{json.dumps(INVENTORY, indent=2)}\n\n"
            f"Active CAD requirements:\n{json.dumps(reqs, indent=2)}\n\n"
            f"Identified shortfalls:\n{shortfall_str}\n\n"
            "If any shortfalls exist, draft a Kaito purchase order in plain text "
            "with item name, quantity, and urgency (URGENT / STANDARD). "
            "If stock is fine, report 'All clear — no orders needed.'"
        ),
        agent=quartermaster,
        expected_output=(
            "A plain-text stock report and, if needed, a drafted Kaito purchase "
            "order listing each item, quantity to order, and urgency level."
        ),
    )

    # ── Crew ──────────────────────────────────────────────────────────────────
    crew = Crew(
        agents=[quartermaster, cad_consultant],
        tasks=[monitor_stock],
        process=Process.sequential,
        verbose=True,
    )
    return crew


# ── Standalone run ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n--- ⚒️  HOWELL FORGE CREW ---")
    print(f"  Local model : {_LOCAL_MODEL}")
    print(f"  Cloud model : {_CLOUD_MODEL}")
    print(f"  Orders dir  : {_ORDERS_DIR}\n")

    # Quick stock check without crewai (works even if crewai isn't installed yet)
    reqs = _load_active_cad_requirements()
    shortfalls = _check_stock_vs_requirements(INVENTORY, reqs)

    print("  INVENTORY:")
    for item, stock in INVENTORY.items():
        print(f"    {item}: {stock}")

    print(f"\n  CAD REQUIREMENTS: {reqs or 'none (no open orders)'}")

    if shortfalls:
        print("\n  ⚠️  SHORTFALLS DETECTED:")
        for s in shortfalls:
            print(f"    {s['item']}: need {s['shortfall']} more units/lbs")
        print("\n  → Quartermaster would draft a Kaito purchase order for the above.")
    else:
        print("\n  ✅ All clear — stock levels are sufficient for active orders.")

    print("\n  To run the full CrewAI crew:")
    print("    pip install crewai crewai-tools")
    print("    ollama pull phi3:mini")
    print("    export GROQ_API_KEY=your_key")
    print("    python agents/crew_logic.py\n")
