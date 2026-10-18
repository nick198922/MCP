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
VENUES = {"bybit": "Bybit", "hyperliquid": "Hyperliquid", "binance": "Binance"}
MAJORS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "WIFUSDT",
    "1000PEPEUSDT",
    "TRUMPUSDT",
    "FARTCOINUSDT",
    "HYPEUSDT",
    "ENAUSDT",
    "TAOUSDT",
    "TIAUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "INJUSDT",
    "SEIUSDT",
    "ONDOUSDT",
    "JUPUSDT",
    "WLDUSDT",
    "TONUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "BTCUSD",
    "ETHUSD",
]


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


def fetch_classic(env: str, symbols: list[str], start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Pre-upgrade (classic account) closed P&L: Bybit needs one symbol per request, 7-day windows."""
    ex = client(env)
    now = int(time.time() * 1000)
    start_ms = max(start_ms, now - 729 * DAY_MS)
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for cat in ("linear", "inverse"):
        found: dict[str, int] = {}
        errors = 0
        for sym in symbols:
            if cat == "inverse" and not sym.endswith("USD"):
                continue
            t = start_ms
            while t < end_ms:
                try:
                    part = _pages(
                        ex.privateGetV5PreUpgradePositionClosedPnl,
                        {"category": cat, "symbol": sym, "startTime": t, "endTime": min(t + WINDOW_MS, end_ms)},
                    )
                except Exception as e:  # noqa: BLE001 - unknown symbols and old windows are expected; keep going
                    errors += 1
                    if errors <= 2:
                        notes.append(f"classic {cat} {sym}: {type(e).__name__}: {str(e)[:100]}")
                    break
                for x in part:
                    x["_category"] = cat + "-classic"
                if part:
                    found[sym] = found.get(sym, 0) + len(part)
                rows += part
                t += WINDOW_MS
        notes.append(
            f"classic {cat}: {sum(found.values())} records in {len(found)} symbols ({', '.join(sorted(found)) or '-'}); {errors} symbols failed"
        )
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
            venue = t.get("venue", "bybit")
            mark = f"{venue}:{t['category']}:{t['oid']}"
            cur = await st.conn.execute("SELECT 1 FROM journal WHERE note LIKE ? LIMIT 1", (f"%{mark}%",))
            if await cur.fetchone():
                skipped += 1
                continue
            tid = await st.journal_insert(
                opened_ts=(t.get("open_ts") or t["ts"]) // 1000,
                symbol=t["symbol"],
                side=t["side"],
                size=t["size"],
                entry=t["entry"],
                tags=[venue, "import", t["category"]],
                thesis=f"імпорт з {VENUES.get(venue, venue)}: теза не записана",
            )
            opened = "час відкриття — з першого fill" if t.get("open_ts") else "час відкриття біржа не віддає — opened_ts = час закриття"
            note = f"{mark}; комісії {t['fees']:.4f}; плече {t['leverage'] or '-'}; {opened}"
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


def duration_lines(rows: list[tuple]) -> list[str]:
    """Holding-time section when the venue gives opening times (rows: closed_ts, symbol, side, pnl, note, opened_ts)."""
    dur = [(r[0] - r[5], r[3] or 0.0) for r in rows if r[5] and r[0] and r[0] > r[5]]
    if len(dur) < max(5, len(rows) // 2):
        return []
    buckets = [
        ("до 1 год", 0, 3600),
        ("1–6 год", 3600, 6 * 3600),
        ("6–24 год", 6 * 3600, 86400),
        ("1–7 дн", 86400, 7 * 86400),
        ("понад 7 дн", 7 * 86400, 1e12),
    ]
    med = sorted(d for d, _ in dur)[len(dur) // 2]
    out = [
        "## Тривалість угод",
        "",
        f"Медіана {med / 3600:.1f} год.",
        "",
        "| Тривалість | Угод | Результат, USD | У плюсі |",
        "|---|---|---|---|",
    ]
    for lab, lo, hi in buckets:
        v = [p for d, p in dur if lo <= d < hi]
        if v:
            out.append(f"| {lab} | {len(v)} | {sum(v):+,.2f} | {sum(p > 0 for p in v) / len(v) * 100:.0f} % |")
    return [*out, ""]


def _levkey(k: str) -> float:
    try:
        return float(k.rstrip("x"))
    except ValueError:
        return 1e9


def stats(db_path: str, venue: str | None = "bybit") -> str:
    """Report over imported closed trades of one venue (None = all venues); rows tagged "airdrop" are left out."""
    import sqlite3  # noqa: PLC0415

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT closed_ts, symbol, side, pnl, note, opened_ts FROM journal WHERE tags LIKE ? AND tags NOT LIKE '%\"airdrop\"%' AND status='closed'",
        (f'%"{venue}"%' if venue else '%"import"%',),
    ).fetchall()
    con.close()
    if not rows:
        return f"У журналі немає імпортованих угод{(' з ' + VENUES.get(venue, venue)) if venue else ''}."
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
    by_lev: dict[str, list[float]] = defaultdict(list)
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        try:
            lev = str(r[4]).split("плече ")[1].split(";")[0].strip()
        except IndexError:
            lev = "-"
        by_lev[lev + "x" if lev not in ("-", "") else "?"].append(r[3] or 0.0)
        by_day[datetime.fromtimestamp(r[0], UTC).strftime("%Y-%m-%d")].append(r[3] or 0.0)
    worst = sorted(((datetime.fromtimestamp(r[0], UTC).strftime("%d.%m.%Y"), r[1], r[3] or 0.0) for r in rows), key=lambda z: z[2])[:10]
    streak = cur = 0
    for r in sorted(rows):
        cur = cur + 1 if (r[3] or 0) < 0 else 0
        streak = max(streak, cur)
    first = datetime.fromtimestamp(min(r[0] for r in rows), UTC).strftime("%d.%m.%Y")
    last = datetime.fromtimestamp(max(r[0] for r in rows), UTC).strftime("%d.%m.%Y")
    pf = sum(wins) / -sum(losses) if losses else float("inf")
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(rows):
        eq += r[3] or 0.0
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out = [
        f"# Угоди {VENUES.get(venue, venue) if venue else 'усіх бірж'} {first} – {last}",
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
        "",
        "## Плече",
        "",
        "| Плече | Угод | Результат, USD | У плюсі |",
        "|---|---|---|---|",
        *[
            f"| {k} | {len(v)} | {sum(v):+,.2f} | {sum(p > 0 for p in v) / len(v) * 100:.0f} % |"
            for k, v in sorted(by_lev.items(), key=lambda kv: _levkey(kv[0]))
        ],
        "",
        "## Найбільші збитки",
        "",
        f"- 10 найбільших збитків: {sum(p for _, _, p in worst):+,.2f} USD — {sum(p for _, _, p in worst) / sum(losses) * 100:.0f} % усіх збитків.",
        *[f"- {d} {sym}: {p:+,.2f}" for d, sym, p in worst],
        "",
        *duration_lines(rows),
        "## Поведінка",
        "",
        f"- Найдовша серія збиткових угод поспіль: {streak}.",
        f"- Днів з угодами: {len(by_day)}; у середньому {n / max(1, len(by_day)):.1f} угоди на такий день; найбільше за день — {max(len(v) for v in by_day.values())}.",
        f"- Дні з 5+ угодами: {sum(len(v) >= 5 for v in by_day.values())}, їхній результат {sum(sum(v) for v in by_day.values() if len(v) >= 5):+,.2f} USD.",
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
    cl = sub.add_parser("import-classic", help="pre-upgrade (classic account) closed P&L, per symbol")
    cl.add_argument("--symbols", required=True, help="comma-separated, or 'journal' = symbols already in the journal plus majors")
    cl.add_argument("--from", dest="from_", default="2024-10-19")
    cl.add_argument("--to", default="2025-05-01")
    s = sub.add_parser("stats")
    s.add_argument("--venue", default="bybit", help="bybit, hyperliquid, binance or all")
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
    if a.cmd == "import-classic":
        if check(a.env):
            return 1
        syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
        if a.symbols == "journal":
            import sqlite3  # noqa: PLC0415

            con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
            syms = sorted({r[0] for r in con.execute("SELECT DISTINCT symbol FROM journal WHERE tags LIKE '%\"bybit\"%'")} | set(MAJORS))
            con.close()
        ms = lambda d: int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)  # noqa: E731
        raw, notes = fetch_classic(a.env, syms, ms(a.from_), ms(a.to))
        trades = [t for t in (to_trade(x) for x in raw) if t]
        added, skipped = asyncio.run(store(trades, a.db))
        print("\n".join(notes))
        print(f"symbols {len(syms)}, records {len(raw)}, usable {len(trades)}, added {added}, already in the journal {skipped}")
        return 0
    rep = stats(a.db, None if a.venue == "all" else a.venue)
    print(rep)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(rep, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
