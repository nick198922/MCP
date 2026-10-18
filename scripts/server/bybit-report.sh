#!/bin/bash
# From the Mac: print the Bybit journal report kept on the server (read-only; the report never leaves the server's disk
# except into this terminal).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cat /var/lib/tradedesk-mcp/journal/bybit-report.md 2>/dev/null || echo "no report yet: run scripts/server/bybit-import.sh"'
