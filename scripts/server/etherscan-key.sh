#!/bin/bash
# Give the MCP service the Etherscan key the research scripts already use (question Q-20261119-1, onchain_* tools).
# Run through nick (root is needed: the service reads only /etc/tradedesk-mcp.env, and ProtectHome=yes hides
# ~tradedesk/.config/tradedesk/onchain.env from it):
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/etherscan-key.sh
# Copies ETHERSCAN_API_KEY from /home/tradedesk/.config/tradedesk/onchain.env (stored there 25.10 by onchain-keys.sh),
# checks it against Etherscan, writes it into /etc/tradedesk-mcp.env (root:tradedesk 0640) and restarts tradedesk-mcp.
# The key never goes on a command line, into git, logs or output. A free read-only data key: it cannot move funds. It
# shares the free limit (3 requests/s, 100k a day) with the research scripts; the MCP service keeps 2/s.
# Undo: delete the ETHERSCAN_API_KEY line from /etc/tradedesk-mcp.env and `systemctl restart tradedesk-mcp`.
set -euo pipefail
SRC=/home/tradedesk/.config/tradedesk/onchain.env
ENV=/etc/tradedesk-mcp.env
test -r "$SRC" || { echo "no $SRC: run scripts/server/onchain-keys.sh first"; exit 1; }
test -f "$ENV" || { echo "no $ENV: run step 05 first"; exit 1; }
key="$(grep -m1 '^ETHERSCAN_API_KEY=' "$SRC" | cut -d= -f2- | tr -d '[:space:]' || true)"
if [[ ! "$key" =~ ^[A-Za-z0-9]{20,64}$ ]]; then
  echo "ETHERSCAN_API_KEY in $SRC is missing or does not look like an Etherscan key (letters and digits)."
  exit 1
fi
# The key goes to curl through stdin (a config file), never the command line.
body="$(printf 'url = "https://api.etherscan.io/v2/api?chainid=1&module=stats&action=ethprice&apikey=%s"\n' "$key" |
  curl -s -m 20 -K - || true)"
if [[ "$body" != *'"status":"1"'* ]]; then
  echo "Etherscan did not accept the stored key (or did not answer). Nothing changed."
  exit 1
fi
echo "Etherscan accepted the key."
tmp="$(mktemp /etc/tradedesk-mcp.env.XXXXXX)"
grep -v '^ETHERSCAN_API_KEY=' "$ENV" >"$tmp" || true
printf 'ETHERSCAN_API_KEY=%s\n' "$key" >>"$tmp"
chown root:tradedesk "$tmp"
chmod 640 "$tmp"
mv "$tmp" "$ENV"
systemctl restart tradedesk-mcp
sleep 5
systemctl is-active tradedesk-mcp
echo "done: ETHERSCAN_API_KEY is in the MCP service environment"
