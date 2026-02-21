#!/usr/bin/env python3
"""
shop_config.py — Read-only ShopConfig for Howell Forge.

This module provides a single `ShopConfig` instance that the voice worker,
safety agent, and API all share. It reads machine_config.json at startup
and never writes to it.

Key responsibilities:
  1. Load machine envelope (build_volume, home_position).
  2. Expose the full workholding_library with computed no-fly zones.
  3. Resolve fixture names from natural language → config entry.
     e.g. "the vise", "kurt vise", "vise 1", "vise_01" → standard_vise
  4. Build the active no-fly zone list from active_layout.current_fixtures
     so SafetyAgent always reflects the live table setup.
  5. Support hot-reload: call .reload() if machine_config.json changes.
     The voice worker calls this at the start of each turn.

Read-only guarantee:
  ShopConfig opens the file with 'r' (never 'w'). No method modifies the file.
  Writes go through forge_api.py POST /machine-config/fixtures only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aria.shop_config")

CONFIG_PATH = Path(__file__).parent / "machine_config.json"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Envelope:
    x: float
    y: float
    z: float
    home: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])

    def contains(self, x: float, y: float, z: float) -> bool:
        return (0 <= x <= self.x) and (0 <= y <= self.y) and (0 <= z <= self.z)


@dataclass
class FixtureDef:
    """Definition from workholding_library — describes a fixture type."""
    key:         str            # library key, e.g. "standard_vise"
    fixture_type: str           # "fixed_vise" | "strap_clamp"
    length:      float          # X dimension
    width:       float          # Z dimension (depth)
    height:      float          # Y dimension (vertical)
    buffer:      float          # no-fly zone buffer, mm
    description: str = ""


@dataclass
class ActiveFixture:
    """An instance placed on the table from active_layout.current_fixtures."""
    id:          str
    fixture_type: str           # key into workholding_library
    position:    list[float]    # [X, Y_machine, Z_machine] in machine coords
    rotation:    float          # degrees around Y (vertical)
    status:      str            # "active" | "inactive"
    definition:  Optional[FixtureDef] = None

    @property
    def no_fly_zone(self) -> dict:
        """
        Compute the no-fly box for this instance.
        Returns min/max bounds in machine coordinate space with buffer applied.
        """
        if not self.definition:
            return {}
        d   = self.definition
        px  = self.position[0]
        pz  = self.position[2] if len(self.position) > 2 else 0.0
        buf = d.buffer
        return {
            "id":     self.id,
            "label":  f"the {self.id} ({d.fixture_type.replace('_',' ')})",
            "x_min":  px        - buf,
            "x_max":  px + d.length + buf,
            "z_min":  pz        - buf,
            "z_max":  pz + d.width  + buf,
            "height": d.height  + buf,
        }


# ── Fixture name resolver ─────────────────────────────────────────────────────
#
# Maps common natural-language names and aliases to workholding_library keys.
# Chris might say "the vise", "kurt", "6-inch vise", "toe clamp", "strap clamp".
# Add entries here as new fixture types are added to machine_config.json.

_NAME_ALIASES: dict[str, str] = {
    # standard_vise
    "vise":           "standard_vise",
    "kurt vise":      "standard_vise",
    "kurt":           "standard_vise",
    "6 inch vise":    "standard_vise",
    "6-inch vise":    "standard_vise",
    "fixed vise":     "standard_vise",
    "jaw vise":       "standard_vise",
    "milling vise":   "standard_vise",
    "machine vise":   "standard_vise",
    "standard vise":  "standard_vise",
    # toe_clamp_set
    "toe clamp":      "toe_clamp_set",
    "strap clamp":    "toe_clamp_set",
    "strap":          "toe_clamp_set",
    "clamp":          "toe_clamp_set",
    "toe clamps":     "toe_clamp_set",
    "strap clamps":   "toe_clamp_set",
}


# ── ShopConfig ────────────────────────────────────────────────────────────────

class ShopConfig:
    """
    Read-only interface to machine_config.json.
    Thread-safe for concurrent reads; reload() is protected by a lock.
    """

    def __init__(self, path: Path = CONFIG_PATH):
        self._path    = path
        self._lock    = threading.RLock()
        self._raw: dict                    = {}
        self.envelope: Envelope            = Envelope(500, 400, 400)
        self.library: dict[str, FixtureDef] = {}
        self.active:  list[ActiveFixture]   = []
        self._mtime: float                  = 0.0
        self.reload()

    # ── Load / reload ─────────────────────────────────────────────────────────

    def reload(self) -> bool:
        """
        Re-read machine_config.json if it has changed since last load.
        Returns True if the file was actually re-loaded, False if unchanged.
        Thread-safe.
        """
        if not self._path.exists():
            logger.warning("machine_config.json not found at %s", self._path)
            return False

        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return False   # unchanged

        with self._lock:
            try:
                raw = json.loads(self._path.read_text())
                self._raw   = raw
                self._mtime = mtime
                self._parse(raw)
                logger.info(
                    "ShopConfig loaded: machine=%s envelope=%.0fx%.0fx%.0f "
                    "library=%d types active=%d fixtures",
                    raw.get("machine_specs", {}).get("name", "?"),
                    self.envelope.x, self.envelope.y, self.envelope.z,
                    len(self.library), len(self.active),
                )
                return True
            except Exception as exc:
                logger.error("Failed to load machine_config.json: %s", exc)
                return False

    def _parse(self, raw: dict) -> None:
        # Machine envelope
        specs = raw.get("machine_specs", {})
        bv    = specs.get("build_volume", {})
        self.envelope = Envelope(
            x    = float(bv.get("x", 500)),
            y    = float(bv.get("y", 400)),
            z    = float(bv.get("z", 400)),
            home = specs.get("home_position", [0.0, 0.0, 0.0]),
        )

        # Workholding library
        self.library = {}
        for key, entry in raw.get("workholding_library", {}).items():
            dim = entry.get("dimensions", {})
            self.library[key] = FixtureDef(
                key          = key,
                fixture_type = entry.get("type", key),
                length       = float(dim.get("length", 0)),
                width        = float(dim.get("width", 0)),
                height       = float(dim.get("height", 0)),
                buffer       = float(entry.get("no_fly_zone_buffer", 0)),
                description  = entry.get("description", ""),
            )

        # Active layout
        self.active = []
        for item in raw.get("active_layout", {}).get("current_fixtures", []):
            ftype = item.get("type", "")
            defn  = self.library.get(ftype)
            self.active.append(ActiveFixture(
                id           = item.get("id", "unknown"),
                fixture_type = ftype,
                position     = item.get("position", [0, 0, 0]),
                rotation     = float(item.get("rotation", 0)),
                status       = item.get("status", "active"),
                definition   = defn,
            ))

    # ── Fixture lookup ────────────────────────────────────────────────────────

    def resolve_fixture_type(self, name: str) -> Optional[str]:
        """
        Resolve a natural-language fixture name to a library key.
        Case-insensitive. Returns None if no match.

        Examples:
          "the vise"         → "standard_vise"
          "kurt vise"        → "standard_vise"
          "toe clamp"        → "toe_clamp_set"
          "standard_vise"    → "standard_vise"   (direct key)
        """
        name_lower = name.strip().lower()
        # Direct library key match first
        if name_lower in self.library:
            return name_lower
        # Alias lookup (longest match wins to avoid "clamp" swallowing "toe clamp")
        best_key, best_len = None, 0
        for alias, lib_key in _NAME_ALIASES.items():
            if alias in name_lower and len(alias) > best_len:
                best_key = lib_key
                best_len = len(alias)
        return best_key

    def get_fixture_def(self, name: str) -> Optional[FixtureDef]:
        """Resolve name to FixtureDef (or None)."""
        key = self.resolve_fixture_type(name)
        return self.library.get(key) if key else None

    def get_active_fixture_by_id(self, fixture_id: str) -> Optional[ActiveFixture]:
        """Look up a placed fixture by its id (e.g. 'vise_01')."""
        for f in self.active:
            if f.id.lower() == fixture_id.lower():
                return f
        return None

    def get_active_no_fly_zones(self) -> list[dict]:
        """
        Return the current list of no-fly zone dicts for all ACTIVE fixtures.
        Used directly by SafetyAgent. Automatically reflects any config reload.
        """
        return [
            f.no_fly_zone
            for f in self.active
            if f.status == "active" and f.definition
        ]

    # ── Natural-language summary helpers ─────────────────────────────────────

    def describe_fixture(self, name: str) -> str:
        """
        Return a one-sentence description of a fixture's dimensions and no-fly zone,
        suitable for ARIA to speak aloud.
        """
        lib_key = self.resolve_fixture_type(name)
        if not lib_key:
            return f"I don't have '{name}' in the workholding library."
        d = self.library[lib_key]
        # Find any active instance of this type
        instances = [f for f in self.active if f.fixture_type == lib_key]
        if not instances:
            return (
                f"The {lib_key.replace('_',' ')} is in the library but not on the table. "
                f"It's {d.length:.1f}×{d.width:.1f}×{d.height:.1f}mm with a {d.buffer:.0f}mm no-fly buffer."
            )
        inst = instances[0]
        nfz  = inst.no_fly_zone
        return (
            f"{inst.id} ({lib_key.replace('_',' ')}) — body {d.length:.1f}×{d.width:.1f}×{d.height:.1f}mm, "
            f"no-fly zone X{nfz['x_min']:.0f} to {nfz['x_max']:.0f}mm, "
            f"Z{nfz['z_min']:.0f} to {nfz['z_max']:.0f}mm. "
            f"First safe tool X is {nfz['x_max']:.1f}mm."
        )

    def describe_active_layout(self) -> str:
        """One-paragraph summary of all active fixtures for the system prompt."""
        if not self.active:
            return "No fixtures are currently active on the table."
        lines = []
        for f in self.active:
            if f.definition and f.status == "active":
                nfz = f.no_fly_zone
                lines.append(
                    f"  {f.id} ({f.fixture_type.replace('_',' ')}) at "
                    f"X{f.position[0]:.0f} Z{f.position[2] if len(f.position)>2 else 0:.0f} — "
                    f"no-fly X{nfz['x_min']:.0f}→{nfz['x_max']:.0f} "
                    f"Z{nfz['z_min']:.0f}→{nfz['z_max']:.0f}"
                )
        return "\n".join(lines)

    def envelope_summary(self) -> str:
        e = self.envelope
        return f"X 0–{e.x:.0f}mm | Y 0–{e.y:.0f}mm | Z 0–{e.z:.0f}mm"

    # ── Fixture mention scanner ───────────────────────────────────────────────

    def scan_mentions(self, text: str) -> list[tuple[str, FixtureDef]]:
        """
        Scan a transcript for fixture mentions.
        Returns list of (matched_text, FixtureDef) for every fixture name found.
        Used by SafetyAgent to decide when to cross-reference dimensions.

        Uses word-boundary matching so "revised" does not match "vise".
        """
        import re as _re
        found = []
        text_lower = text.lower()
        # Sort aliases longest-first so "toe clamp" beats "clamp"
        for alias in sorted(_NAME_ALIASES, key=len, reverse=True):
            pattern = r'\b' + _re.escape(alias) + r'\b'
            if _re.search(pattern, text_lower):
                lib_key = _NAME_ALIASES[alias]
                defn    = self.library.get(lib_key)
                if defn:
                    found.append((alias, defn))
                    # Blank out matched region to avoid double-counting
                    text_lower = _re.sub(pattern, " " * len(alias), text_lower, count=1)
        return found

    # ── Machine name ──────────────────────────────────────────────────────────

    @property
    def machine_name(self) -> str:
        return self._raw.get("machine_specs", {}).get("name", "CNC Machine")


# ── Module-level singleton ────────────────────────────────────────────────────
#
# Import this instance everywhere:
#   from shop_config import shop_cfg
#
# It is read-only and safe to share across threads.

shop_cfg = ShopConfig()


# ── CLI smoke test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Machine: {shop_cfg.machine_name}")
    print(f"Envelope: {shop_cfg.envelope_summary()}")
    print()
    print("Workholding library:")
    for key, d in shop_cfg.library.items():
        print(f"  {key}: {d.length}×{d.width}×{d.height}mm, buffer {d.buffer}mm")
    print()
    print("Active layout:")
    print(shop_cfg.describe_active_layout())
    print()

    # Resolve tests
    resolve_tests = [
        "the vise",
        "kurt vise",
        "toe clamp",
        "strap",
        "standard_vise",
        "angle plate",   # not in library
    ]
    print("Fixture resolver:")
    for name in resolve_tests:
        result = shop_cfg.get_fixture_def(name)
        if result:
            print(f"  '{name}' → {result.key} ({result.length}×{result.width}×{result.height}mm)")
        else:
            print(f"  '{name}' → not found")
    print()

    # Scan mention tests
    scan_tests = [
        "I want to hold the part in the vise",
        "Can we add a toe clamp on the left side?",
        "No fixtures, just a soft jaw",
    ]
    print("Mention scanner:")
    for text in scan_tests:
        hits = shop_cfg.scan_mentions(text)
        label = ", ".join(f"{a}→{d.key}" for a, d in hits) or "none"
        print(f"  '{text[:55]}' → {label}")
    print()

    # describe_fixture
    print("Fixture description (as ARIA would speak it):")
    for name in ["the vise", "toe clamp", "angle plate"]:
        print(f"  [{name}] {shop_cfg.describe_fixture(name)}")
