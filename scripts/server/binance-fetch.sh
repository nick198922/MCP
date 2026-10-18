#!/bin/bash
# From the Mac: download the Binance history files that are ready (requests made by binance-history.sh).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/binance_import.py --env /etc/tradedesk-binance.env fetch'
