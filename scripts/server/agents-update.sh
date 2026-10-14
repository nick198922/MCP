#!/bin/bash
# From the Mac: put the latest main on the server, restart the agents loop and send a status message to Telegram now.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
ssh tradedesk@"$SERVER" 'git -C ~/tradedesk-mcp pull -q --ff-only && git -C ~/tradedesk-mcp log -1 --oneline'
ssh nick@"$SERVER" 'sudo systemctl restart tradedesk-agents-loop && (sudo systemctl try-restart tradedesk-tgbridge || true) && sleep 3 && systemctl is-active tradedesk-agents-loop'
ssh tradedesk@"$SERVER" 'cd ~/tradedesk-mcp && python3 scripts/agents_status.py && echo "status sent to Telegram"'
