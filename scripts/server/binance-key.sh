#!/bin/bash
# From the Mac, in the Terminal app (it asks for the key, so it does not work through Claude's "!"):
#   bash ~/MCP/tradedesk-mcp/scripts/server/bybit-key.sh
# Before: Binance -> API Management -> Create API -> System generated; restrictions: ONLY «Enable Reading» (no spot/margin,
# futures, options, transfers or withdrawals), «Restrict access to trusted IPs only» with the server's 169.58.169.184.
# The key and secret go over ssh stdin into /etc/tradedesk-binance.env (root:tradedesk 0640, a file of its own: the MCP
# service does not load it) and are checked ON THE SERVER: the script asks Binance for the key's own restrictions and
# deletes the file again unless the key can only read. They never go on a command line, into git, logs or chat.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "Binance API key (hidden): " key; echo
read -rsp "Binance API secret (hidden): " secret; echo
if [[ ! "$key" =~ ^[A-Za-z0-9]{40,80}$ || ! "$secret" =~ ^[A-Za-z0-9]{40,80}$ ]]; then
  echo "This does not look like a Binance key pair (letters and digits). Run again and paste key, then secret."
  exit 1
fi
printf '%s\n%s\n' "$key" "$secret" | ssh nick@"$SERVER" 'sudo bash -c '"'"'
set -euo pipefail
read -r k; read -r s
F=/etc/tradedesk-binance.env
tmp="$(mktemp /etc/tradedesk-binance.env.XXXXXX)"
printf "BINANCE_API_KEY=%s\nBINANCE_API_SECRET=%s\n" "$k" "$s" >"$tmp"
chown root:tradedesk "$tmp"; chmod 640 "$tmp"; mv "$tmp" "$F"
cd /home/tradedesk/tradedesk-mcp
if ! runuser -u tradedesk -- git pull -q --ff-only; then echo "git pull failed"; fi
if runuser -u tradedesk -- /home/tradedesk/.local/bin/uv run -q python scripts/binance_import.py --env "$F" check; then
  echo "done: the read-only Binance key is in $F"
else
  rm -f "$F"; echo "the key was NOT kept (see the message above)"; exit 1
fi
'"'"''
