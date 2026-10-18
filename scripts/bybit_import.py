"""Import the owner's closed Bybit positions (up to 2 years) into the trade journal. Read-only.

    uv run python scripts/bybit_import.py --env /etc/tradedesk-bybit.env check           # key must be read-only
    uv run python scripts/bybit_import.py --env /etc/tradedesk-bybit.env import [--days 730] [--db PATH]
    uv run python scripts/bybit_import.py stats [--db PATH] [--out REPORT.md]

The key pair is read from the env file (never from the command line, never printed). Only GET endpoints are called:
user/query-api (the key's own permissions), position/closed-pnl and its pre-upgrade twin (linear and inverse), and
position/get-closed-positions (options). `check` refuses any key that is not read-only. Each closed-P&L record becomes
one closed journal row (tags "bybit", "import", the category; note holds the Bybit order id, used to skip duplicates).
Bybit gives no opening time for a closed position, so `opened_ts` is the close time and the note says so. R is empty:
there is no planned stop. The report goes outside git: it is the owner's private trading record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DB_DEFAULT = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DAY_MS = 86_400_000
WINDOW_MS = 7 * DAY_MS  # Bybit: endTime - startTime <= 7 days


def read_env(path: str) -> tuple[str, str]:
    vals: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    key, secret = vals.get("BYBIT_API_KEY", ""), vals.get("BYBIT_API_SECRET", "")
    if not key or not secret:
        sys.exit(f"{path}: BYBIT_API_KEY / BYBIT_API_SECRET missing")
    return key, secret


def client(env: str) -> Any:
    import ccxt  # noqa: PLC0415

    key, secret = read_env(env)
    return ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})


def check(env: str) -> int:
    ex = client(env)
    try:
        r = ex.privateGetV5UserQueryApi()
    except Exception as e:  # noqa: BLE001 - report any refusal plainly
        print(f"Bybit refused the key: {type(e).__name__}: {str(e)[:200]}")
        print("Check that the key was copied in full and that its IP list contains this server (169.58.169.184).")
        return 1
    res = r.get("result") or {}
    ro, ips, perms = str(res.get("readOnly")), res.get("ips") or [], res.get("permissions") or {}
    granted = {k: v for k, v in perms.items() if v}
    print(f"key note: {res.get('note') or '-'}; read-only: {'yes' if ro == '1' else 'NO'}; IPs: {', '.join(ips) or 'none'}")
    print("permissions:", json.dumps(granted, ensure_ascii=False))
    if ro != "1":
        print("This key can trade. Create a new one with the permission «Read-Only»; this one is not kept.")
        return 1
    if not ips or ips == ["*"]:
        print("Warning: the key has no IP restriction. It works, but bind it to 169.58.169.184 in Bybit.")
    return 0


def _pages(call: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    out, cursor = [], None
    while True:
        p = dict(params, limit=100)
        if cursor:
            p["cursor"] = cursor
        r = call(p)
        res = r.get("result") or {}
        out += res.get("list") or []
        cursor = res.get("nextPageCursor")
        if not cursor or not res.get("list"):
            return out


def fetch(env: str, days: int) -> tuple[list[dict[str, Any]], list[str]]:
    ex = client(env)
    now = int(time.time() * 1000)
    # Bybit refuses anything older than its 2-year limit, counted a little stricter than 730 days: start a day later
    start = now - (min(days, 730) - 1) * DAY_MS
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for cat in ("linear", "inverse"):
        got, errors, t = 0, 0, start
        while t < now:
            try:
                part = _pages(ex.privateGetV5PositionClosedPnl, {"category": cat, "startTime": t, "endTime": min(t + WINDOW_MS, now)})
            except Exception as e:  # noqa: BLE001 - one failing window must not stop the rest
                errors += 1
                if errors <= 2:
                    notes.append(f"{cat} closed-pnl {datetime.fromtimestamp(t / 1000, UTC):%Y-%m-%d}: {type(e).__name__}: {str(e)[:120]}")
                part = []
            for x in part:
                x["_category"] = cat
            rows += part
            got += len(part)
            t += WINDOW_MS
        notes.append(f"{cat} closed-pnl: {got} records, {errors} failed windows")
    # The pre-upgrade (classic account) history needs a symbol per request; it is fetched only when asked for
    try:
        opts = _pages(ex.privateGetV5PositionGetClosedPositions, {"category": "option"})
        for x in opts:
            x["_category"] = "option"
        notes.append(f"options closed positions (Bybit keeps the last 6 months): {len(opts)} records")
        rows += [dict(x, _option=True) for x in opts]
    except Exception as e:  # noqa: BLE001
        notes.append(f"options: {type(e).__name__}: {str(e)[:120]}")
    return rows, notes


def to_trade(x: dict[str, Any]) -> dict[str, Any] | None:
    """One closed-P&L record as a journal row; the side is taken from the sign that matches the gross P&L."""
    try:
        if x.get("_option"):
            qty, entry, exit_ = (
                float(x.get("closedSize") or x.get("size") or 0),
                float(x.get("avgEntryPrice") or 0),
                float(x.get("avgExitPrice") or 0),
            )
            pnl = float(x.get("closedPnl") or 0)
            fees = float(x.get("openFee") or 0) + float(x.get("closeFee") or 0)
            side = "long" if str(x.get("side", "")).lower() == "buy" else "short"
        else:
            qty = float(x.get("closedSize") or x.get("qty") or 0)
            entry, exit_ = float(x.get("avgEntryPrice") or 0), float(x.get("avgExitPrice") or 0)
            pnl = float(x.get("closedPnl") or 0)
            fees = float(x.get("openFee") or 0) + float(x.get("closeFee") or 0)
            gross_long = (exit_ - entry) * qty
            side = "long" if abs((pnl + fees) - gross_long) <= abs((pnl + fees) + gross_long) else "short"
        ts = int(x.get("updatedTime") or x.get("createdTime") or 0)
        oid = x.get("orderId") or x.get("positionId") or f"{x.get('symbol')}-{ts}"
    except (TypeError, ValueError):
        return None
    if qty <= 0 or entry <= 0 or ts <= 0:
        return None
    return {
        "ts": ts,
        "symbol": x.get("symbol", "?"),
        "side": side,
        "size": qty,
        "entry": entry,
        "exit": exit_,
        "pnl": pnl,
        "fees": fees,
        "leverage": x.get("leverage"),
        "category": x["_category"],
        "oid": str(oid),
    }


async def store(trades: list[dict[str, Any]], db_path: str) -> tuple[int, int]:
    from tradedesk.analytics.journal import outcome_of  # noqa: PLC0415
    from tradedesk.store.db import Store  # noqa: PLC0415

    st = Store(db_path)
    await st.init()
    added = skipped = 0
    try:
        for t in sorted(trades, key=lambda z: z["ts"]):
            mark = f"bybit:{t['category']}:{t['oid']}"
            cur = await st.conn.execute("SELECT 1 FROM journal WHERE note LIKE ? LIMIT 1", (f"%{mark}%",))
            if await cur.fetchone():
                skipped += 1
                continue
            tid = await st.journal_insert(
                opened_ts=t["ts"] // 1000,
                symbol=t["symbol"],
                side=t["side"],
                size=t["size"],
                entry=t["entry"],
                tags=["bybit", "import", t["category"]],
                thesis="імпорт з Bybit: теза не записана",
            )
            note = (
                f"{mark}; комісії {t['fees']:.4f}; плече {t['leverage'] or '-'}; час відкриття Bybit не віддає — opened_ts = час закриття"
            )
            await st.journal_update_close(
                tid,
                closed_ts=t["ts"] // 1000,
                status="closed",
                exit_=t["exit"],
                outcome=outcome_of(t["pnl"]),
                pnl=t["pnl"],
                r_multiple=None,
                note=note,
            )
            added += 1
    finally:
        await st.aclose()
    return added, skipped


def stats(db_path: str) -> str:
    import sqlite3  # noqa: PLC0415

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT closed_ts, symbol, side, pnl, note FROM journal WHERE tags LIKE '%\"bybit\"%' AND status='closed'"
    ).fetchall()
    con.close()
    if not rows:
        return "У журналі немає угод з Bybit."
    n = len(rows)
    pnl = [r[3] or 0.0 for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    fees = 0.0
    for r in rows:
        try:
            fees += float(str(r[4]).split("комісії ")[1].split(";")[0])
        except (IndexError, ValueError):
            pass
    by_month: dict[str, list[float]] = defaultdict(list)
    by_sym: dict[str, list[float]] = defaultdict(list)
    by_side: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_month[datetime.fromtimestamp(r[0], UTC).strftime("%Y-%m")].append(r[3] or 0.0)
        by_sym[r[1]].append(r[3] or 0.0)
        by_side[r[2]].append(r[3] or 0.0)
    first = datetime.fromtimestamp(min(r[0] for r in rows), UTC).strftime("%d.%m.%Y")
    last = datetime.fromtimestamp(max(r[0] for r in rows), UTC).strftime("%d.%m.%Y")
    pf = sum(wins) / -sum(losses) if losses else float("inf")
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(rows):
        eq += r[3] or 0.0
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out = [
        f"# Угоди Bybit {first} – {last}",
        "",
        f"- Закритих позицій: **{n}**; у плюсі {len(wins)} ({len(wins) / n * 100:.0f} %).",
        f"- Чистий результат: **{sum(pnl):+,.2f} USD** (комісії ≈ {fees:,.2f} USD уже враховані).",
        f"- Середній виграш {sum(wins) / max(1, len(wins)):+,.2f}, середній програш {sum(losses) / max(1, len(losses)):+,.2f}; profit factor {pf:.2f}.",
        f"- Найбільша просадка за кривою закритих угод: {dd:,.2f} USD.",
        "",
        "## За напрямом",
        "",
        *[
            f"- {s}: {len(v)} угод, {sum(v):+,.2f} USD, у плюсі {sum(p > 0 for p in v) / len(v) * 100:.0f} %"
            for s, v in sorted(by_side.items())
        ],
        "",
        "## Монети (за чистим результатом)",
        "",
        "| Монета | Угод | Результат, USD | У плюсі |",
        "|---|---|---|---|",
        *[
            f"| {s} | {len(v)} | {sum(v):+,.2f} | {sum(p > 0 for p in v) / len(v) * 100:.0f} % |"
            for s, v in sorted(by_sym.items(), key=lambda kv: sum(kv[1]))
        ],
        "",
        "## Місяці",
        "",
        "| Місяць | Угод | Результат, USD |",
        "|---|---|---|",
        *[f"| {m} | {len(v)} | {sum(v):+,.2f} |" for m, v in sorted(by_month.items())],
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default="/etc/tradedesk-bybit.env")
    ap.add_argument("--db", default=DB_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    imp = sub.add_parser("import")
    imp.add_argument("--days", type=int, default=730)
    s = sub.add_parser("stats")
    s.add_argument("--out", default=None, help="also write the report here (keep it outside git)")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return check(a.env)
    if a.cmd == "import":
        if check(a.env):
            return 1
        raw, notes = fetch(a.env, a.days)
        trades = [t for t in (to_trade(x) for x in raw) if t]
        added, skipped = asyncio.run(store(trades, a.db))
        print("\n".join(notes))
        print(f"records {len(raw)}, usable {len(trades)}, added {added}, already in the journal {skipped}")
        return 0
    rep = stats(a.db)
    print(rep)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(rep, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
