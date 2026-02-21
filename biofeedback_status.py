#!/usr/bin/env python3
"""
biofeedback_status.py — Read-only CLI snapshot of biofeedback state.

Usage:
    python3 biofeedback_status.py

Also importable — format_status() is used by telegram_biofeedback_bot.py.
Does not write to any state.
"""

import sys

from biofeedback import get_biofeedback_status


def format_status(status: dict, *, compact: bool = False) -> str:
    """
    Return a multi-line human-readable string from a get_biofeedback_status() dict.

    compact=True omits the divider header (suitable for Telegram messages).
    """
    lines: list[str] = []

    if not compact:
        lines.append("── Biofeedback Status " + "─" * 36)

    lines += [
        f"Current Score:  {status['current_score']:+.2f}",
        f"Scale Mode:     {status['scale_mode']}",
        "",
        "Recent Counts (48h):",
    ]

    counts = {k: v for k, v in status["recent_counts"].items() if v > 0}
    if counts:
        col = max(len(k) for k in counts)
        for k, v in counts.items():
            lines.append(f"  {k:<{col}}  {v}")
    else:
        lines.append("  (no activity in last 48h)")

    lines.append("")
    lines.append("Active Boosts:")

    boosts = status["active_boosts"]
    # Constraint boosts (negative weight types boosted due to repeats) are the
    # operationally interesting signal.  Reward boosts for zero-count types are
    # always active (count=0 ≤ rare threshold) and shown last to reduce noise.
    constraint_boosts = {k: v for k, v in boosts.items() if "≥" in v}
    reward_boosts     = {k: v for k, v in boosts.items() if "≤" in v}

    if constraint_boosts:
        for k, note in constraint_boosts.items():
            lines.append(f"  {k}: {note}")
    if reward_boosts:
        lines.append("  Reward (rare):")
        for k, note in reward_boosts.items():
            lines.append(f"    {k}: {note}")
    if not boosts:
        lines.append("  (none)")

    return "\n".join(lines)


def main() -> int:
    status = get_biofeedback_status()
    print(format_status(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
