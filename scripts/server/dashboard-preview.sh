#!/bin/bash
# From the Mac: build the dashboard page once on the server and save it locally (default /tmp/tradedesk-dashboard.html),
# without the service or a tunnel. Reads only.
out="${1:-/tmp/tradedesk-dashboard.html}"
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && f=$(mktemp) && python3 -I scripts/dashboard.py once --out "$f" >/dev/null && cat "$f"; rm -f "$f"' >"$out"
echo "-> $out ($(wc -c <"$out") bytes)"
