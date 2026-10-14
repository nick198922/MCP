#!/bin/bash
# From the Mac: what the agents loop is doing right now (last log lines).
ssh nick@"${TRADEDESK_SERVER:-169.58.169.184}" 'sudo journalctl -u tradedesk-agents-loop -n 15 --no-pager'
