#!/bin/bash
# Step 13: when the kernel kills an agent's process for exceeding the agents' memory cap (12-agents-memory.sh), keep the
# agents' service running instead of stopping and restarting it. Then only the oversized research script dies, the
# agent sees the error in the same run and can redesign it, and the run's progress is not lost (on 2026-10-16 two
# night runs were cut short this way). Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/13-agents-oom-policy.sh
set -euo pipefail
mkdir -p /etc/systemd/system/tradedesk-agents-loop.service.d
cat >/etc/systemd/system/tradedesk-agents-loop.service.d/oom.conf <<'CONF'
[Service]
OOMPolicy=continue
CONF
systemctl daemon-reload
systemctl show tradedesk-agents-loop.service -p OOMPolicy -p MemoryHigh -p MemoryMax
echo "OOMPolicy=continue set; systemd applies it after the reload (the line above should read OOMPolicy=continue)"
