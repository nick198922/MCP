#!/bin/bash
# From the Mac, in the Terminal app (it asks for the key, so it does not work through Claude's "!"):
#   bash ~/MCP/tradedesk-mcp/scripts/server/bybit-key.sh
# Before: Bybit -> API -> Create New Key -> System-generated API Keys, permission "Read-Only" (every read box ticked),
# IP restriction ON with only the server's address 169.58.169.184. A key that can trade or withdraw is refused.
# The key and secret go over ssh stdin into /etc/tradedesk-bybit.env (root:tradedesk 0640, a file of its own: the MCP
# service does not load it) and are checked ON THE SERVER (the key only works from the server's IP): the script asks Bybit
# for the key's own permissions and deletes the file again unless the key is read-only. They never go on a command line,
# into git, logs or chat. Used only by scripts/bybit_import.py (journal import, read-only endpoints).
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "Bybit API key (hidden): " key; echo
read -rsp "Bybit API secret (hidden): " secret; echo
if [[ ! "$key" =~ ^[A-Za-z0-9]{10,40}$ || ! "$secret" =~ ^[A-Za-z0-9]{20,64}$ ]]; then
  echo "This does not look like a Bybit system-generated key pair (letters and digits). Run again and paste key, then secret."
  exit 1
fi
printf '%s\n%s\n' "$key" "$secret" | ssh nick@"$SERVER" 'sudo bash -c '"'"'
set -euo pipefail
read -r k; read -r s
F=/etc/tradedesk-bybit.env
tmp="$(mktemp /etc/tradedesk-bybit.env.XXXXXX)"
printf "BYBIT_API_KEY=%s\nBYBIT_API_SECRET=%s\n" "$k" "$s" >"$tmp"
chown root:tradedesk "$tmp"; chmod 640 "$tmp"; mv "$tmp" "$F"
cd /home/tradedesk/tradedesk-mcp
if ! runuser -u tradedesk -- git pull -q --ff-only; then echo "git pull failed"; fi
if runuser -u tradedesk -- /home/tradedesk/.local/bin/uv run -q python scripts/bybit_import.py --env "$F" check; then
  echo "done: the read-only Bybit key is in $F"
else
  rm -f "$F"; echo "the key was NOT kept (see the message above)"; exit 1
fi
'"'"''
