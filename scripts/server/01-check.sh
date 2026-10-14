#!/bin/bash
# Step 1 of the server move: show the machine and check that every venue the project uses answers from here.
# Run from the Mac:  ssh root@SERVER 'bash -s' < scripts/server/01-check.sh
set -u

echo "== machine"
hostname
. /etc/os-release && echo "$PRETTY_NAME"
echo "cpus: $(nproc)"
free -h | sed -n 2p
df -h / | tail -1
echo "ip geo: $(curl -s -m 10 https://ipinfo.io/json | tr -d '\n ' | head -c 200)"

echo
echo "== venues (HTTP status; 200 = ok, 403/451 = blocked)"
check() {
  local name="$1" url="$2" body
  shift 2
  body="$(curl -s -m 15 -w '\n%{http_code}' "$@" "$url")"
  local code="${body##*$'\n'}"
  printf '%-22s %s' "$name" "$code"
  if [[ "$code" != "200" ]]; then
    printf '  %s' "$(echo "${body%$'\n'*}" | tr -d '\n' | head -c 160)"
  fi
  echo
}
check "binance futures" "https://fapi.binance.com/fapi/v1/ping"
check "binance spot" "https://api.binance.com/api/v3/ping"
check "bybit" "https://api.bybit.com/v5/market/time"
check "okx" "https://www.okx.com/api/v5/public/time"
check "deribit" "https://www.deribit.com/api/v2/public/get_time"
check "hyperliquid" "https://api.hyperliquid.xyz/info" -X POST -H "Content-Type: application/json" -d '{"type":"meta"}'
check "github" "https://api.github.com"
check "anthropic (any code ok)" "https://api.anthropic.com" -o /dev/null
