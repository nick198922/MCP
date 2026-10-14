#!/bin/bash
# From the Mac: dry-run the agents on the server and print the summary.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" \
  '~/tradedesk-mcp/scripts/nightly_agents.sh --dry-run; cat "$(ls -t ~/.local/state/tradedesk-agents/*.summary.md | head -1)"'
