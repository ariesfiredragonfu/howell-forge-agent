#!/bin/bash
# Run all agents via orchestrator. Use with cron.
# Example crontab (every hour):
#   0 * * * * cd /home/USER/howell-forge-agent && python3 run_agents.py >> ~/project_docs/agent-run.log 2>&1

cd "$(dirname "$0")"
exec python3 run_agents.py "$@"
