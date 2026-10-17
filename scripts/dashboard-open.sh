#!/bin/bash
# On the Mac: open the project dashboard. Starts an SSH tunnel to the server's 127.0.0.1:8090 (if not running yet) and
# opens http://localhost:8090 in the browser. The page is never exposed to the internet.
#   bash ~/MCP/tradedesk-mcp/scripts/dashboard-open.sh
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
PORT="${TRADEDESK_DASHBOARD_PORT:-8090}"
if ! nc -z localhost "$PORT" 2>/dev/null; then
  ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L "$PORT:127.0.0.1:8090" tradedesk@"$SERVER"
  sleep 1
fi
open "http://localhost:$PORT"
echo "dashboard: http://localhost:$PORT (the tunnel stays open in the background; close it with: pkill -f 'L $PORT:127.0.0.1:8090')"
