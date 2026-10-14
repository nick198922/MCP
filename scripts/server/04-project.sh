#!/bin/bash
# Step 4 of the server move: clone the repo with the deploy key, install, lint, test, live smoke,
# and prove the server can push agent/* branches.
#   ssh tradedesk@SERVER 'bash -s' < scripts/server/04-project.sh
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME"

echo "== clone"
[[ -d tradedesk-mcp/.git ]] || git clone -q git@github.com:nick198922/MCP.git tradedesk-mcp
cd tradedesk-mcp
git config user.name "Mykola Khanetskyi (server)"
git config user.email "n.hanetskiy@gmail.com"
git log -1 --oneline

echo "== install, lint, tests"
uv sync --quiet --extra dev
make lint
make test

echo "== live smoke against the venues"
make smoke

echo "== push check: create and delete a throwaway agent/ branch"
probe="agent/0000-00-00-server-push-check"
git push -q origin "HEAD:refs/heads/$probe"
git push -q origin --delete "$probe"
echo "server can push agent/* branches"
