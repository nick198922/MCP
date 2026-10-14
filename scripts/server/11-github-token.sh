#!/bin/bash
# From the Mac, in the Terminal app (it asks for the token, so it does not work through Claude's "!"):
#   bash ~/MCP/tradedesk-mcp/scripts/server/11-github-token.sh
# Lets the agents' launcher and the doctor open pull requests themselves (scripts/gh_pr.py).
# Before: github.com -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate:
#   Repository access: Only select repositories -> nick198922/MCP
#   Permissions -> Repository: Pull requests: Read and write; Contents: Read-only (Metadata: Read-only comes with it)
# Nothing else: the token can open pull requests but cannot push, merge or change settings.
# The token goes straight into the server's ~/.config/tradedesk/github.env (0600): never into git, logs or chat.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"

read -rsp "GitHub fine-grained token (hidden while you type): " token
echo
if [[ ! "$token" =~ ^github_pat_[A-Za-z0-9_]{20,}$ ]]; then
  echo "This does not look like a fine-grained token (github_pat_...). Run it again in the Terminal app and paste the token."
  exit 1
fi
printf 'GITHUB_TOKEN=%s\n' "$token" | ssh tradedesk@"$SERVER" \
  'umask 077 && mkdir -p ~/.config/tradedesk && cat >~/.config/tradedesk/github.env && chmod 600 ~/.config/tradedesk/github.env'
ssh tradedesk@"$SERVER" 'git -C ~/tradedesk-mcp pull -q --ff-only; python3 ~/tradedesk-mcp/scripts/gh_pr.py --repo nick198922/MCP --check'
echo "done: the agents will open pull requests themselves"
