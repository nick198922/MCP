"""Import the owner's Binance USD-M futures history into the trade journal. Read-only.

    uv run python scripts/binance_import.py --env /etc/tradedesk-binance.env check
    uv run python scripts/binance_import.py request --from 2024-10-19 --to 2025-10-18     # ask Binance to prepare a file
    uv run python scripts/binance_import.py fetch                                          # download prepared files

The key pair is read from the env file (never from the command line, never printed). `check` asks Binance for the key's
own restrictions and refuses any key that can trade, transfer or withdraw. Long history comes from the asynchronous
trade-history download (`/fapi/v1/trade/asyn`: at most 1 year per request and 5 requests a month, shared with the
website), because the regular trade endpoint keeps only 6 months. Requests and files live in the server's journal
directory, outside git.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DANGEROUS = [
    "enableWithdrawals",
    "enableInternalTransfer",
    "permitsUniversalTransfer",
    "enableMargin",
    "enableFutures",
    "enableSpotAndMarginTrading",
    "enableVanillaOptions",
    "enablePortfolioMarginTrading",
    "enableFixApiTrade",
]


def client(env: str) -> Any:
    import ccxt  # noqa: PLC0415

    vals = dict(line.split("=", 1) for line in Path(env).read_text().splitlines() if "=" in line and not line.startswith("#"))
    key, secret = vals.get("BINANCE_API_KEY", "").strip(), vals.get("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        sys.exit(f"{env}: BINANCE_API_KEY / BINANCE_API_SECRET missing")
    return ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True})


def check(env: str) -> int:
    ex = client(env)
    try:
        r = ex.sapiGetAccountApiRestrictions()
    except Exception as e:  # noqa: BLE001
        print(f"Binance refused the key: {type(e).__name__}: {str(e)[:200]}")
        print("Check that the key was copied in full and that its IP list contains this server (169.58.169.184).")
        return 1
    bad = [k for k in DANGEROUS if str(r.get(k)).lower() == "true"]
    print(
        f"reading: {r.get('enableReading')}; IP restricted: {r.get('ipRestrict')}; trading/transfer/withdraw on: {', '.join(bad) or 'none'}"
    )
    if str(r.get("enableReading")).lower() != "true":
        print("The key cannot read. Enable «Enable Reading» only.")
        return 1
    if bad:
        print("This key can do more than read. Create a new one with only «Enable Reading»; this one is not kept.")
        return 1
    if str(r.get("ipRestrict")).lower() != "true":
        print("Warning: no IP restriction. It works, but restrict it to 169.58.169.184 in Binance.")
    return 0


STATE = Path("/var/lib/tradedesk-mcp/journal/binance")


def _state() -> dict[str, Any]:
    f = STATE / "downloads.json"
    return json.loads(f.read_text()) if f.exists() else {"requests": []}


def _save(st: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "downloads.json").write_text(json.dumps(st, indent=1))


def request(env: str, frm: str, to: str) -> int:
    if check(env):
        return 1
    ms = lambda d: int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)  # noqa: E731
    st = _state()
    if any(r["from"] == frm and r["to"] == to for r in st["requests"]):
        print(f"already requested {frm} – {to}; run fetch")
        return 0
    r = client(env).fapiPrivateGetTradeAsyn({"startTime": ms(frm), "endTime": ms(to)})
    st["requests"].append({"from": frm, "to": to, "downloadId": r.get("downloadId"), "asked": int(time.time())})
    _save(st)
    print(
        f"requested {frm} – {to}: downloadId {r.get('downloadId')}, Binance says ready in about {r.get('avgCostTimestampOfLast30d', '?')} ms"
    )
    return 0


def fetch(env: str) -> int:
    import httpx  # noqa: PLC0415

    ex, st = client(env), _state()
    for r in st["requests"]:
        if r.get("file"):
            print(f"{r['from']} – {r['to']}: have {r['file']}")
            continue
        info = ex.fapiPrivateGetTradeAsynId({"downloadId": r["downloadId"]})
        status, url = info.get("status"), info.get("url")
        if status != "completed" or not url:
            print(f"{r['from']} – {r['to']}: {status or 'pending'}")
            continue
        data = httpx.get(url, timeout=120, follow_redirects=True).content
        name = f"trades_{r['from']}_{r['to']}" + (".zip" if data[:2] == b"PK" else ".csv")
        (STATE / name).write_bytes(data)
        r["file"] = name
        print(f"{r['from']} – {r['to']}: saved {name} ({len(data)} bytes)")
    _save(st)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default="/etc/tradedesk-binance.env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    rq = sub.add_parser("request")
    rq.add_argument("--from", dest="from_", required=True)
    rq.add_argument("--to", required=True)
    sub.add_parser("fetch")
    a = ap.parse_args(argv)
    if a.cmd == "request":
        return request(a.env, a.from_, a.to)
    if a.cmd == "fetch":
        return fetch(a.env)
    return check(a.env)


if __name__ == "__main__":
    sys.exit(main())
