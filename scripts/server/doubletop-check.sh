#!/bin/bash
# From the Mac: does the agents' Claude Code on the server see the claude.ai DOUBLETOP connector? Lists the MCP servers
# Claude Code knows for the tradedesk user (claude.ai connectors appear as "claude.ai <name>") and their status.
# Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'export PATH="$HOME/.local/bin:$PATH"; cd ~/tradedesk-agents 2>/dev/null || cd ~; claude --version; timeout 90 claude mcp list 2>&1 | head -40'
