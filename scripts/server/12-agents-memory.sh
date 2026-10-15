#!/bin/bash
# Step 12: cap the agents' memory so a heavy research script can never starve the MCP server, sshd or the system
# (12 GB RAM, no swap; on 2026-10-15 a quant script grew to ~6 GB and the kernel's global OOM killer stepped in).
# Above MemoryHigh the kernel reclaims and slows the agents down; at MemoryMax only a process inside the agents'
# service is killed, and the loop restarts and continues its work. Applies at once and persists. Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/12-agents-memory.sh
set -euo pipefail
systemctl set-property tradedesk-agents-loop.service MemoryHigh=6G MemoryMax=7G MemorySwapMax=0
systemctl show tradedesk-agents-loop.service -p MemoryHigh -p MemoryMax -p MemoryCurrent
echo "agents memory capped: high 6G, max 7G"
