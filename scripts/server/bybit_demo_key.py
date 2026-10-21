"""Save the owner's Bybit DEMO TRADING API key on the server and check it (run by the owner, it asks for the secret).

From the Mac Terminal, one line (the -t gives the hidden secret prompt a terminal):
    ssh -t tradedesk@169.58.169.184 python3 tradedesk-mcp/scripts/server/bybit_demo_key.py

The key goes to ~/.config/tradedesk/bybit-demo.env (0600) only after Bybit's DEMO endpoint (api-demo.bybit.com) accepts it,
so a real-account key cannot be saved by mistake: the demo endpoint does not know real keys. Checks from
GET /v5/user/query-api: read-write, IP allow-list is exactly the server, no withdrawal or transfer rights. Nothing is
printed or logged except the checks and the demo balance. No order is placed here.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://api-demo.bybit.com"
SERVER_IP = "169.58.169.184"
ENV_FILE = Path.home() / ".config" / "tradedesk" / "bybit-demo.env"
RECV_WINDOW = "10000"
# Permission groups a trading key needs (at least one), and ones it must not have.
NEEDED = ("ContractTrade", "Spot", "Derivatives")
FORBIDDEN = ("Wallet", "Withdraw", "Exchange", "FiatP2P", "Affiliate")


def sign(secret: str, ts: str, key: str, query: str) -> str:
    """Bybit V5 HMAC-SHA256 of timestamp + api_key + recv_window + query string."""
    return hmac.new(secret.encode(), (ts + key + RECV_WINDOW + query).encode(), hashlib.sha256).hexdigest()


def key_problems(info: dict, server_ip: str = SERVER_IP) -> list[str]:
    """Problems with the key's settings from /v5/user/query-api ``result``; empty = fine."""
    out = []
    if int(info.get("readOnly", 1)) != 0:
        out.append("the key is read-only: choose Read-Write")
    perms = info.get("permissions") or {}
    if not any(perms.get(g) for g in NEEDED):
        out.append("no trading permission: tick Orders and Positions (Contracts / Unified Trading) and Trade (Spot)")
    bad = [g for g in FORBIDDEN if perms.get(g)]
    if bad:
        out.append(f"untick {', '.join(bad)}: the server only needs trading")
    ips = [ip.strip() for ip in (info.get("ips") or []) if ip.strip()]
    if ips != [server_ip]:
        out.append(f"IP restriction must be only {server_ip} (now: {', '.join(ips) or 'none'})")
    return out


def call(key: str, secret: str, path: str, query: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    req = urllib.request.Request(
        f"{BASE}{path}" + (f"?{query}" if query else ""),
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign(secret, ts, key, query),
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> int:
    print("Bybit DEMO TRADING key (from demo mode: avatar -> API). The secret is hidden while you type.")
    key = input("API key: ").strip()
    secret = getpass.getpass("API secret: ").strip()
    if not (10 <= len(key) <= 64 and key.isalnum() and 20 <= len(secret) <= 128 and secret.isalnum()):
        print("This does not look like a Bybit key and secret (letters and digits). Run it again and paste them.")
        return 1
    try:
        info = call(key, secret, "/v5/user/query-api")
    except OSError as e:
        print(f"No answer from {BASE}: {e}")
        return 1
    if info.get("retCode") != 0:
        print(f"Bybit DEMO did not accept the key: {info.get('retMsg')} (code {info.get('retCode')}).")
        print("Most likely it is a key of the real account: create it again after switching to Demo Trading.")
        return 1
    problems = key_problems(info.get("result") or {})
    if problems:
        print("The key works, but fix it on Bybit (API -> Edit) and run this again:")
        for p in problems:
            print(f"  - {p}")
        return 1
    ENV_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"BYBIT_DEMO_API_KEY={key}\nBYBIT_DEMO_API_SECRET={secret}\n")
    os.chmod(ENV_FILE, 0o600)
    print(f"saved: {ENV_FILE} (0600)")
    try:
        bal = call(key, secret, "/v5/account/wallet-balance", "accountType=UNIFIED")
        equity = (bal.get("result") or {}).get("list", [{}])[0].get("totalEquity")
        print(f"demo balance: {equity} USD" if bal.get("retCode") == 0 else f"balance: {bal.get('retMsg')}")
    except (OSError, IndexError, ValueError) as e:
        print(f"balance not read ({e}); the key is saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
