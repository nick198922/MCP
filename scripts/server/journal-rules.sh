#!/bin/bash
# From the Mac: replay the owner's draft trading rules on the imported journal (read-only what-if).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && git pull -q --ff-only && ~/.local/bin/uv run -q python scripts/journal_rules.py'
