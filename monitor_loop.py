#!/usr/bin/env python3
"""
Monitor Loop — Runs the Monitor agent on an interval.
"""

import subprocess
import sys
import time
from pathlib import Path

INTERVAL_SEC = 300  # 5 minutes


def main():
    agent_dir = Path(__file__).parent
    monitor_script = agent_dir / "monitor.py"
    while True:
        subprocess.run([sys.executable, str(monitor_script)], cwd=str(agent_dir))
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
