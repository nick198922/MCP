"""Import the owner's Hyperliquid perp trades into the trade journal. Read-only, no key: fills are public by address.

    uv run python scripts/hyperliquid_import.py import 0xADDRESS [--db PATH]
    uv run python scripts/hyperliquid_import.py stats [--db PATH] [--out REPORT.md]

Fills come from the public info endpoint (`userFillsByTime`, 2 000 per page; Hyperliquid keeps the 10 000 most recent
fills). They are grouped per coin into round trips: a trip starts when the position leaves zero and ends when it is
flat again (a flip closes one trip and opens the next). P&L = Hyperliquid's closedPnl minus fees; funding is not included.
Spot fills are skipped. The address is not stored in git; the report stays on the server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bybit_import as bi  # noqa: E402

URL = "https://api.hyperliquid.xyz/info"
DB_DEFAULT = bi.DB_DEFAULT


def fetch_fills(address: str, start_ms: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t, now = start_ms, int(time.time() * 1000)
    with httpx.Client(timeout=30) as c:
        while t < now:
            r = c.post(URL, json={"type": "userFillsByTime", "user": address, "startTime": t, "endTime": now, "aggregateByTime": True})
            r.raise_for_status()
            page = r.json() or []
            if not page:
                break
            out += page
            last = max(int(f["time"]) for f in page)
            if len(page) < 2000 or last + 1 <= t:
                break
            t = last + 1
            time.sleep(0.3)
    seen, uniq = set(), []
    for f in sorted(out, key=lambda f: (int(f["time"]), str(f.get("tid")))):
        k = (f.get("tid"), f.get("time"), f.get("oid"))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def round_trips(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group perp fills per coin into closed round trips (position from zero back to zero)."""
    trips: list[dict[str, Any]] = []
    state: dict[str, dict[str, Any]] = {}
    for f in fills:
        coin = str(f.get("coin", ""))
        if "/" in coin or coin.startswith("@") or str(f.get("dir", "")) in ("Buy", "Sell"):
            continue  # spot
        sz, px = float(f["sz"]), float(f["px"])
        signed = sz if f.get("side") == "B" else -sz
        start = float(f.get("startPosition") or 0.0)
        end = start + signed
        st = state.get(coin)
        if st is None and abs(start) > 1e-12:
            continue  # history starts mid-position: skip until flat
        if st is None:
            st = state[coin] = {
                "side": "long" if signed > 0 else "short",
                "open_ts": int(f["time"]),
                "in_sz": 0.0,
                "in_val": 0.0,
                "out_sz": 0.0,
                "out_val": 0.0,
                "pnl": 0.0,
                "fees": 0.0,
                "oid": str(f.get("oid") or f.get("tid")),
            }
        opening = (st["side"] == "long") == (signed > 0)
        flip = start != 0 and end != 0 and (start > 0) != (end > 0)
        close_sz = abs(start) if flip else (0.0 if opening else sz)
        open_sz = abs(end) if flip else (sz if opening else 0.0)
        st["pnl"] += float(f.get("closedPnl") or 0.0)
        st["fees"] += float(f.get("fee") or 0.0)
        if close_sz:
            st["out_sz"] += close_sz
            st["out_val"] += close_sz * px
        if open_sz and not flip:
            st["in_sz"] += open_sz
            st["in_val"] += open_sz * px
        if abs(end) < 1e-12 or flip:
            if st["in_sz"] > 0 and st["out_sz"] > 0:
                trips.append(
                    {
                        "venue": "hyperliquid",
                        "category": "perp",
                        "oid": st["oid"],
                        "symbol": coin,
                        "side": st["side"],
                        "size": st["in_sz"],
                        "entry": st["in_val"] / st["in_sz"],
                        "exit": st["out_val"] / st["out_sz"],
                        "pnl": st["pnl"] - st["fees"],
                        "fees": st["fees"],
                        "leverage": None,
                        "open_ts": st["open_ts"],
                        "ts": int(f["time"]),
                    }
                )
            state.pop(coin, None)
            if flip:
                state[coin] = {
                    "side": "long" if end > 0 else "short",
                    "open_ts": int(f["time"]),
                    "in_sz": abs(end),
                    "in_val": abs(end) * px,
                    "out_sz": 0.0,
                    "out_val": 0.0,
                    "pnl": 0.0,
                    "fees": 0.0,
                    "oid": str(f.get("oid") or f.get("tid")) + "f",
                }
    return trips


def mark_airdrop(db_path: str, before: str) -> int:
    """Owner 2026-10-18: older Hyperliquid positions were held to farm the airdrop, not traded; keep them, tag them."""
    import json  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    cut = int(datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    con = sqlite3.connect(db_path)
    n = 0
    for tid, tags in con.execute("SELECT id, tags FROM journal WHERE tags LIKE '%\"hyperliquid\"%' AND closed_ts < ?", (cut,)).fetchall():
        t = json.loads(tags or "[]")
        if "airdrop" not in t:
            con.execute(
                "UPDATE journal SET tags = ?, thesis = ? WHERE id = ?",
                (json.dumps([*t, "airdrop"]), "фарм дропу Hyperliquid (не торгівля)", tid),
            )
            n += 1
    con.commit()
    con.close()
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DB_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("address")
    md = sub.add_parser("mark-airdrop", help="tag Hyperliquid trades closed before a date as airdrop farming (left out of stats)")
    md.add_argument("--before", required=True, help="YYYY-MM-DD (UTC)")
    s = sub.add_parser("stats")
    s.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "import":
        fills = fetch_fills(a.address)
        trips = round_trips(fills)
        added, skipped = asyncio.run(bi.store(trips, a.db))
        span = (
            (
                time.strftime("%d.%m.%Y", time.gmtime(int(fills[0]["time"]) / 1000))
                + " – "
                + time.strftime("%d.%m.%Y", time.gmtime(int(fills[-1]["time"]) / 1000))
            )
            if fills
            else "-"
        )
        print(f"fills {len(fills)} ({span}), closed round trips {len(trips)}, added {added}, already in the journal {skipped}")
        return 0
    if a.cmd == "mark-airdrop":
        print(f"marked {mark_airdrop(a.db, a.before)} trades as airdrop farming")
        return 0
    rep = bi.stats(a.db, venue="hyperliquid")
    print(rep)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(rep, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
