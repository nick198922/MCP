#!/bin/bash
# From the Mac: is COINALYZE_API_KEY in the MCP service environment, and does Coinalyze accept it from the server?
# Prints only yes/no and HTTP codes, never the key. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'grep -q "^COINALYZE_API_KEY=." /etc/tradedesk-mcp.env && echo "key in /etc/tradedesk-mcp.env: yes" || echo "key in /etc/tradedesk-mcp.env: no"
systemctl show tradedesk-mcp -p ActiveState -p ActiveEnterTimestamp --value | paste -sd" " -
cd ~/tradedesk-mcp && ~/.local/bin/uv run -q python -c "
import os, re, httpx
env = open(\"/etc/tradedesk-mcp.env\").read()
m = re.search(r\"^COINALYZE_API_KEY=(.+)$\", env, re.M)
if m:
    r = httpx.get(\"https://api.coinalyze.net/v1/exchanges\", headers={\"api_key\": m.group(1).strip()}, timeout=20)
    print(\"coinalyze from the server:\", r.status_code, len(r.json()) if r.status_code == 200 else r.text[:80], \"exchanges\")
"'
