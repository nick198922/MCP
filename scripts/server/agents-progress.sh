#!/bin/bash
# From the Mac: the latest actions of the running (or last) agent step, from its readable transcript.
#   agents-progress.sh [LINES] [full]   (full: every line, not only headings, messages and tool calls)
FILTER='^## |^### |^\*\*|^  - '
[[ "${2:-}" == full ]] && FILTER='.'
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" \
  "cd ~/tradedesk-mcp && ~/.local/bin/uv run --no-project python scripts/agents_transcript.py --project=-home-tradedesk-tradedesk-agents | grep -E '$FILTER' | tail -${1:-30}"
