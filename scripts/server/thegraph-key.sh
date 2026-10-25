#!/bin/bash
# From the Mac, in the Terminal app (it asks for the key, so it does not work through Claude's "!"):
#   ssh tradedesk@169.58.169.184 cat tradedesk-chat/scripts/server/thegraph-key.sh > /tmp/g.sh && bash /tmp/g.sh
# Before (owner's decision 25.10, card IDEA-20261015-smart-money-persistence: trader P&L history from the Polymarket
#   subgraphs): a free API key at https://thegraph.com/studio/apikeys/ (free tier, 100k queries a month).
# The key goes over ssh stdin, as tradedesk (no sudo), into ~/.config/tradedesk/onchain.env (0600) as GRAPH_API_KEY,
# next to the Etherscan / Dune keys, and is checked ON THE SERVER with one query to the main Polymarket subgraph. It
# never goes on a command line, into git, logs or chat. A read-only data key: it cannot move funds.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
read -rsp "The Graph API key (hidden, Enter = keep the stored one and only check it): " key; echo
if [[ -n "$key" && ! "$key" =~ ^[A-Za-z0-9]{20,64}$ ]]; then echo "This does not look like a The Graph API key (letters and digits)."; exit 1; fi
printf '%s\n' "$key" | ssh tradedesk@"$SERVER" 'python3 -I -c "
import json, os, sys, urllib.error, urllib.request
from pathlib import Path
key = sys.stdin.readline().strip()
f = Path.home() / \".config/tradedesk/onchain.env\"
old = {}
if f.exists():
    for line in f.read_text().splitlines():
        k, _, v = line.partition(\"=\")
        old[k.strip()] = v.strip()
if key:
    old[\"GRAPH_API_KEY\"] = key
key = old.get(\"GRAPH_API_KEY\", \"\")
if not key:
    sys.exit(\"no key stored yet: run again and paste it\")
f.parent.mkdir(parents=True, exist_ok=True)
tmp = f.with_suffix(\".tmp\")
tmp.write_text(\"\".join(f\"{k}={v}\n\" for k, v in old.items() if v))
os.chmod(tmp, 0o600)
tmp.replace(f)
url = \"https://gateway.thegraph.com/api/\" + key + \"/subgraphs/id/Bx1W4S7kDVxs9gC3s2G6DS8kdNBJNVhMviCtin2DiBp\"
req = urllib.request.Request(url, data=json.dumps({\"query\": \"{ _meta { block { number } } }\"}).encode(), headers={\"Content-Type\": \"application/json\", \"User-Agent\": \"tradedesk-mcp/1.0\"})  # Cloudflare refuses the default Python-urllib agent (error 1010)
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    block = (((body.get(\"data\") or {}).get(\"_meta\") or {}).get(\"block\") or {}).get(\"number\")
    if block:
        print(\"The Graph: ok (Polymarket subgraph at block\", block, \")\")
    else:
        print(\"The Graph: NOT accepted:\", str(body.get(\"errors\"))[:200])
except urllib.error.HTTPError as e:
    print(\"The Graph: NOT accepted (HTTP\", e.code, \")\")
except Exception as e:
    print(\"The Graph: no answer:\", type(e).__name__)
print(\"stored in ~/.config/tradedesk/onchain.env\")
"'
