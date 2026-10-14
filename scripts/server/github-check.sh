#!/bin/bash
# From the Mac: can the server open pull requests with its GitHub token? Prints the result only, never the token.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'python3 ~/tradedesk-mcp/scripts/gh_pr.py --repo nick198922/MCP --check'
