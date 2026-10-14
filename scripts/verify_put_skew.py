"""Put-skew premium on Deribit BTC (IDEA-20261013-deribit-put-skew-premium).

Pre-registrations live in the idea card ("Попередня реєстрація" = v1, "Попередня реєстрація v2" = v2);
``--scheme`` selects which one this script implements (default v2).
  * v1: prints of the 08:00-10:00 UTC window, cache ``<cache>/trades/*.json``; failed its coverage gate.
  * v2: prints of the 24 h before 09:00 UTC, cache ``<cache>/trades24/*.json.gz``; calendar expiry rule.
Data (all via the runtime providers):
  * BTC option trades from history.deribit.com (``rt.deribit_history.last_trades_by_currency_and_time``),
    one cached file per day;
  * delivery prices (``rt.deribit.delivery_prices``), BTC-PERPETUAL hourly funding
    (``rt.deribit.funding_rate_history``), DVOL (``rt.deribit.volatility_index_data``).

Usage:
    uv run python scripts/verify_put_skew.py --cache-dir ~/.cache/tradedesk-agents/put_skew --scheme v2 --fetch-only
    uv run python scripts/verify_put_skew.py --cache-dir ~/.cache/tradedesk-agents/put_skew --scheme v2 --coverage-only
    uv run python scripts/verify_put_skew.py --cache-dir ~/.cache/tradedesk-agents/put_skew --scheme v2 --out report.md
    # exploratory coverage rows on the 40-day full-day sample (no P&L):
    uv run python scripts/verify_put_skew.py --cache-dir ~/.cache/tradedesk-agents/put_skew \
        --sample-coverage ~/.cache/tradedesk-agents/put_skew_fullday_sample
The gate: if coverage of the primary configuration is below 80 %, no P&L is computed.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import gzip
import json
import logging
import os
import random
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics.carry import pearson
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import (
    V1_BUILD,
    block_bootstrap_ci,
    build_day,
    chain,
    dvol_known_at_entry,
    plan_entry,
    run_book,
    tercile_spread,
    trailing_percentile,
)
from tradedesk.analytics.vrp import stats
from tradedesk.server import build_server

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
FETCH_START = datetime(2021, 1, 1, tzinfo=UTC)
FETCH_END = datetime(2026, 10, 13, tzinfo=UTC)
# window = (start, end) hours relative to the entry day's 00:00 UTC; tenors = target DTE, DTE band, expiry rule
SCHEMES: dict[str, dict[str, Any]] = {
    "v1": {
        "dir": "trades",
        "gz": False,
        "window": (8, 10),
        "build": V1_BUILD,
        "snap": "snapshots_v1.json",
        "tenors": {"30d": (30, (21, 45), "nearest"), "10d": (10, (7, 14), "nearest")},
    },
    "v2": {
        "dir": "trades24",
        "gz": True,
        "window": (-15, 9),
        "build": {},
        "snap": "snapshots_v2.json",
        "tenors": {"30d": (30, (21, 56), "calendar_monthly"), "10d": (10, (7, 14), "nearest")},
    },
    # v2 pre-registered sensitivity (exploratory): marks and forward only from prints <= 3 h old
    "v2_age3": {
        "dir": "trades24",
        "gz": True,
        "window": (-15, 9),
        "build": {"max_mark_age_h": 3.0},
        "snap": "snapshots_v2_age3.json",
        "tenors": {"30d": (30, (21, 56), "calendar_monthly"), "10d": (10, (7, 14), "nearest")},
    },
    # v2 exploratory expiry rule: most prints in the window (not 25-delta put prints)
    "v2_liquid": {
        "dir": "trades24",
        "gz": True,
        "window": (-15, 9),
        "build": {},
        "snap": "snapshots_v2.json",
        "tenors": {"30d": (30, (21, 56), "most_liquid_total"), "10d": (10, (7, 14), "nearest")},
    },
}
KEEP = ("trade_id", "instrument_name", "timestamp", "iv", "price", "mark_price", "index_price", "direction", "amount")


def day_iter(a: datetime, b: datetime):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


async def fetch_window(rt: Any, start: int, end: int) -> tuple[list[dict[str, Any]], int, int]:
    """All trades in [start, end] with timestamp pagination; returns (trades, pages, skipped_ms_collisions)."""
    cur, pages, collisions = start, 0, 0
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    while True:
        d = (await rt.deribit_history.last_trades_by_currency_and_time("BTC", "option", cur, end, count=1000)).data
        pages += 1
        for t in d["trades"]:
            if t.get("trade_id") not in seen:
                seen.add(t.get("trade_id"))
                out.append({k: t.get(k) for k in KEEP})
        if not d["has_more"] or not d["trades"]:
            break
        nxt = int(d["trades"][-1]["timestamp"])
        if nxt <= cur:  # a full page within one millisecond: step over it (rare; counted)
            nxt = cur + 1
            collisions += 1
        cur = nxt
    return out, pages, collisions


def day_path(tdir: Path, d: datetime, gz: bool) -> Path:
    return tdir / f"{d:%Y-%m-%d}.json{'.gz' if gz else ''}"


def write_day(p: Path, blob: dict[str, Any]) -> None:
    raw = json.dumps(blob, separators=(",", ":")).encode()
    tmp = p.with_name(p.name + ".part")  # atomic: an interrupted run never leaves a truncated day file
    tmp.write_bytes(gzip.compress(raw, 6) if p.suffix == ".gz" else raw)
    os.replace(tmp, p)


def read_day(p: Path) -> dict[str, Any]:
    raw = p.read_bytes()
    blob = json.loads(gzip.decompress(raw) if p.suffix == ".gz" else raw)
    return blob if isinstance(blob, dict) else {"trades": blob}  # sample files are bare lists


async def fetch_all(cache: Path, scheme: str) -> None:
    sch = SCHEMES[scheme]
    _, rt = build_server()
    tdir = cache / sch["dir"]
    tdir.mkdir(parents=True, exist_ok=True)
    days = list(day_iter(FETCH_START, FETCH_END))
    todo = [d for d in days if not day_path(tdir, d, sch["gz"]).exists()]
    print(f"trades: {len(days)} days, {len(todo)} to fetch", flush=True)
    t0 = time.perf_counter()
    try:
        for i, d in enumerate(todo):
            s = int(d.timestamp() * 1000) + sch["window"][0] * HOUR_MS
            e = int(d.timestamp() * 1000) + sch["window"][1] * HOUR_MS
            tr, pages, coll = await fetch_window(rt, s, e)
            p = day_path(tdir, d, sch["gz"])
            await asyncio.to_thread(write_day, p, {"trades": tr, "pages": pages, "collisions": coll})
            if i % 50 == 0 or i == len(todo) - 1:
                el = time.perf_counter() - t0
                eta = el / (i + 1) * (len(todo) - i - 1)
                print(f"  {d:%Y-%m-%d}: {len(tr)} trades, {pages} pages; {i + 1}/{len(todo)} in {el:.0f}s, eta {eta:.0f}s", flush=True)
        p = cache / "delivery_btc_usd.json"
        if not p.exists():
            rows: list[dict[str, Any]] = []
            off = 0
            while True:
                f = await rt.deribit.delivery_prices("btc_usd", offset=off, count=1000)
                batch = f.data.get("data") or []
                rows += batch
                off += len(batch)
                if not batch or off >= int(f.data.get("records_total") or 0):
                    break
            p.write_text(json.dumps(rows))
            print(f"delivery prices: {len(rows)}", flush=True)
        p = cache / "funding_btc_perp.json"
        if not p.exists():
            recs: dict[int, float] = {}
            a = int(FETCH_START.timestamp() * 1000)
            end = int((FETCH_END + timedelta(days=1)).timestamp() * 1000)
            while a < end:
                b = min(a + 30 * DAY_MS, end)
                f = await rt.deribit.funding_rate_history("BTC-PERPETUAL", a, b)
                for r in f.data or []:
                    recs[int(r["timestamp"])] = float(r.get("interest_1h") or 0.0)
                a = b
            p.write_text(json.dumps(sorted(recs.items())))
            print(f"funding records: {len(recs)}", flush=True)
        p = cache / "dvol_BTC.json"
        if not p.exists():
            rows2: dict[int, float] = {}
            start = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)
            end = int((FETCH_END + timedelta(days=1)).timestamp() * 1000)
            for _ in range(30):
                f = await rt.deribit.volatility_index_data("BTC", start, end, 86400)
                data = f.data or {}
                for x in data.get("data") or []:
                    rows2[int(x[0])] = float(x[4]) / 100.0
                cont = data.get("continuation")
                if not cont or int(cont) <= start or int(cont) >= end:
                    break
                end = int(cont)
            p.write_text(json.dumps(sorted(rows2.items())))
            print(f"dvol days: {len(rows2)}", flush=True)
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- surfaces

ENTRY_START_MS = int(datetime(2021, 4, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = int(FETCH_END.timestamp() * 1000) + 8 * HOUR_MS
SPLIT_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def day_ms_of(path: Path) -> int:
    return int(datetime.strptime(path.name[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def _build_file(path: str, build: dict[str, Any]) -> tuple[int, dict[str, Any] | None, int, int, int]:
    raw = read_day(Path(path))
    day = day_ms_of(Path(path))
    snap = build_day(raw["trades"], day, **build)
    return day, _ser(snap), int(raw.get("pages", 0)), int(raw.get("collisions", 0)), len(raw["trades"])


def _ser(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    return dict(snap, expiries=[[e, dict(x, strikes=[[k, v] for k, v in x["strikes"].items()])] for e, x in snap["expiries"].items()])


def _de(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    return dict(snap, expiries={int(e): dict(x, strikes={float(k): v for k, v in x["strikes"]}) for e, x in snap["expiries"]})


def load_snapshots(cache: Path, scheme: str) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    sch = SCHEMES[scheme]
    files = sorted(str(p) for p in (cache / sch["dir"]).glob("*.json*") if not p.name.endswith(".part"))
    sp = cache / sch["snap"]
    if sp.exists():
        blob = json.loads(sp.read_text())
        if blob.get("n_files") == len(files):
            return {int(d): _de(s) for d, s in blob["days"].items() if s}, blob["meta"]
    t0 = time.perf_counter()
    out: dict[str, Any] = {}
    meta = {"pages": 0, "collisions": 0, "trades": 0, "empty_days": []}
    workers = max(1, (os.cpu_count() or 2) - 2)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        build = functools.partial(_build_file, build=sch["build"])
        for i, (day, snap, pages, coll, n) in enumerate(ex.map(build, files, chunksize=8)):
            out[str(day)] = snap
            meta["pages"] += pages
            meta["collisions"] += coll
            meta["trades"] += n
            if snap is None:
                meta["empty_days"].append(day)
            if i % 250 == 0:
                print(f"  surfaces {i}/{len(files)} in {time.perf_counter() - t0:.0f}s", flush=True)
    sp.write_text(json.dumps({"n_files": len(files), "days": out, "meta": meta}))
    return {int(d): _de(s) for d, s in out.items() if s}, meta


# ----------------------------------------------------------------------------- futures cross-check

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def future_name(expiry_ms: int) -> str:
    d = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    return f"BTC-{d.day}{MONTHS[d.month - 1]}{d:%y}"


async def futures_check(cache: Path, plans: dict[int, dict[str, Any]], days: dict[int, dict[str, Any]]) -> dict[str, list[list[float]]]:
    """(price, index) of the dated future's prints in the hour before 09:00 on days whose chosen expiry is
    quarterly (pre-registered consistency check of the solved F/S). Cached in one JSON file, resumable."""
    from tradedesk.core.errors import ProviderError

    p = cache / "futures_fs_v2.json"
    got: dict[str, list[list[float]]] = json.loads(p.read_text()) if p.exists() else {}
    todo = [
        (d, pl)
        for d, pl in sorted(plans.items())
        if pl["covered"] and datetime.fromtimestamp(pl["expiry"] / 1000, UTC).month in (3, 6, 9, 12) and str(d) not in got
    ]
    if not todo:
        return got
    print(f"futures check: {len(todo)} days to fetch", flush=True)
    _, rt = build_server()
    try:
        for i, (d, pl) in enumerate(todo):
            t_ref = days[d]["t_ms"]
            try:
                f = await rt.deribit_history.last_trades_by_instrument_and_time(
                    future_name(pl["expiry"]), t_ref - HOUR_MS, t_ref, count=100, sorting="desc"
                )
                got[str(d)] = [
                    [float(t["price"]), float(t["index_price"])] for t in f.data["trades"] if t.get("price") and t.get("index_price")
                ]
            except ProviderError as e:
                print(f"  {ymd(d)} {future_name(pl['expiry'])}: {e}", flush=True)
                got[str(d)] = []
            if i % 100 == 0:
                print(f"  futures {i + 1}/{len(todo)}", flush=True)
                p.write_text(json.dumps(got))
    finally:
        await rt.aclose()
        p.write_text(json.dumps(got))
    return got


# ----------------------------------------------------------------------------- books

COSTS: dict[str, dict[str, Any]] = {
    "base": {"hs_w": 0.015, "hs_a": 0.01, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005, "side": False},
    "zero": {"hs_w": 0.0, "hs_a": 0.0, "option_fee": 0.0, "delivery_fee": 0.0, "hedge_fee": 0.0, "side": False},
    "stress": {"hs_w": 0.03, "hs_a": 0.015, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.001, "side": False},
    "trade_side": {"hs_w": 0.0, "hs_a": 0.0, "option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005, "side": True},
}
DELTAS = (0.25, 0.20, 0.30)


def make_books(snap: dict[str, Any], plan: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
    x = snap["expiries"][plan["expiry"]]
    st = x["strikes"]
    kp, ka = plan["kp"], plan["ka"]
    mp, ma = st[kp]["mark_iv"], st[ka]["mark_iv"]
    if cost["side"]:
        pe, se, be = st[kp]["sell_iv"], st[ka]["sell_iv"], st[ka]["buy_iv"]
    else:
        pe, se, be = mp - cost["hs_w"], ma - cost["hs_a"], ma + cost["hs_a"]
    F, T = x["F"], x["T"]
    v_p = black76_greeks(F, kp, T, mp, False)["vega"]
    v_s = black76_greeks(F, ka, T, ma, True)["vega"] + black76_greeks(F, ka, T, ma, False)["vega"]
    n = v_p / v_s if v_s > 0 else 0.0
    books: dict[str, Any] = {"n_atm": n}
    books["put"] = [(kp, False, -1.0, pe)] if pe else None
    books["straddle"] = [(ka, True, -1.0, se), (ka, False, -1.0, se)] if se else None
    books["P"] = [(kp, False, -1.0, pe), (ka, True, n, be), (ka, False, n, be)] if pe and be else None
    return books


def make_plans(days: dict[int, dict[str, Any]], tenors: dict[str, Any]) -> dict[tuple[str, float], dict[int, dict[str, Any]]]:
    plans: dict[tuple[str, float], dict[int, dict[str, Any]]] = {}
    for tenor, (tgt, band, rule) in tenors.items():
        for dl in DELTAS:
            pl: dict[int, dict[str, Any]] = {}
            for d, snap in days.items():
                if d < ENTRY_START_MS:
                    continue
                p = plan_entry(snap, target_days=tgt, dte_band=band, target_delta=dl, delta_band=(dl - 0.05, dl + 0.05), expiry_rule=rule)
                if p is not None:
                    pl[d] = p
            plans[(tenor, dl)] = pl
    return plans


def run_all(
    days: dict[int, dict[str, Any]],
    plans: dict[tuple[str, float], dict[int, dict[str, Any]]],
    delivery: dict[str, float],
    funding: list[tuple[int, float]],
):
    results: dict[tuple[str, float, str, bool], dict[str, dict[int, dict[str, Any]]]] = {}
    for tenor, dl in plans:
        if True:
            pl = plans[(tenor, dl)]
            variants = [(c, True) for c in COSTS] + [("base", False)]
            for ck, fund in variants:
                res: dict[str, dict[int, dict[str, Any]]] = {"put": {}, "straddle": {}, "P": {}}
                cost = COSTS[ck]
                for d, p in pl.items():
                    if not p["covered"] or p["expiry"] > LAST_EXPIRY_MS:
                        continue
                    dp = delivery.get(datetime.fromtimestamp(p["expiry"] / 1000, UTC).strftime("%Y-%m-%d"))
                    if dp is None:
                        continue
                    books = make_books(days[d], p, cost)
                    for name in ("put", "straddle", "P"):
                        if name == "straddle" and dl != 0.25:
                            continue
                        legs = books[name]
                        if legs is None:
                            continue
                        r = run_book(
                            days,
                            d,
                            p["expiry"],
                            legs,
                            dp,
                            funding=funding if fund else (),
                            option_fee=cost["option_fee"],
                            delivery_fee=cost["delivery_fee"],
                            hedge_fee=cost["hedge_fee"],
                        )
                        if r is not None:
                            r["n_atm"] = books["n_atm"]
                            r["iv_put"] = days[d]["expiries"][p["expiry"]]["strikes"][p["kp"]]["mark_iv"]
                            r["iv_atm"] = days[d]["expiries"][p["expiry"]]["strikes"][p["ka"]]["mark_iv"]
                            res[name][d] = r
                results[(tenor, dl, ck, fund)] = res
            print(f"  books {tenor} Δ{dl}: {sum(1 for p in pl.values() if p['covered'])} covered entry days", flush=True)
    return results


# ----------------------------------------------------------------------------- reporting

HDR = "| Вибірка | n | середнє | 95% CI | частка > 0 | найгірше | макс. DD (сума) | Sharpe (річний) |\n|---|---|---|---|---|---|---|---|"


def fmt(label: str, s: dict[str, Any], k: float = 100.0, unit: str = "") -> str:
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — | — | — | — |"
    ci = s["ci95"] or (float("nan"), float("nan"))
    sh = f"{s['sharpe']:.2f}" if s.get("sharpe") is not None else "—"
    return (
        f"| {label} | {s['n']} | {s['mean'] * k:+.2f}{unit} | [{ci[0] * k:+.2f}; {ci[1] * k:+.2f}] | {s['share_positive']:.0%} | "
        f"{s['min'] * k:+.2f} | {s['max_drawdown'] * k:.2f} | {sh} |"
    )


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def per_vega(r: dict[str, Any]) -> float:
    """P&L in vol points: USD P&L divided by |entry vega| (USD per vol point)."""
    return r["pnl"] * r["spot"] / abs(r["vega"]) / 100.0 if r["vega"] else 0.0


def h3_diff(res: dict[str, dict[int, dict[str, Any]]]) -> dict[int, dict[str, Any]]:
    out = {}
    for d, rp in res["put"].items():
        rs = res["straddle"].get(d)
        if rs is not None and rs["expiry"] == rp["expiry"]:
            out[d] = {"pnl": per_vega(rp) - per_vega(rs), "expiry": rp["expiry"], "day": d, "spot": rp["spot"]}
    return out


def phase_chains(trades: dict[int, dict[str, Any]], n_phases: int = 30) -> list[list[dict[str, Any]]]:
    last = max(trades) if trades else 0
    return [[trades[d] for d in chain(trades, ENTRY_START_MS + k * DAY_MS, last)] for k in range(n_phases)]


STRESS = (
    ("2022-05/06 (LUNA, 3AC)", "2022-05-05", "2022-06-30"),
    ("2022-11 (FTX)", "2022-11-06", "2022-11-14"),
    ("2024-08-05", "2024-08-05", "2024-08-05"),
    ("2025-10-10", "2025-10-10", "2025-10-10"),
)


def _ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def section(
    L: list[str], title: str, trades: dict[int, dict[str, Any]], *, key: str = "pnl", k: float = 100.0, unit: str = "% спота"
) -> dict[str, Any]:
    """Standard block: phase-0 non-overlapping, 30 phases, all days, by year, splits, top-3, worst, stress."""
    ppy = 365 / 30
    chains = phase_chains(trades)
    p0 = chains[0]
    vals0 = [t[key] for t in p0]
    s0 = stats(vals0, periods_per_year=ppy)
    L.append(f"\n#### {title} ({unit} за угоду)\n")
    L.append(HDR)
    L.append(fmt("неперекривні, фаза 0 (основна)", s0, k))
    pm = [statistics.mean([t[key] for t in c]) for c in chains if c]
    lows = [stats([t[key] for t in c])["ci95"][0] for c in chains if len(c) > 1]
    allv = [t[key] for t in trades.values()]
    L.append(fmt("усі дні входу (перекриваються, CI не валідний)", stats(allv), k))
    for lab, lo, hi in (("2021-2023", 0, SPLIT_MS), ("2024-2026", SPLIT_MS, 10**15)):
        L.append(fmt(f"{lab}, фаза 0", stats([t[key] for t in p0 if lo <= t["day"] < hi]), k))
        L.append(fmt(f"{lab}, усі дні", stats([t[key] for t in trades.values() if lo <= t["day"] < hi]), k))
    for y in sorted({ymd(d)[:4] for d in trades}):
        L.append(fmt(f"{y}, усі дні", stats([t[key] for d, t in trades.items() if ymd(d)[:4] == y]), k))
    srt = sorted(vals0, reverse=True)
    no_top = stats(srt[3:]) if len(srt) > 4 else {"n": 0}
    L.append(fmt("фаза 0 без топ-3 угод", no_top, k))
    L.append("")
    if pm:
        L.append(
            f"30 фаз старту: середнє від {min(pm) * k:+.2f} до {max(pm) * k:+.2f} (медіана {statistics.median(pm) * k:+.2f}); "
            f"нижня межа CI > 0 у {sum(1 for x in lows if x > 0)}/{len(lows)} фаз."
        )
    if p0:
        w = min(p0, key=lambda t: t[key])
        mult = w[key] / s0["mean"] if s0.get("mean") else float("nan")
        L.append(
            f"Найгірша угода фази 0: {ymd(w['day'])} → {ymd(w['expiry'])}: {w[key] * k:+.2f} ({mult:+.1f}× середнього); "
            f"найгірша серед усіх днів: {ymd(min(trades.values(), key=lambda t: t[key])['day'])} "
            f"{min(allv) * k:+.2f}."
        )
    L.append("\nСтрес-вікна (усі угоди, що тримались через дату; перекриваються):\n")
    L.append("| Вікно | n угод | середнє | найгірше | угода фази 0 |\n|---|---|---|---|---|")
    for lab, a, b in STRESS:
        lo, hi = _ms(a), _ms(b) + DAY_MS
        sel = [t for t in trades.values() if t["day"] < hi and t["expiry"] > lo]
        ph = [t for t in p0 if t["day"] < hi and t["expiry"] > lo]
        L.append(
            f"| {lab} | {len(sel)} | {statistics.mean(t[key] for t in sel) * k:+.2f} | {min(t[key] for t in sel) * k:+.2f} | "
            + (", ".join(f"{ymd(t['day'])}: {t[key] * k:+.2f}" for t in ph) or "—")
            + " |"
            if sel
            else f"| {lab} | 0 | — | — | — |"
        )
    return {"s0": s0, "phase_median": statistics.median(pm) if pm else None, "late": stats([t[key] for t in p0 if t["day"] >= SPLIT_MS])}


def h2_rows(
    plans: dict[int, dict[str, Any]], trades: dict[int, dict[str, Any]], dvol: list[tuple[int, float]], key: str = "pnl"
) -> list[dict[str, float]]:
    rr = [(d, p["rr25"]) for d, p in sorted(plans.items()) if p.get("rr25") is not None]
    rr_pct = trailing_percentile(rr, window_ms=90 * DAY_MS, min_obs=60)
    dv = dvol_known_at_entry(dvol)  # previous day's close: no look-ahead past the 09:00 entry
    dv_pct = trailing_percentile(sorted(dv.items()), window_ms=365 * DAY_MS, min_obs=180)
    rows = []
    for d in sorted(trades):
        if d in rr_pct and d in dv_pct:
            rows.append({"day": d, "pnl": trades[d][key], "rr_pct": rr_pct[d], "dvol_pct": dv_pct[d]})
    return rows


def h2_block(L: list[str], title: str, rows: list[dict[str, float]], k: float = 100.0) -> tuple[float | None, tuple[float, float] | None]:
    L.append(f"\n#### {title}\n")
    L.append("| DVOL (перц. 365д) | RR25 дорогий (перц. < 1/3) | середній | дешевий (≥ 2/3) | різниця |\n|---|---|---|---|---|")
    for lab, cond in (("< 0.5", lambda c: c < 0.5), ("≥ 0.5", lambda c: c >= 0.5)):
        part = [r for r in rows if cond(r["dvol_pct"])]
        cells = []
        for lo, hi in ((0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.01)):
            v = [r["pnl"] for r in part if lo <= r["rr_pct"] < hi]
            cells.append((statistics.mean(v) if v else None, len(v)))
        diff = cells[0][0] - cells[2][0] if cells[0][0] is not None and cells[2][0] is not None else None
        L.append(
            f"| {lab} | " + " | ".join(f"{c[0] * k:+.2f} (n={c[1]})" if c[0] is not None else "—" for c in cells) + f" | {diff * k:+.2f} |"
            if diff is not None
            else f"| {lab} | — | — | — | — |"
        )
    est = tercile_spread(rows)
    ci = block_bootstrap_ci(rows, tercile_spread, block=30, n_boot=2000)
    L.append(
        f"\nСтатистика H2 (середня по половинах DVOL різниця «дорогий − дешевий»): "
        f"{est * k:+.2f}"
        + (f", блоковий бутстреп 95% CI [{ci[0] * k:+.2f}; {ci[1] * k:+.2f}]" if ci else ", CI —")
        + f", n днів = {len(rows)}."
        if est is not None
        else "\nH2: недостатньо даних у клітинках."
    )
    return est, ci


SAMPLE_SEED = 20261014
SAMPLE_N = 40
SAMPLE_LAST = datetime(2026, 8, 29, tzinfo=UTC)


def sample_days() -> list[datetime]:
    """The 40 random entry days of the exploratory full-day coverage sample (fixed seed)."""
    start = datetime(2021, 4, 1, tzinfo=UTC)
    rng = random.Random(SAMPLE_SEED)
    return [start + timedelta(days=k) for k in sorted(rng.sample(range((SAMPLE_LAST - start).days), SAMPLE_N))]


def sample_todo(sdir: Path) -> list[datetime]:
    sdir.mkdir(parents=True, exist_ok=True)
    return [d for d in sample_days() if not (sdir / f"{d:%Y-%m-%d}.json").exists() and not (sdir / f"{d:%Y-%m-%d}.json.gz").exists()]


async def fetch_sample(sdir: Path, todo: list[datetime]) -> None:
    if not todo:
        return
    _, rt = build_server()
    try:
        for d in todo:
            dm = int(d.timestamp() * 1000)
            tr, _, _ = await fetch_window(rt, dm - 15 * HOUR_MS, dm + 9 * HOUR_MS)
            await asyncio.to_thread(write_day, sdir / f"{d:%Y-%m-%d}.json.gz", {"trades": tr})
            print(f"  sample {d:%Y-%m-%d}: {len(tr)} trades", flush=True)
    finally:
        await rt.aclose()


# (label, window start/end hours rel. to 00:00 UTC, surface scheme, target, DTE band, expiry rule, coverage test)
SAMPLE_ROWS = (
    ("v1-реєстрація: 08–10, найближча до 30д у [21; 45]", (8, 10), "v1", 30, (21, 45), "nearest", "covered"),
    ("00–09, найближча до 30д у [21; 45]", (0, 9), "v1", 30, (21, 45), "nearest", "covered"),
    ("24 год до 09:00, найближча до 30д у [21; 45]", (-15, 9), "v1", 30, (21, 45), "nearest", "covered"),
    ("08–10, найліквідніша за путами 25Δ у [21; 56] (лише ≥ 3 путових угод)", (8, 10), "v1", 30, (21, 56), "most_liquid_puts", "wing"),
    (
        "24 год до 09:00, найліквідніша за путами 25Δ у [21; 56] (лише ≥ 3 путових угод)",
        (-15, 9),
        "v1",
        30,
        (21, 56),
        "most_liquid_puts",
        "wing",
    ),
    ("08–10, найліквідніша за путами 25Δ у [21; 56], повна умова", (8, 10), "v1", 30, (21, 56), "most_liquid_puts", "covered"),
    ("24 год до 09:00, найліквідніша за путами 25Δ у [21; 56], повна умова", (-15, 9), "v1", 30, (21, 56), "most_liquid_puts", "covered"),
    (
        "**v2 основне:** 24 год, поверхня v2, monthly (остання п'ятниця) у [21; 56], найближча до 30",
        (-15, 9),
        "v2",
        30,
        (21, 56),
        "calendar_monthly",
        "covered",
    ),
    (
        "v2 розвідувальне: 24 год, поверхня v2, найліквідніша за всіма угодами у [21; 56]",
        (-15, 9),
        "v2",
        30,
        (21, 56),
        "most_liquid_total",
        "covered",
    ),
    ("v2 10д: 24 год, поверхня v2, найближча до 10 у [7; 14]", (-15, 9), "v2", 10, (7, 14), "nearest", "covered"),
)


def sample_coverage(sdir: Path) -> str:
    """Exploratory coverage rows (no P&L) on the fixed 40-day full-day sample; reproduces the report table."""
    asyncio.run(fetch_sample(sdir, sample_todo(sdir)))
    L = [f"# Розвідувальне покриття на {SAMPLE_N} випадкових днях (seed {SAMPLE_SEED}), без P&L\n"]
    L.append(
        "| Правило | покрито | медіана путових угод 25Δ | медіана DTE | вік mark путу, год (медіана / p90 / max) |\n|---|---|---|---|---|"
    )
    files = []
    for d in sample_days():
        p = sdir / f"{d:%Y-%m-%d}.json"
        files.append((d, p if p.exists() else sdir / f"{d:%Y-%m-%d}.json.gz"))
    data = [(int(d.timestamp() * 1000), read_day(p)["trades"]) for d, p in files]
    for label, (h0, h1), surf, tgt, band, rule, test in SAMPLE_ROWS:
        ok, wing, dte, ages = 0, [], [], []
        for dm, tr in data:
            sub = [t for t in tr if dm + h0 * HOUR_MS <= t["timestamp"] <= dm + h1 * HOUR_MS]
            snap = build_day(sub, dm, **SCHEMES[surf]["build"])
            pl = plan_entry(snap, target_days=tgt, dte_band=band, expiry_rule=rule) if snap else None
            wing.append(pl["wing_trades"] if pl else 0)
            if pl:
                dte.append(pl["dte"])
            hit = bool(pl) and (pl["covered"] if test == "covered" else pl["wing_trades"] >= 3)
            ok += hit
            if hit and pl["kp_mark_age_h"] is not None:
                ages.append(pl["kp_mark_age_h"])
        ages.sort()
        age_s = f"{statistics.median(ages):.1f} / {ages[int(0.9 * (len(ages) - 1))]:.1f} / {ages[-1]:.1f}" if ages else "—"
        L.append(
            f"| {label} | {ok}/{len(data)} = {ok / len(data):.1%} | {statistics.median(wing):.1f} | "
            f"{statistics.median(dte) if dte else float('nan'):.1f} | {age_s} |"
        )
    L.append(
        "\nВік mark: від останньої використаної угоди страйку 25Δ-путу до 09:00 (у поверхні v1 може бути від'ємним — угоди після 09:00)."
    )
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--scheme", choices=sorted(SCHEMES), default="v2")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--coverage-only", action="store_true", help="stop after the coverage gate and data checks (no P&L)")
    ap.add_argument("--sample-coverage", default="", help="directory of the 40-day full-day sample: print exploratory coverage rows only")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    for name in ("httpx", "httpcore"):  # one INFO line per request otherwise
        logging.getLogger(name).setLevel(logging.WARNING)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.sample_coverage:
        report = sample_coverage(Path(a.sample_coverage).expanduser())
        print(report)
        if a.out:
            Path(a.out).expanduser().write_text(report + "\n")
        return
    sch = SCHEMES[a.scheme]
    asyncio.run(fetch_all(cache, a.scheme))
    if a.fetch_only:
        return
    days, meta = load_snapshots(cache, a.scheme)
    delivery = {r["date"]: float(r["delivery_price"]) for r in json.loads((cache / "delivery_btc_usd.json").read_text())}
    funding = [(int(t), float(v)) for t, v in json.loads((cache / "funding_btc_perp.json").read_text())]
    dvol = [(int(t), float(v)) for t, v in json.loads((cache / "dvol_BTC.json").read_text())]
    plans = make_plans(days, sch["tenors"])
    win = sch["window"]

    L = [f"# Премія за путовий скью на Deribit BTC: сирий вивід `scripts/verify_put_skew.py --scheme {a.scheme}`\n"]
    L.append(
        f"Дані: {len(days)} днів із поверхнею, {meta['trades']} угод у вікнах [{win[0]:+d}; {win[1]:+d}] год від 00:00 UTC, "
        f"{meta['pages']} сторінок, "
        f"колізій пагінації (повна сторінка в одну мс) {meta['collisions']}, порожніх днів {len(meta['empty_days'])}. "
        f"Delivery prices {len(delivery)}, funding-годин {len(funding)}, DVOL-днів {len(dvol)}.\n"
    )

    # coverage
    L.append("## Покриття (поріг зафіксовано до завантаження: ≥ 80 % днів входу)\n")
    L.append(
        "| Тенор, Δ | днів входу | покрито | частка | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    cov_primary = None
    for (tenor, dl), pl in plans.items():
        tgt = sch["tenors"][tenor][1][1]
        all_days = [d for d in range(ENTRY_START_MS, LAST_EXPIRY_MS - int(tgt) * DAY_MS, DAY_MS)]
        ok = [d for d in all_days if (p := pl.get(d)) is not None and p["covered"]]
        share = len(ok) / len(all_days)
        by_y = []
        for y in range(2021, 2027):
            ys = [d for d in all_days if ymd(d)[:4] == str(y)]
            by_y.append(f"{sum(1 for d in ys if d in set(ok)) / len(ys):.0%}" if ys else "—")
        L.append(f"| {tenor}, {dl:.2f} | {len(all_days)} | {len(ok)} | {share:.0%} | " + " | ".join(by_y) + " |")
        if (tenor, dl) == ("30d", 0.25):
            cov_primary = share
    L.append(
        f"\nОсновна конфігурація (30д, 0.25): покриття {cov_primary:.1%} → поріг {'виконано' if cov_primary and cov_primary >= 0.8 else 'НЕ виконано'}.\n"
    )
    pl0 = plans[("30d", 0.25)]
    lo30, hi30 = sch["tenors"]["30d"][1]
    no_exp = f"немає торгованої експірації ({sch['tenors']['30d'][2]}) з DTE у [{lo30}; {hi30}]"
    base_days = list(range(ENTRY_START_MS, LAST_EXPIRY_MS - hi30 * DAY_MS, DAY_MS))
    reasons = {
        "немає поверхні дня": 0,
        no_exp: 0,
        "< 3 путових угод з \\|Δ\\| у [0.20; 0.30]": 0,
        "немає торгованого страйку 25Δ": 0,
        "ATM-страйк не в call-Δ [0.40; 0.60]": 0,
    }
    for d in base_days:
        p = pl0.get(d)
        if d not in days:
            reasons["немає поверхні дня"] += 1
        elif p is None:
            reasons[no_exp] += 1
        elif not p["covered"]:
            if p["wing_trades"] < 3:
                reasons["< 3 путових угод з \\|Δ\\| у [0.20; 0.30]"] += 1
            elif p["kp"] is None:
                reasons["немає торгованого страйку 25Δ"] += 1
            else:
                reasons["ATM-страйк не в call-Δ [0.40; 0.60]"] += 1
    L.append("Причини непокриття (30д, 0.25; перша причина, що спрацювала):\n")
    L.append("| Причина | днів | частка від усіх днів |\n|---|---|---|")
    for k, v in reasons.items():
        L.append(f"| {k} | {v} | {v / len(base_days):.1%} |")
    wing = [pl0[d]["wing_trades"] for d in base_days if d in pl0]
    if wing:
        L.append(
            f"\nПутових угод з |Δ| у [0.20; 0.30] у вибраній експірації за вікно: медіана {statistics.median(wing):.0f}, "
            f"частка днів з ≥ 1: {sum(1 for w in wing if w >= 1) / len(base_days):.0%}, з ≥ 3: {sum(1 for w in wing if w >= 3) / len(base_days):.0%}.\n"
        )

    for leg in ("kp", "ka"):
        ag = sorted(p[f"{leg}_mark_age_h"] for d in base_days if (p := pl0.get(d)) and p["covered"] and p[f"{leg}_mark_age_h"] is not None)
        if ag:
            L.append(
                f"Вік mark ({'25Δ пут' if leg == 'kp' else 'ATM'}) на покритих днях, год: медіана {statistics.median(ag):.2f}, "
                f"p90 {ag[int(0.9 * (len(ag) - 1))]:.2f}, max {ag[-1]:.2f}."
            )

    # sanity
    L.append("\n## Перевірки узгодженості\n")
    pairs = []
    dv = dvol_known_at_entry(dvol)
    for d, p in plans[("30d", 0.25)].items():
        if p["ka"] is not None and d in dv:
            pairs.append((days[d]["expiries"][p["expiry"]]["strikes"][p["ka"]]["mark_iv"], dv[d]))
    if pairs:
        L.append(
            f"- ATM mark IV (~30д, 09:00) проти DVOL на закритті попереднього дня: середнє {statistics.mean(x for x, _ in pairs) * 100:.1f}% проти "
            f"{statistics.mean(y for _, y in pairs) * 100:.1f}%, кореляція {pearson(pairs):.3f}, n={len(pairs)}."
        )
    dd = []
    for d, snap in days.items():
        dp = delivery.get(ymd(d))
        if dp:
            dd.append(abs(snap["spot"] / dp - 1))
    L.append(
        f"- Спот входу ({'медіана index вікна' if a.scheme == 'v1' else 'index за останні 15 хв до 09:00'}) проти delivery price 08:00 того ж дня: середнє |відхилення| {statistics.mean(dd) * 100:.2f}%, p95 {sorted(dd)[int(0.95 * len(dd))] * 100:.2f}%."
    )
    gate_failed = not cov_primary or cov_primary < 0.8
    if gate_failed or a.coverage_only:
        L.append(
            "\n**Поріг покриття не виконано. За попередньою реєстрацією P&L не рахується; картка → `inconclusive` (data-blocked).**"
            if gate_failed
            else "\n**Поріг покриття виконано; `--coverage-only`: P&L не рахувався.**"
        )
        report = "\n".join(L)
        print(report)
        if a.out:
            Path(a.out).expanduser().write_text(report + "\n")
        return
    if a.scheme == "v2":
        fut = asyncio.run(futures_check(cache, plans[("30d", 0.25)], days))
        diffs = []
        for ds, prints in fut.items():
            d = int(ds)
            p = plans[("30d", 0.25)].get(d)
            if prints and p is not None:
                fr = statistics.median(px / ix for px, ix in prints)
                diffs.append(days[d]["expiries"][p["expiry"]]["ratio"] - fr)
        if diffs:
            ad = sorted(abs(x) for x in diffs)
            L.append(
                f"- F/S з угод опціонів проти F/S квартального ф'ючерса (угоди за годину до 09:00), n={len(diffs)} днів: "
                f"медіана різниці {statistics.median(diffs) * 100:+.3f}%, медіана |різниці| {statistics.median(ad) * 100:.3f}%, "
                f"p95 |різниці| {ad[int(0.95 * (len(ad) - 1))] * 100:.3f}%; днів без угод ф'ючерса {sum(1 for v in fut.values() if not v)}."
            )
    results = run_all(days, plans, delivery, funding)
    pr = results[("30d", 0.25, "base", True)]
    puts = list(pr["put"].values())
    if puts:
        L.append(
            f"- Короткий 25Δ пут (30д): середня премія {statistics.mean(t['premium'] for t in puts) * 100:.2f}% спота, "
            f"vega {statistics.mean(abs(t['vega']) / t['spot'] for t in puts) * 100:.3f}% спота на вол-пункт; середня mark IV путу "
            f"{statistics.mean(t['iv_put'] for t in puts) * 100:.1f}% проти ATM {statistics.mean(t['iv_atm'] for t in puts) * 100:.1f}%; "
            f"vega-еквівалент ATM n = {statistics.mean(t['n_atm'] for t in puts):.2f} стреддла на пут."
        )
        stale = sum(t["stale_leg_days"] for t in puts) / max(1, sum(t["hedge_days"] for t in puts))
        L.append(f"- Частка нога-днів хеджу з застарілою IV (немає угод експірації у вікні): {stale:.2%}.")
        L.append(
            f"- Середні складові P&L путу, % спота: премія {statistics.mean(t['premium'] for t in puts) * 100:+.2f}, виплата "
            f"{statistics.mean(t['payoff'] for t in puts) * 100:+.2f}, хедж {statistics.mean(t['hedge_pnl'] for t in puts) * 100:+.2f}, "
            f"funding {statistics.mean(t['funding_pnl'] for t in puts) * 100:+.3f}, витрати −{statistics.mean(t['costs'] for t in puts) * 100:.2f}."
        )

    # primary
    L.append(
        "\n## Основна гіпотеза P: vega-нейтральна позиція (короткий 25Δ пут + довгий vega-еквівалент ATM-стреддла), 30д, основні витрати, з funding\n"
    )
    prim = section(L, "P", pr["P"])
    L.append("\n### H3: P&L на одиницю vega, пут − стреддл (вол-пункти, парно на тих самих датах)\n")
    h3 = section(L, "H3 різниця", h3_diff(pr), unit="вол-пунктів")
    L.append("\nДля довідки, на одиницю vega окремо (усі дні, вол-пункти):")
    L.append(
        f"пут {statistics.mean(per_vega(t) for t in pr['put'].values()) * 100:+.2f}, стреддл {statistics.mean(per_vega(t) for t in pr['straddle'].values()) * 100:+.2f}."
    )

    L.append("\n## H1 (вторинна): короткий 25Δ пут з дельта-хеджем, 30д\n")
    h1 = section(L, "H1 пут", pr["put"])
    L.append(
        "\nІнверсний облік (розвідувально): P&L у BTC за курсом експірації (`pnl_usd / S_T`, частка 1 BTC номіналу). "
        f"Фаза 0: середнє {statistics.mean(t['pnl_btc_at_expiry'] for t in phase_chains(pr['put'])[0]) * 100:+.2f}%, "
        f"найгірше {min(t['pnl_btc_at_expiry'] for t in pr['put'].values()) * 100:+.2f}% (USD-лінійне найгірше {min(t['pnl'] for t in pr['put'].values()) * 100:+.2f}%)."
    )
    L.append("\n## Порівняння: короткий ATM-стреддл (mark − 1 вп) з дельта-хеджем, 30д, ті самі дати\n")
    section(L, "стреддл", pr["straddle"])

    # H2
    L.append("\n## H2: терцилі RR25 (перцентиль за 90 днів) × половини DVOL (перцентиль за 365 днів), усі дні входу\n")
    h2 = h2_block(L, "H2 на P (основна залежна змінна)", h2_rows(plans[("30d", 0.25)], pr["P"], dvol))
    h2_block(L, "H2 на H1-путі (розвідувально)", h2_rows(plans[("30d", 0.25)], pr["put"], dvol))

    # grid
    L.append("\n## Сітка варіантів (усе розвідувальне, крім рядка «30d, 0.25, base, funding»)\n")
    L.append(
        "| Тенор | Δ | витрати | funding | книга | n фаза 0 | середнє фаза 0 | 95% CI | медіана 30 фаз | 2024-26 фаза 0 | усі дні |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for (tenor, dl, ck, fund), res in results.items():
        for name in ("P", "put", "straddle", "H3"):
            tr = h3_diff(res) if name == "H3" else res[name]
            if not tr:
                continue
            ch = phase_chains(tr)
            s0 = stats([t["pnl"] for t in ch[0]])
            if not s0.get("n"):
                continue
            pm = statistics.median([statistics.mean([t["pnl"] for t in c]) for c in ch if c])
            late = [t["pnl"] for t in ch[0] if t["day"] >= SPLIT_MS]
            ci = s0["ci95"] or (float("nan"), float("nan"))
            u = " вп" if name == "H3" else ""
            L.append(
                f"| {tenor} | {dl:.2f} | {ck} | {'так' if fund else 'ні'} | {name} | {s0['n']} | {s0['mean'] * 100:+.2f}{u} | "
                f"[{ci[0] * 100:+.2f}; {ci[1] * 100:+.2f}] | {pm * 100:+.2f} | "
                f"{(statistics.mean(late) * 100 if late else float('nan')):+.2f} | {statistics.mean(t['pnl'] for t in tr.values()) * 100:+.2f} |"
            )

    # verdict lines (mechanical, per pre-registration)
    def ok(x: dict[str, Any]) -> str:
        s0, late = x["s0"], x["late"]
        c1 = bool(s0.get("ci95")) and s0["ci95"][0] > 0
        c2 = (x["phase_median"] or 0) > 0
        c3 = bool(late.get("n")) and late["mean"] > 0
        return (
            f"CI фази 0 > 0: {'так' if c1 else 'ні'}; медіана 30 фаз > 0: {'так' if c2 else 'ні'}; 2024-2026 > 0: {'так' if c3 else 'ні'}"
        )

    L.append("\n## Механічна перевірка критеріїв попередньої реєстрації\n")
    L.append(f"- P: {ok(prim)}")
    L.append(f"- H3: {ok(h3)}")
    L.append(f"- H1: {ok(h1)}")
    est, ci = h2
    L.append(f"- H2: оцінка {est * 100 if est is not None else float('nan'):+.2f}, CI {'> 0' if ci and ci[0] > 0 else 'включає 0 або —'}")
    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    main()
