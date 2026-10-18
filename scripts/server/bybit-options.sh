#!/bin/bash
# From the Mac: the owner's Bybit options — fill and delivery counts for 2 years and the open option positions with
# greeks (read-only key). Raw records are saved on the server, outside git.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "cd ~/tradedesk-mcp && git pull -q --ff-only &&
~/.local/bin/uv run -q python scripts/bybit_import.py options-probe --save /var/lib/tradedesk-mcp/journal/bybit-options-raw.json"
