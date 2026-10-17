#!/bin/bash
# Step 14: the agents pause when the Claude plan usage (5-hour or weekly) reaches this many percent (owner's decision
# 2026-10-17: 97 instead of 95). Writes a systemd drop-in for the agents loop, reloads systemd and restarts the loop as
# soon as no agent step or doctor run is working, so a running step is not killed. Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/14-agents-max-usage.sh          (97 %)
#   ssh nick@SERVER 'sudo bash -s -- 96' < scripts/server/14-agents-max-usage.sh    (another value)
set -euo pipefail
MAX="${1:-97}"
[[ "$MAX" =~ ^[0-9]+$ ]] && ((MAX >= 50 && MAX <= 99)) || { echo "the threshold must be 50..99, got: $MAX"; exit 1; }
mkdir -p /etc/systemd/system/tradedesk-agents-loop.service.d
printf '[Service]\nEnvironment=TRADEDESK_AGENTS_MAX_USAGE=%s\n' "$MAX" >/etc/systemd/system/tradedesk-agents-loop.service.d/usage.conf
systemctl daemon-reload
systemctl show tradedesk-agents-loop.service -p Environment | tr ' ' '\n' | grep MAX_USAGE || true
nohup bash -c '
  while pgrep -u tradedesk -f "scripts/(nightly_agents|agents_doctor)\.sh" >/dev/null; do sleep 30; done
  systemctl restart tradedesk-agents-loop
  logger -t tradedesk "agents loop restarted for the new usage threshold"
' >/dev/null 2>&1 &
echo "threshold ${MAX}% set; the loop restarts with it when the current step ends (its start line in agents-status.sh shows «max usage ${MAX}%»)"
