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


def read_files(folder: Path = STATE) -> list[dict[str, str]]:
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    rows: list[dict[str, str]] = []
    for f in sorted(folder.glob("trades_*")):
        texts = (
            [zipfile.ZipFile(f).read(n).decode("utf-8-sig") for n in zipfile.ZipFile(f).namelist()]
            if f.suffix == ".zip"
            else [f.read_text("utf-8-sig")]
        )
        for t in texts:
            rows += list(csv.DictReader(io.StringIO(t)))
    seen, uniq = set(), []
    for r in rows:
        k = (r.get("Symbol"), r.get("Trade Id"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def trips_from_fills(fills: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int]:
    """Group futures fills per symbol and position side into closed round trips; returns (trips, fills skipped).

    One-way mode ("BOTH"): the position is the running signed quantity; a trip runs from flat to flat, a flip closes one
    trip and opens the next. Hedge mode ("LONG"/"SHORT"): each side is its own position. P&L = Binance's realized profit
    minus fees in USDT/USDC (fees paid in other coins are left out and counted). Fills that close a position opened
    before the history starts (non-zero realized profit with no open trip) are skipped."""

    def ts(r: dict[str, str]) -> int:
        return int(datetime.strptime(r["Time(UTC)"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000)

    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for r in fills:
        groups.setdefault((r["Symbol"], r.get("Position Side", "BOTH")), []).append(r)
    trips: list[dict[str, Any]] = []
    skipped = 0
    for (sym, pside), fs in groups.items():
        fs.sort(key=lambda r: (ts(r), int(r.get("Trade Id") or 0)))
        pos, cur = 0.0, None
        for r in fs:
            q, px, rp = float(r["Quantity"]), float(r["Price"]), float(r["Realized Profit"] or 0)
            fee_s = str(r.get("Fee", "0")).split()
            fee = float(fee_s[0]) if len(fee_s) == 2 and fee_s[1] in ("USDT", "USDC") else 0.0
            if pside == "LONG":
                signed = q if r["Side"] == "BUY" else -q
            elif pside == "SHORT":
                signed = -q if r["Side"] == "SELL" else q
            else:
                signed = q if r["Side"] == "BUY" else -q
            if cur is None:
                if abs(rp) > 1e-12:
                    skipped += 1  # closes a position opened before the history starts
                    continue
                cur = {
                    "side": "long" if signed > 0 else "short",
                    "open_ts": ts(r),
                    "in_q": 0.0,
                    "in_v": 0.0,
                    "out_q": 0.0,
                    "out_v": 0.0,
                    "pnl": 0.0,
                    "fees": 0.0,
                    "oid": r.get("Order Id") or r.get("Trade Id"),
                }
                pos = 0.0
            new = pos + signed
            flip = pos != 0 and new != 0 and (pos > 0) != (new > 0)
            opening = (signed > 0) == (cur["side"] == "long") and not flip
            close_q = abs(pos) if flip else (0.0 if opening else q)
            if opening:
                cur["in_q"] += q
                cur["in_v"] += q * px
            if close_q:
                cur["out_q"] += close_q
                cur["out_v"] += close_q * px
            cur["pnl"] += rp
            cur["fees"] += fee
            pos = new
            if abs(pos) < 1e-9 or flip:
                if cur["in_q"] > 0 and cur["out_q"] > 0:
                    trips.append(
                        {
                            "venue": "binance",
                            "category": "usdm",
                            "oid": str(cur["oid"]),
                            "symbol": sym,
                            "side": cur["side"],
                            "size": cur["in_q"],
                            "entry": cur["in_v"] / cur["in_q"],
                            "exit": cur["out_v"] / cur["out_q"],
                            "pnl": cur["pnl"] - cur["fees"],
                            "fees": cur["fees"],
                            "leverage": None,
                            "open_ts": cur["open_ts"],
                            "ts": ts(r),
                        }
                    )
                cur = None
                if flip:
                    cur = {
                        "side": "long" if pos > 0 else "short",
                        "open_ts": ts(r),
                        "in_q": abs(pos),
                        "in_v": abs(pos) * px,
                        "out_q": 0.0,
                        "out_v": 0.0,
                        "pnl": 0.0,
                        "fees": 0.0,
                        "oid": str(r.get("Order Id")) + "f",
                    }
    return trips, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default="/etc/tradedesk-binance.env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    rq = sub.add_parser("request")
    rq.add_argument("--from", dest="from_", required=True)
    rq.add_argument("--to", required=True)
    sub.add_parser("fetch")
    sub.add_parser("import", help="build round trips from the downloaded files and add them to the journal")
    ap.add_argument("--db", default="/var/lib/tradedesk-mcp/tradedesk.sqlite")
    a = ap.parse_args(argv)
    if a.cmd == "import":
        import asyncio  # noqa: PLC0415

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        import bybit_import as bi  # noqa: PLC0415

        fills = read_files()
        trips, skipped = trips_from_fills(fills)
        added, dup = asyncio.run(bi.store(trips, a.db))
        print(
            f"fills {len(fills)}, closed round trips {len(trips)}, fills before the history start skipped {skipped}, added {added}, already in the journal {dup}"
        )
        return 0
    if a.cmd == "request":
        return request(a.env, a.from_, a.to)
    if a.cmd == "fetch":
        return fetch(a.env)
    return check(a.env)


if __name__ == "__main__":
    sys.exit(main())
