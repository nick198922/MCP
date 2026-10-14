#!/bin/bash
# From the Mac, after switching Topics on in the Telegram group and making the bot an admin that can manage topics:
#   bash scripts/server/telegram-topics.sh
# Creates the topics (agents, status, market alerts), moves the agents' messages there, then re-runs step 9 so the
# MCP's market alerts use the new chat id and their topic.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
ssh tradedesk@"$SERVER" 'cd ~/tradedesk-mcp && git pull -q --ff-only && python3 scripts/telegram_topics.py'
ssh nick@"$SERVER" 'sudo bash -s' <"$(dirname "$0")/09-alerts-telegram.sh"
ssh tradedesk@"$SERVER" 'cd ~/tradedesk-mcp && python3 scripts/agents_status.py && echo "status sent to its topic"'
