#!/bin/bash
# From the Mac: ask Binance to prepare the futures trade-history files for the last two years (two 1-year requests,
# each counted against Binance's 5 downloads a month), then fetch whatever is ready. Re-run later to fetch the rest.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && git pull -q --ff-only &&
U=~/.local/bin/uv; P="scripts/binance_import.py --env /etc/tradedesk-binance.env"
y1=$(date -u -d "1 year ago" +%F); y2=$(date -u -d "2 years ago + 1 day" +%F); t=$(date -u +%F)
$U run -q python $P request --from "$y1" --to "$t" && $U run -q python $P request --from "$y2" --to "$y1" && $U run -q python $P fetch'
