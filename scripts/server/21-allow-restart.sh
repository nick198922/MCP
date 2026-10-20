#!/bin/bash
# Step 21: let tradedesk (Claude's sessions) restart the MCP server now instead of waiting for the hourly update.
# Grants exactly one root command, no general sudo: `sudo -n /usr/local/sbin/tradedesk-update --force`
# (pull main as tradedesk, uv sync, restart tradedesk-mcp; see step 20). Undo: rm /etc/sudoers.d/tradedesk-update
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/21-allow-restart.sh
set -euo pipefail
test -x /usr/local/sbin/tradedesk-update || { echo "run step 20 first"; exit 1; }
f=/etc/sudoers.d/tradedesk-update
tmp="$(mktemp)"
echo 'tradedesk ALL=(root) NOPASSWD: /usr/local/sbin/tradedesk-update --force' >"$tmp"
visudo -cf "$tmp"
install -m 0440 -o root -g root "$tmp" "$f"
rm -f "$tmp"
echo "ok: tradedesk may run 'sudo -n /usr/local/sbin/tradedesk-update --force' (and nothing else as root)"
