"""Temporary (item 383a): inventory of carry A / B paper data for the mid re-pricing. Prints counts and keys only, no P&L."""

import json
import sqlite3
from collections import Counter

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
print("cp_position cols:", [r[1] for r in con.execute("PRAGMA table_info(cp_position)")])
print("strategies:", [tuple(r) for r in con.execute("SELECT strategy, status, count(*) FROM cp_position GROUP BY 1, 2")])
for s in ("A", "B"):
    pos = [dict(r) for r in con.execute("SELECT * FROM cp_position WHERE strategy=? ORDER BY opened_ts", (s,))]
    side = [
        json.loads(r["payload"] or "{}") | {"_cyc": r["cycle_ts"]}
        for r in con.execute("SELECT cycle_ts, payload FROM cp_log WHERE strategy=? AND kind='side' ORDER BY id", (s,))
    ]
    by: dict[tuple[str, str], list[dict]] = {}
    for x in side:
        by.setdefault((str(x.get("trade_id")), str(x.get("event"))), []).append(x)
    print(f"\n== {s}: positions {len(pos)}, side rows {len(side)}, events {Counter(e for _, e in by)}")
    print("  legs per (trade,event):", Counter(len(v) for v in by.values()))
    print("  side payload keys:", sorted(side[0].keys()) if side else None)
    t0 = next((p for p in pos if p["status"] == "closed"), None)
    if t0:
        tr = json.loads(t0["trade"]) if isinstance(t0.get("trade"), str) else t0.get("trade")
        print("  trade keys:", sorted(tr.keys()) if isinstance(tr, dict) else type(tr))
        st = json.loads(t0["state"]) if isinstance(t0.get("state"), str) else t0.get("state")
        print("  state keys:", sorted(st.keys()) if isinstance(st, dict) else type(st))
    full = Counter()
    for p in pos:
        tid = str(p["trade_id"])
        o, c = by.get((tid, "open"), []), by.get((tid, "close"), [])
        ok_o = len(o) == 2 and all(x.get("book_available") and x.get("mid") for x in o)
        ok_c = len(c) == 2 and all(x.get("book_available") and x.get("mid") for x in c)
        full[(p["status"], "open_book" if ok_o else "open_nobook", "close_book" if ok_c else ("close_nobook" if c else "no_close"))] += 1
    for k, v in sorted(full.items()):
        print("  ", k, v)
    lat = sorted(x["book_latency_ms"] / 1000 for x in side if x.get("book_latency_ms") is not None)
    if lat:
        print(f"  book latency s: min {lat[0]:.0f}, median {lat[len(lat) // 2]:.0f}, max {lat[-1]:.0f}, n {len(lat)}")
    mults = Counter(x.get("contract_multiplier") for x in side)
    print("  multipliers:", dict(mults))
    print("  date range opened:", min((p["opened_ts"] for p in pos), default=None), max((p["opened_ts"] for p in pos), default=None))
con.close()
