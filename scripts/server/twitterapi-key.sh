#!/bin/bash
# From the Mac, in the Terminal app (it asks for the key):
#   bash <(ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/server/twitterapi-key.sh)
# Before: an account at https://twitterapi.io (Google sign-in is enough), then the API key from its dashboard.
# Lets scripts/x_backfill.py download the X timelines of the traders the owner follows (owner 13.11).
# The key is checked against twitterapi.io, then written to the server's ~/.config/tradedesk/twitterapi.env (0600)
# as TWITTERAPI_KEY. It never goes on a command line, into git, logs or chat; the MCP service does not get it.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "twitterapi.io API key (hidden while you type): " key
echo
if [[ ! "$key" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
  echo "This does not look like a twitterapi.io key (letters, digits, - or _). Run it again and paste the key."
  exit 1
fi
# The key goes to curl through stdin (a config file), never the command line.
code="$(printf 'header = "X-API-Key: %s"\nurl = "https://api.twitterapi.io/twitter/user/info?userName=Trader_XO"\n' "$key" |
  curl -s -m 20 -o /dev/null -w '%{http_code}' -K - || true)"
if [[ "$code" != "200" ]]; then
  echo "twitterapi.io did not accept the key (HTTP ${code:-none}). The key you pasted: ${#key} characters, ending in …${key: -2}."
  echo "Copy it again from the twitterapi.io dashboard and run again."
  exit 1
fi
echo "twitterapi.io accepted the key."
printf 'TWITTERAPI_KEY=%s\n' "$key" | ssh tradedesk@"$SERVER" \
  'umask 077 && mkdir -p ~/.config/tradedesk && cat >~/.config/tradedesk/twitterapi.env && chmod 600 ~/.config/tradedesk/twitterapi.env'
echo "done: tell Claude in Telegram «ключ X готовий»"
