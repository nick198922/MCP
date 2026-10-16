#!/bin/bash
# From the Mac: the latest actions of the running (or last) agent step, from its readable transcript.
#   agents-progress.sh [LINES] [full]   (full: every line, not only headings, messages and tool calls)
#   agents-progress.sh denied [HOURS]   (tool calls the permission rules refused, as in the doctor's daily report)
if [[ "${1:-}" == denied ]]; then
  hours="${2:-24}"; [[ "$hours" =~ ^[0-9]+$ ]] || hours=24
  exec ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" \
    "cd ~/tradedesk-mcp && ~/.local/bin/uv run --no-project python scripts/agents_transcript.py --project=-home-tradedesk-tradedesk-agents --denied $hours"
fi
FILTER='^## |^### |^\*\*|^  - '
[[ "${2:-}" == full ]] && FILTER='.'
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" \
  "cd ~/tradedesk-mcp && ~/.local/bin/uv run --no-project python scripts/agents_transcript.py --project=-home-tradedesk-tradedesk-agents | grep -E '$FILTER' | tail -${1:-30}"
