#!/bin/bash
# From the Mac: disk use on the server — the root filesystem, the MCP data dir (SQLite), the agents' research cache and
# clone, logs. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'df -h / | tail -1
du -sh /var/lib/tradedesk-mcp ~/.cache/tradedesk-agents ~/tradedesk-agents ~/tradedesk-mcp ~/.local/state/tradedesk-agents 2>/dev/null
ls -la /var/lib/tradedesk-mcp/*.db* 2>/dev/null | awk "{print \$5, \$9}"'
