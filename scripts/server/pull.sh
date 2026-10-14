#!/bin/bash
# From the Mac: update the server's main clone now (otherwise it pulls at 02:15). Safe while an agent step runs.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'git -C ~/tradedesk-mcp pull --ff-only && git -C ~/tradedesk-mcp log -1 --oneline'
