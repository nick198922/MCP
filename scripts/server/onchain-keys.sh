#!/bin/bash
# From the Mac, in the Terminal app (it asks for the keys, so it does not work through Claude's "!"):
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/server/onchain-keys.sh > /tmp/k.sh && bash /tmp/k.sh
# Before (owner's decision 25.10, card IDEA-20261017-market-makers, part B): free accounts at
#   https://etherscan.io/myapikey  (API key, free tier) and  https://dune.com/settings/api  (API key, free tier).
# Either key may be left empty (Enter) to keep the one already stored. The keys go over ssh stdin, as tradedesk (no
# sudo), into ~/.config/tradedesk/onchain.env (0600) as ETHERSCAN_API_KEY / DUNE_API_KEY, and are checked ON THE SERVER
# by a short Python check reading that file. They never go on a command line, into git, logs or chat. Read-only data
# keys: neither service can move funds.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "Etherscan API key (hidden, Enter = keep): " eth; echo
read -rsp "Dune API key (hidden, Enter = keep): " dune; echo
if [[ -n "$eth" && ! "$eth" =~ ^[A-Za-z0-9]{20,64}$ ]]; then echo "This does not look like an Etherscan key (letters and digits)."; exit 1; fi
if [[ -n "$dune" && ! "$dune" =~ ^[A-Za-z0-9_-]{20,80}$ ]]; then echo "This does not look like a Dune key."; exit 1; fi
printf '%s\n%s\n' "$eth" "$dune" | ssh tradedesk@"$SERVER" 'python3 -I -c "
import json, os, sys, urllib.error, urllib.request
from pathlib import Path
eth, dune = sys.stdin.readline().strip(), sys.stdin.readline().strip()
f = Path.home() / \".config/tradedesk/onchain.env\"
old = {}
if f.exists():
    for line in f.read_text().splitlines():
        k, _, v = line.partition(\"=\")
        old[k.strip()] = v.strip()
if eth: old[\"ETHERSCAN_API_KEY\"] = eth
if dune: old[\"DUNE_API_KEY\"] = dune
f.parent.mkdir(parents=True, exist_ok=True)
tmp = f.with_suffix(\".tmp\")
tmp.write_text(\"\".join(f\"{k}={v}\n\" for k, v in old.items() if v))
os.chmod(tmp, 0o600)
tmp.replace(f)
def get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=20) as r:
            return r.status, r.read()[:400]
    except urllib.error.HTTPError as e:
        return e.code, b\"\"
    except Exception as e:
        return 0, str(type(e).__name__).encode()
ok = True
if old.get(\"ETHERSCAN_API_KEY\"):
    code, body = get(\"https://api.etherscan.io/v2/api?chainid=1&module=stats&action=ethprice&apikey=\" + old[\"ETHERSCAN_API_KEY\"])
    good = code == 200 and b\"\\\"status\\\":\\\"1\\\"\" in body
    ok &= good
    print(\"Etherscan:\", \"ok\" if good else f\"NOT accepted (HTTP {code})\")
if old.get(\"DUNE_API_KEY\"):
    code, _ = get(\"https://api.dune.com/api/v1/query/1/results?limit=1\", {\"X-Dune-API-Key\": old[\"DUNE_API_KEY\"]})
    good = code not in (0, 401)
    ok &= good
    print(\"Dune:\", \"ok\" if good else f\"NOT accepted (HTTP {code})\")
print(\"stored in ~/.config/tradedesk/onchain.env\" if ok else \"stored, but check the key above and run again\")
"'
