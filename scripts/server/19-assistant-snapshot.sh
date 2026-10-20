#!/bin/bash
# Step 19: turn on the collector job `assistant_snapshot` (docs/ideas/IDEA-20261018-assistant.md): every 10 minutes it
# writes one JSON file with the data for the dashboard's brief and options tabs (tickers, DVOL, VRP, term structure, skew,
# expected move, option chain, funding, macro calendar). Read-only market data, no keys, no orders.
# Off by default; this script sets TRADEDESK_ASSISTANT_SNAPSHOT_ENABLED=1 in /etc/tradedesk-mcp.env and restarts the MCP service.
# The file: /var/lib/tradedesk-mcp/assistant/snapshot.json (written atomically, readable by the dashboard).
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/19-assistant-snapshot.sh
set -euo pipefail
SVC=tradedesk
ENV=/etc/tradedesk-mcp.env
DIR=/var/lib/tradedesk-mcp/assistant
FILE="$DIR/snapshot.json"
grep -q -E '^TRADEDESK_COLLECTOR_ENABLED=1' "$ENV" || { echo "collector is off in $ENV: run scripts/server/05-services.sh first"; exit 1; }
runuser -u "$SVC" -- git -C "/home/$SVC/tradedesk-mcp" pull -q --ff-only
test -f "/home/$SVC/tradedesk-mcp/src/tradedesk/collector/assistant_snapshot.py"
runuser -u "$SVC" -- "/home/$SVC/.local/bin/uv" --directory "/home/$SVC/tradedesk-mcp" sync --quiet --extra dev
install -d -o "$SVC" -g "$SVC" "$DIR"
tmp="$(mktemp /etc/tradedesk-mcp.env.XXXXXX)"
grep -v -E '^TRADEDESK_ASSISTANT_SNAPSHOT_(ENABLED|SECONDS|PATH)=' "$ENV" >"$tmp" || true
printf 'TRADEDESK_ASSISTANT_SNAPSHOT_ENABLED=1\nTRADEDESK_ASSISTANT_SNAPSHOT_SECONDS=600\nTRADEDESK_ASSISTANT_SNAPSHOT_PATH=%s\n' "$FILE" >>"$tmp"
chown root:"$SVC" "$tmp"
chmod 640 "$tmp"
mv "$tmp" "$ENV"
systemctl restart tradedesk-mcp
sleep 150
systemctl is-active tradedesk-mcp
runuser -u "$SVC" -- python3 -I -c "
import json, sys, time
try:
    d = json.load(open('$FILE'))
except Exception as e:
    sys.exit('snapshot not there yet: %s (see journalctl -u tradedesk-mcp | grep assistant)' % e)
print('asof', d['asof_utc'], 'sections', len(d['sections']), 'missing', d['missing'])"
