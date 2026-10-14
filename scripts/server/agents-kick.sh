#!/bin/bash
# From the Mac: restart the agents loop now (instead of waiting for its next check) and show its log.
ssh nick@"${TRADEDESK_SERVER:-169.58.169.184}" 'sudo systemctl restart tradedesk-agents-loop && sleep 15 && sudo journalctl -u tradedesk-agents-loop -n 8 --no-pager'
