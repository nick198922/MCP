#!/bin/bash
# From the Mac, in the Terminal app (it asks for the key, so it does not work through Claude's "!"):
#   bash ~/MCP/tradedesk-mcp/scripts/server/coinalyze-key.sh
# Before: a free account at https://coinalyze.net, then the API key from the account page (free tier, ~40 requests/min).
# The key is checked against the Coinalyze API, then written as COINALYZE_API_KEY into the MCP service environment
# (/etc/tradedesk-mcp.env, root:tradedesk 0640, through nick's sudo) and the service is restarted. It never goes on a
# command line, into git, logs or chat. The agents do not get it: Coinalyze history is fetched by the MCP service.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "Coinalyze API key (hidden while you type): " key
echo
if [[ ! "$key" =~ ^[A-Za-z0-9-]{16,64}$ ]]; then
  echo "This does not look like a Coinalyze key (letters, digits, dashes). Run it again and paste the key."
  exit 1
fi
# The key goes to curl through stdin (a config file), never the command line. Coinalyze takes it as the api_key header
# or the api_key query parameter: try the header first, then the parameter.
try() { curl -s -m 20 -o /dev/null -w '%{http_code}' -K - || true; }
code="$(printf 'header = "api_key: %s"\nurl = "https://api.coinalyze.net/v1/exchanges"\n' "$key" | try)"
if [[ "$code" != "200" ]]; then
  code="$(printf 'url = "https://api.coinalyze.net/v1/exchanges?api_key=%s"\n' "$key" | try)"
fi
if [[ "$code" != "200" ]]; then
  echo "Coinalyze did not accept the key (HTTP ${code:-none}). The key you pasted: ${#key} characters, ending in …${key: -2}."
  echo "Compare it with https://coinalyze.net/account/api-key/ (generate one there if the page has none; a new account may"
  echo "need its e-mail confirmed first), then run again."
  exit 1
fi
echo "Coinalyze accepted the key."
printf '%s\n' "$key" | ssh nick@"$SERVER" 'sudo bash -c '"'"'
set -euo pipefail
read -r k
ENV=/etc/tradedesk-mcp.env
tmp="$(mktemp /etc/tradedesk-mcp.env.XXXXXX)"
grep -v "^COINALYZE_API_KEY=" "$ENV" >"$tmp" || true
printf "COINALYZE_API_KEY=%s\n" "$k" >>"$tmp"
chown root:tradedesk "$tmp"
chmod 640 "$tmp"
mv "$tmp" "$ENV"
systemctl restart tradedesk-mcp
sleep 5
systemctl is-active tradedesk-mcp
'"'"''
echo "done: COINALYZE_API_KEY is in the MCP service environment"
