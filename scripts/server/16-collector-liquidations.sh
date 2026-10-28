#!/bin/bash
# Step 16: turn on the collector's live liquidation streams (WebSocket: Binance all symbols, Bybit BTC/ETH/SOL, OKX
# swaps) into the `liquidations` table, so `liquidations_summary` has data and the liquidation-cascade tests get
# event-level forward data next to the Coinalyze history. Off by default since Phase 2; found off on 2026-10-17.
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/16-collector-liquidations.sh
set -euo pipefail
ENV=/etc/tradedesk-mcp.env
tmp="$(mktemp /etc/tradedesk-mcp.env.XXXXXX)"
grep -v -E '^TRADEDESK_COLLECT_LIQUIDATIONS_(ENABLED|VENUES)=' "$ENV" >"$tmp" || true
# Binance only: the Bybit (side) and OKX (contracts read as coins) liquidation parsers are buggy, see docs/BACKLOG.md.
printf 'TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED=1\nTRADEDESK_COLLECT_LIQUIDATIONS_VENUES=binance\n' >>"$tmp"
chown root:tradedesk "$tmp"
chmod 640 "$tmp"
mv "$tmp" "$ENV"
systemctl restart tradedesk-mcp
sleep 90
systemctl is-active tradedesk-mcp
runuser -u tradedesk -- python3 -I -c "
import sqlite3, time
con = sqlite3.connect('file:/var/lib/tradedesk-mcp/tradedesk.sqlite?mode=ro', uri=True)
n, last = con.execute('SELECT count(*), max(ts) FROM liquidations').fetchone()
print('liquidations rows:', n, 'newest', round(time.time() - last) if last else None, 's ago')"
