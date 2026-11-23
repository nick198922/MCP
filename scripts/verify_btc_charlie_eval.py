"""Evaluation of @btc_charlie's calls (card IDEA-20261113-x-traders, item 3.3 / 3.3б): markup and stage 0 only.

    uv run python scripts/verify_btc_charlie_eval.py --stage markup          # labels -> docs/calls/x/btc_charlie-eval.csv
    uv run python scripts/verify_btc_charlie_eval.py --stage zero            # counters from the markup, no network
    uv run python scripts/verify_btc_charlie_eval.py --stage zero --bars     # + 1h candle availability (loads candles,
                                                                             #   prints only availability; after the
                                                                             #   markup commit)
    uv run python scripts/verify_btc_charlie_eval.py --stage zero --smoke    # availability path on synthetic bars
    uv run python scripts/verify_btc_charlie_eval.py --stage full --smoke    # the full pipeline on synthetic bars
    uv run python scripts/verify_btc_charlie_eval.py --stage full --out ~/.cache/tradedesk-agents/btc_charlie_eval/full.txt

Labels come only from the pre-registered tags in the CSV ``note`` (``tradedesk.analytics.call_labels``); this script never
decides a label. Stage 0 prints counters by group and candle availability; it prints no return, P&L or price outcome
(the only price it reads is the open at t0, compared with the call's zone to catch ticker / unit collisions). The full
run (metric, placebo, verdict) follows the registration of card item 3.3в (pp. 1–20, committed before the run); it
writes the report to ``full_report.md`` in the cache dir and prints a short summary.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import math
import random
import re
import resource
import statistics
import sys
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from tradedesk.analytics import call_eval as ce
from tradedesk.analytics.call_labels import Labels, parse_tag, quote_units
from tradedesk.analytics.calls import Call, parse_calls
from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import (
    Plan,
    Trade,
    cluster_bootstrap_ci,
    cochran_q,
    funding_paid,
    holm,
    mde,
    per_call_r,
    per_call_ret,
    permutation_p,
    placed_plan,
    plan_entry,
    relative_plan,
    simulate,
    verdict_checks,
    week_of,
)

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "docs/calls/x/btc_charlie.csv"
MARKUP = ROOT / "docs/calls/x/btc_charlie-eval.csv"
CACHE = Path.home() / ".cache/tradedesk-agents/btc_charlie_eval"
H, D = 3_600_000, 86_400_000
NEED_H = 168 + 168  # registered as in 3.2: an order waits 7 days, a fill is held 7 days
GAP_H = 3  # a hole = a run of more than 3 missing 1h bars inside t0 … t0 + 336 h (review B 3.3б, p. 6)
SHORT_HOLE_H = 12  # 'short' if one hole is longer than 12 h ...
SHORT_MISSING = 0.05  # ... or the missing hours total more than 5 % of the 336 h window (set before the --bars run)
DRIFT_HOLD_H = 168  # the local-drift benchmark holds 7 days (3.3, metric p. 7)
SAMPLES = {"main": (date(2020, 1, 1), date(2022, 12, 31)), "combo": (date(2025, 10, 1), date(2026, 9, 30))}
VENUES = ("binanceusdm", "bybit", "okx", "gate", "bitget", "mexc")
PAGE = {"binanceusdm": 1000, "bybit": 1000, "okx": 100, "bitget": 200, "binance": 1000}
COLUMNS = (
    "id sample quarter units t0_utc t0_id t0_moved tag group ownership scalp exclusion primary idlist_variant border "
    "border_variant border_ownership border_alt q2_template q2_out limit_conditional"
).split()

# Compositions of the variants whose rows the CSV columns do not give (card 3.3в, p. 18; review B 3.3в, p. 2). Fixed
# from the note text and labels only (tweets of a row <= its t0, as in 3.3), before any P&L; the full run reads them.
STRICT_BOUNDARY_OUT = {  # 'stricter ex ante boundary' (< 0.25 R; no stop: swing < 1 %, scalp < 0.25 %): rows it drops
    "BC-2020-01-09-TOMO-LONG": "0.47 R",
    "BC-2020-05-26-TOMO-LONG": "0.46 R",
    "BC-2020-03-20-BTC-SHORT": "no stop, swing, m 1.16 % (primary reading, rule p. 4)",
    "BC-2025-10-10-BTC-LONG": "no stop, swing, m 1.50 %",
    "BC-2026-09-29-APT-LONG": "(0.8545 - 0.8292) / 0.1005 = 0.2517 R",
}
ONE_IDEA = {  # 'one idea': cluster head -> the members the variant drops (one tweet / one trade by the text <= t0)
    "BC-2020-01-05-TOMO-LONG": ("BC-2020-01-09-TOMO-LONG", "BC-2020-01-30-TOMO-LONG"),
    "BC-2020-03-20-BTC-LONG-2": ("BC-2020-04-29-BTC-LONG",),
}
CONFIRMED_ENTRY = {  # 'conditional as an entry at the author's confirmation': id -> (t0 UTC, confirming tweet id)
    "BC-2020-04-24-ALGO-LONG": ("2020-04-25T07:14", "1253945366384443400"),
}


@dataclass(frozen=True)
class Row:
    call: Call
    sample: str
    lab: Labels

    @property
    def id(self) -> str:
        return self.call.id

    @property
    def key(self) -> str:
        return self.call.symbol or self.call.asset


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M")


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def quarter(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def sample_of(d: date) -> str:
    return next((s for s, (lo, hi) in SAMPLES.items() if lo <= d <= hi), "")


def load_rows() -> tuple[list[Row], list[str]]:
    calls, errors = parse_calls(CALLS.read_text(encoding="utf-8"))
    rows = []
    for c in calls:
        try:
            lab = parse_tag(c.note)
        except ValueError as e:
            errors.append(f"{c.id}: {e}")
            continue
        if lab is None:
            errors.append(f"{c.id}: no 3.3 / 3.3б tag")
            continue
        rows.append(Row(c, sample_of(c.day), lab))
    return rows, errors


def markup_line(r: Row) -> dict[str, str]:
    lab = r.lab
    b = lambda x: "1" if x else "0"  # noqa: E731
    return {
        "id": r.id,
        "sample": r.sample,
        "quarter": quarter(r.call.day),
        "units": quote_units(r.call.symbol),
        "t0_utc": _fmt(lab.t0_ms),
        "t0_id": lab.t0_id,
        "t0_moved": b(lab.t0_moved),
        "tag": lab.tag,
        "group": lab.group,
        "ownership": lab.ownership,
        "scalp": b(lab.scalp),
        "exclusion": lab.exclusion,
        "primary": b(lab.primary),
        "idlist_variant": b(lab.idlist_variant),
        "border": b(lab.border),
        "border_variant": b(lab.border_variant),
        "border_ownership": lab.border_ownership,
        "border_alt": lab.border_alt[:120],
        "q2_template": b(lab.q2_template),
        "q2_out": b(lab.q2_out),
        "limit_conditional": b(lab.limit_conditional),
    }


def markup_text(rows: list[Row]) -> str:
    """The markup file's text (rows within the samples only); a test pins the committed file to this output."""
    buf = io.StringIO()
    buf.write(
        "# Розмітка btc_charlie для пункту 3 (картка IDEA-20261113-x-traders, 3.3 / 3.3б). Згенеровано скриптом "
        "scripts/verify_btc_charlie_eval.py --stage markup з тегів «3.3» / «3.3б» у note docs/calls/x/btc_charlie.csv, до будь-яких цін.\n"
        "# primary = ex ante без виключень; idlist_variant — обов'язковий варіант «виключення за списком id 3.3»; border_variant — "
        "обов'язковий варіант «межові за 3.3 у протилежній групі» (border_ownership — власність у ньому); q2_out — шаблон "
        "Q-20261118-2 поза правилом 10 хв; q2_template = 0 — обов'язковий варіант «без шаблону Q-20261118-2».\n"
    )
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in sorted(rows, key=lambda r: (r.sample, r.lab.t0_ms, r.id)):
        w.writerow(markup_line(r))
    return buf.getvalue()


def stage_markup(rows: list[Row]) -> None:
    MARKUP.write_text(markup_text(rows), encoding="utf-8")
    print(f"written {MARKUP.relative_to(ROOT)}: {len(rows)} rows")


def summary(rows: list[Row], sample: str) -> dict[str, int]:
    """Counters of one sample from the markup alone (no prices); a test pins them for the committed CSV."""
    sel = [r for r in rows if r.sample == sample]
    prim = [r for r in sel if r.lab.primary]
    brd = [r for r in sel if r.lab.border_variant]
    return {
        "rows": len(sel),
        "primary": len(prim),
        "idlist_variant": sum(r.lab.idlist_variant for r in sel),
        "border_variant": len(brd),
        "without_border_rows": sum(r.lab.primary and not r.lab.border for r in sel),
        "without_q2_template": sum(not r.lab.q2_template for r in prim),
        "q2_template_rows": sum(r.lab.q2_template for r in sel),
        "q2_template_in_primary": sum(r.lab.q2_template for r in prim),
        "strict_primary": sum(r.lab.ownership == "strict" for r in prim),
        "strict_border_variant": sum(r.lab.border_ownership == "strict" for r in brd),
        "strict_without_q2_template": sum(r.lab.ownership == "strict" and not r.lab.q2_template for r in prim),
        "scalps_primary": sum(r.lab.scalp for r in prim),
        "btc_units_primary": sum(quote_units(r.call.symbol) == "BTC" for r in prim),
        "shorts_primary": sum(r.call.side == "short" for r in prim),
        "weeks_primary": len({week_of(r.lab.t0_ms) for r in prim}),
    }


def confirmed_t0(row_id: str) -> int:
    t = datetime.strptime(CONFIRMED_ENTRY[row_id][0], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    return int(t.timestamp() * 1000)


def variant_ids(rows: list[Row], sample: str) -> dict[str, list[str]]:
    """Row ids of the registered variants not given by the CSV columns (card 3.3в, p. 18): the primary set minus the
    'stricter boundary' rows, minus the non-head members of 'one idea' clusters, plus the conditional rows entered at
    the author's confirmation (their t0 is ``confirmed_t0``)."""
    prim = [r.id for r in rows if r.sample == sample and r.lab.primary]
    members = {m for ms in ONE_IDEA.values() for m in ms}
    conf = [r.id for r in rows if r.sample == sample and r.id in CONFIRMED_ENTRY]
    return {
        "strict_boundary": [i for i in prim if i not in STRICT_BOUNDARY_OUT],
        "one_idea": [i for i in prim if i not in members],
        "confirmed_entry": prim + conf,
    }


def counters(rows: list[Row]) -> None:
    for s in SAMPLES:
        sel = [r for r in rows if r.sample == s]
        prim = [r for r in sel if r.lab.primary]
        print(f"\n=== sample {s}: {len(sel)} rows")
        print("summary:", summary(rows, s))
        by_q = Counter((quarter(r.call.day), r.lab.group) for r in sel)
        print("quarter   ex_ante in_profit unknown  primary")
        for q in sorted({q for q, _ in by_q}):
            pq = sum(quarter(r.call.day) == q for r in prim)
            print(f"{q}  {by_q[(q, 'ex_ante')]:7d} {by_q[(q, 'in_profit')]:9d} {by_q[(q, 'unknown')]:7d} {pq:8d}")
        ex = [r for r in sel if r.lab.group == "ex_ante"]
        print("exclusions among ex ante:", dict(Counter(r.lab.exclusion for r in ex if r.lab.exclusion)))
        print(f"primary {len(prim)} | idlist variant {sum(r.lab.idlist_variant for r in sel)} | border variant "
              f"{sum(r.lab.border_variant for r in sel)} | without border rows {sum(r.lab.primary and not r.lab.border for r in sel)}")  # fmt: skip
        print(f"  strict own in primary {sum(r.lab.ownership == 'strict' for r in prim)}; scalps in primary "
              f"{sum(r.lab.scalp for r in prim)}; units {dict(Counter(quote_units(r.call.symbol) for r in prim))}; "
              f"sides {dict(Counter(r.call.side for r in prim))}")  # fmt: skip
        print(
            f"  Q-20261118-2 template rows: {sum(r.lab.q2_template for r in sel)} (in primary "
            f"{sum(r.lab.q2_template for r in prim)}; outside the 10-min rule {sum(r.lab.q2_out for r in sel)}); "
            f"primary without template rows {sum(not r.lab.q2_template for r in prim)}"
        )
        print(f"  ISO weeks with a primary row (W): {len({week_of(r.lab.t0_ms) for r in prim})}")
        print("  t0 moved:", " ".join(r.id[3:] for r in sel if r.lab.t0_moved) or "none")
        print("  border rows:", " ".join(f"{r.id[3:]}({'in' if r.lab.border_variant else 'out'})" for r in sel if r.lab.border))
        print("  limit-style conditional:", " ".join(r.id[3:] for r in sel if r.lab.limit_conditional) or "none")
        print("  primary ids:", " ".join(r.id[3:] for r in prim))


# --- candle availability (stage 0, --bars) ------------------------------------------------------------------------


def candidates(key: str) -> list[tuple[str, str]]:
    """Markets tried in order: a pinned ``venue:SYM`` alone (plus the Binance archive for Binance spot); else perps,
    Binance spot, then the Binance archive (USD-M, spot), which keeps delisted pairs."""
    if ":" in key:
        venue, sym = key.split(":", 1)
        out = [(venue, sym)]
        if venue == "binance":
            out.append(("vision_spot", sym.replace("/", "")))
        return out
    return [*((v, f"{key}/USDT:USDT") for v in VENUES), ("binance", f"{key}/USDT"), ("vision_um", f"{key}USDT"),
            ("vision_spot", f"{key}USDT")]  # fmt: skip


def holes(bars: list[Bar], t0: int, need_ms: int) -> tuple[list[int], int]:
    """Holes inside the call's window (review B 3.3б, p. 6; edges per review B 3.3в, p. 1): every hour of the grid
    [hour of t0, t0 + ``need_ms``] without a bar is missing, including runs at the start and at the end of the window.
    Returns the lengths (hours) of the runs of more than ``GAP_H`` missing hours and the total of missing hours. Only
    bar times are read."""
    lo, hi = t0 // H * H, t0 + need_ms
    have = {b.t for b in bars if lo <= b.t <= hi}
    runs, run = [], 0
    for h in range(lo, hi + 1, H):
        if h in have:
            if run:
                runs.append(run)
            run = 0
        else:
            run += 1
    if run:
        runs.append(run)
    return [g for g in runs if g > GAP_H], sum(runs)


def availability(bars: list[Bar], t0: int, need_ms: int) -> str:
    """'missing' if no bar opens at or after the hour of t0 within 2 h past t0 (the call's first hours); 'short' if the
    bars stop before ``need_ms`` past t0, a hole (start and end of the window included) is longer than
    ``SHORT_HOLE_H`` or the missing hours total more than ``SHORT_MISSING`` of the window; else 'ok'. Only bar times are
    read. Thresholds registered with the output, before any return (card 3.3б)."""
    first = next((b.t for b in bars if b.t >= t0 // H * H), None)
    if first is None or first > t0 + 2 * H:
        return "missing"
    long_holes, missing = holes(bars, t0, need_ms)
    if any(g > SHORT_HOLE_H for g in long_holes) or missing > SHORT_MISSING * need_ms / H:
        return "short"
    return "ok" if bars[-1].t >= t0 + need_ms else "short"


def drift_coverage(times: set[int], t0: int) -> float:
    """Share of the hours h in [t0 − 30 d, t0 + 30 d] whose 7-day same-side hold (open of bar h, close of bar
    h + 167 h) is in the data: the local-drift benchmark of 3.3, metric p. 7. Only bar times are read."""
    base = t0 // H * H
    hours = range(base - 30 * D, base + 30 * D + 1, H)
    return sum(h in times and h + (DRIFT_HOLD_H - 1) * H in times for h in hours) / len(hours)


async def fetch_bars(rt, venue: str, sym: str, since: int, until: int, cache: Path, markets: dict) -> list[Bar]:
    path = cache / "bars" / f"{venue}_{sym.replace('/', '-').replace(':', '-')}_1h_{since}_{until}.json"
    if path.exists():
        return [Bar(*r) for r in json.loads(path.read_text())]
    rows: list[list] = []
    if venue.startswith("vision_"):
        bv = rt.binance_vision
        y, m = datetime.fromtimestamp(since / 1000, UTC).year, datetime.fromtimestamp(since / 1000, UTC).month
        while (y, m) <= (datetime.fromtimestamp(until / 1000, UTC).year, datetime.fromtimestamp(until / 1000, UTC).month):
            fn = bv.um_klines_monthly if venue == "vision_um" else bv.spot_klines_monthly
            try:
                got = (await fn(sym, "1h", y, m)).data or []
            except Exception as e:  # noqa: BLE001 — a missing month is "no data", logged
                logging.info("%s %s %d-%02d: %s", venue, sym, y, m, e)
                got = []
            rows += [[int(r[0]) // (1000 if r[0] > 1e14 else 1), *r[1:5]] for r in got]
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        if venue not in markets:
            markets[venue] = set(((await rt.exchanges.markets(venue)).data or {}).keys())
        if sym not in markets[venue]:
            return []
        page, t = PAGE.get(venue, 300), since
        while t <= until:
            end = min(until, t + (page - 1) * H)
            got = (await rt.exchanges.ohlcv(venue, sym, "1h", page, since_ms=t, until_ms=end)).data or []
            rows += [r for r in got if t <= int(r[0]) <= end]
            t = end + H
    seen, bars = set(), []
    for r in sorted(rows, key=lambda r: int(r[0])):
        if since <= int(r[0]) <= until and int(r[0]) not in seen and None not in r[1:5]:
            seen.add(int(r[0]))
            bars.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([[b.t, b.open, b.high, b.low, b.close] for b in bars]))
    return bars


async def load_markets(rows: list[Row], cache: Path) -> dict[str, tuple[str, list[Bar]]]:
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    by: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by[r.key].append(r.lab.t0_ms)
    out: dict[str, tuple[str, list[Bar]]] = {}
    _, rt = build_server()
    markets: dict = {}
    try:
        for k, ts in sorted(by.items()):
            since = min(ts) // D * D - 30 * D
            until = min(max(ts) // D * D + 60 * D, int(time.time() * 1000) // D * D - 1)
            t_start, best = time.time(), None
            for venue, sym in candidates(k):
                try:
                    bars = await fetch_bars(rt, venue, sym, since, until, cache, markets)
                except (ProviderError, KeyError, TypeError, ValueError) as e:
                    logging.warning("%s %s: %s", k, venue, e)
                    continue
                if bars and (best is None or bars[0].t < best[1][0].t):
                    best = (f"{venue}:{sym}", bars)
                if bars and bars[0].t <= min(ts) + 2 * H:
                    break
            out[k] = best or ("—", [])
            print(f"  bars {k:22} {out[k][0]:32} n={len(out[k][1]):6} {time.time() - t_start:5.1f}s rss={_rss_mb():.0f}MB", flush=True)
    finally:
        await rt.aclose()
    return out


def synthetic(rows: list[Row]) -> dict[str, tuple[str, list[Bar]]]:
    """Flat synthetic bars (price 1.0) from the first t0 − 30 d to the last t0 + 20 d of every market (smoke test of the
    availability path; the ticker / units check is skipped on them)."""
    by: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by[r.key].append(r.lab.t0_ms)
    out: dict[str, tuple[str, list[Bar]]] = {}
    for k, ts in by.items():
        lo, hi = min(ts) // H * H - 30 * D, max(ts) // H * H + 20 * D
        out[k] = ("synthetic", [Bar(t, 1.0, 1.0, 1.0, 1.0) for t in range(lo, hi + 1, H)])
    return out


def bar_rows(rows: list[Row]) -> list[Row]:
    """Rows whose markets stage 0 loads (the full run loads exactly these, so the market of every row is the same)."""
    sel = [r for r in rows if r.lab.group == "ex_ante" and r.lab.exclusion not in ("tradfi", "collision")]
    return sel + [r for r in rows if r.lab.border_variant and r not in sel]


def stage_bars(rows: list[Row], cache: Path, smoke: bool) -> None:
    sel = bar_rows(rows)
    series = synthetic(sel) if smoke else asyncio.run(load_markets(sel, cache))
    need = NEED_H * H
    print(f"\nthresholds: hole = > {GAP_H} missing 1h bars in a row in t0 … t0 + {NEED_H} h; 'short' if a hole > "
          f"{SHORT_HOLE_H} h or missing hours > {SHORT_MISSING:.0%} of the window, or the bars end before t0 + {NEED_H} h")  # fmt: skip
    times = {k: {b.t for b in v[1]} for k, v in series.items()}
    for s in SAMPLES:
        part = [r for r in sel if r.sample == s]
        by_id = {r.id: r for r in part}
        cov = {r.id: availability(series.get(r.key, ("—", []))[1], r.lab.t0_ms, need) for r in part}
        print(f"\n=== {s}: per row (ex ante + border-variant rows; market, status, holes > {GAP_H} h, missing hours, "
              "local-drift hours with a full 7-day hold)")  # fmt: skip
        drift = {}
        for r in sorted(part, key=lambda r: r.lab.t0_ms):
            src, bars = series.get(r.key, ("—", []))
            hl, miss = holes(bars, r.lab.t0_ms, need)
            drift[r.id] = drift_coverage(times.get(r.key, set()), r.lab.t0_ms)
            sets = "".join(c for c, f in (("P", r.lab.primary), ("I", r.lab.idlist_variant), ("B", r.lab.border_variant)) if f)  # fmt: skip
            print(f"  {r.id[3:]:24} {sets:3} {src:32} {cov[r.id]:7} holes={len(hl)} max={max(hl, default=0):3d}h "
                  f"missing={miss:3d}h drift={drift[r.id]:.2f}")  # fmt: skip
        prim = [r for r in part if r.lab.primary and cov[r.id] == "ok"]
        dr = sorted(drift[r.id] for r in prim)
        if dr:
            print(f"  {s} primary rows with data: local-drift coverage min {dr[0]:.2f}, median {dr[len(dr) // 2]:.2f}; "
                  f"rows < 0.50: {sum(x < 0.5 for x in dr)}")  # fmt: skip
        holed = [r for r in part if r.lab.primary and holes(series.get(r.key, ("", []))[1], r.lab.t0_ms, need)[0]]
        print(f"  {s} primary rows with any hole > {GAP_H} h:", " ".join(r.id[3:] for r in holed) or "none")
        for name, pick in (("primary", lambda r: r.lab.primary), ("idlist variant", lambda r: r.lab.idlist_variant),
                           ("border variant", lambda r: r.lab.border_variant)):  # fmt: skip
            c = Counter(cov[r.id] for r in part if pick(r))
            weeks = len({week_of(r.lab.t0_ms) for r in part if pick(r) and cov[r.id] == "ok"})
            print(f"{s} {name:15} ok={c['ok']} short={c['short']} missing={c['missing']} weeks_with_data={weeks}")
        c = Counter(cov[r.id] for r in part if r.lab.primary and not r.lab.q2_template)
        print(f"{s} {'no Q2 template':15} ok={c['ok']} short={c['short']} missing={c['missing']}")
        for r in part:
            if r.id in CONFIRMED_ENTRY:  # the variant's row is read from its confirmation time, not from t0
                cov[r.id] = availability(series.get(r.key, ("—", []))[1], confirmed_t0(r.id), need)
        for name, ids in variant_ids(part, s).items():
            c = Counter(cov[i] for i in ids)
            weeks = len({week_of(confirmed_t0(i) if i in CONFIRMED_ENTRY else by_id[i].lab.t0_ms)
                         for i in ids if cov[i] == "ok"})  # fmt: skip
            print(f"{s} {name:15} ok={c['ok']} short={c['short']} missing={c['missing']} weeks_with_data={weeks}")
        print(f"  {s} no_data (primary):", " ".join(r.id[3:] for r in part if r.lab.primary and cov[r.id] != "ok") or "none")
        print(f"  {s} markets from the Binance archive:",
              " ".join(sorted({series[r.key][0] for r in part if series.get(r.key, ('',))[0].startswith('vision')})) or "none")  # fmt: skip
        far = []
        for r in [] if smoke else part:
            bars = series.get(r.key, ("", []))[1]
            first = next((b for b in bars if b.t >= r.lab.t0_ms), None)
            mid = (r.call.zone_low + r.call.zone_high) / 2
            if first and not 0.5 <= first.open / mid <= 2.0:
                far.append(f"{r.id[3:]}({first.open / mid:.2f}x)")
        print(f"  {s} open at t0 outside 0.5–2x of the zone (ticker / units check):", " ".join(far) or "none")
    beam = next((r for r in sel if r.id == "BC-2020-04-24-BEAM-LONG"), None)
    if beam:
        print(f"BEAM 04-24 stop-entry {beam.call.zone_low} (style nearest): market {series.get(beam.key, ('—',))[0]}, "
              f"bars {availability(series.get(beam.key, ('', []))[1], beam.lab.t0_ms, need)}")  # fmt: skip
    print(f"peak rss {_rss_mb():.0f} MB")


# --- full run (card 3.3в, registration pp. 1–20; committed before this code ran) -------------------------------------

FILL_H = HOLD_H = 168  # order window and hold, 7 days each (p. 4–5)
SPAN = (FILL_H + HOLD_H + 2) * H
PERMS = 1000
SEED_FULL = 20261123  # +0 P1, +1 P2, +2 P3 (p. 9); +3 P2 with a random time over the whole pool (reference)
N_BOOT = 10_000
MARGIN_MS = 14 * D  # a placebo start leaves 14 days of data (order window + hold)
POOLS = {  # placebo time pool: blocks of the quarters that entered, start − 30 d … end + 60 d (p. 9, 16)
    "main": ((date(2019, 12, 2), date(2020, 11, 29)), (date(2021, 9, 1), date(2022, 5, 30))),
    "combo": ((date(2025, 9, 1), date(2026, 11, 29)),),
}
HOLM_KNOWN = (("KillaXBT", 0.5115), ("Trader_XO", 0.7183))  # p. 15
SECONDARY_PLUS = "BC-2020-05-11-BTC-SHORT"  # 3.3: secondary set 'strictly own' + this row
MODES = (("P1", "time_side", 0), ("P2", "time", 1), ("P3", "side", 2), ("P2pool", "time_pool", 3))
Z_HOLM = 2.241  # one-sided z at 0.0125 (MDE for ✓, p. 20)
NAN = float("nan")
SAT = 1e-8  # one satoshi: the smallest price step of a */BTC market (exploratory cost floor, review B 33в)


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def pool_blocks(sample: str, now_ms: int) -> list[tuple[int, int]]:
    return [(_day_ms(a), min(_day_ms(b) + D - H, now_ms)) for a, b in POOLS[sample]]


@dataclass(frozen=True)
class Unit:
    """One evaluated call: a markup row at its t0 (or at the author's confirmation for the 'confirmed entry' variant)."""

    row: Row
    t0: int
    market: str
    status: str  # stage-0 availability: ok | short | missing
    confirmed: bool = False

    @property
    def uid(self) -> str:
        return self.row.id + ("@confirmed" if self.confirmed else "")

    @property
    def side(self) -> str:
        return self.row.call.side

    @property
    def week(self) -> int:
        return week_of(self.t0)

    @property
    def cost(self) -> float:
        return ce.cost_of(self.market)


Series = dict[str, tuple[list[Bar], list[int]]]


def _series(bars: list[Bar]) -> tuple[list[Bar], list[int]]:
    b = sorted({x.t: x for x in bars}.values(), key=lambda x: x.t)
    return b, [x.t for x in b]


def _window(S: Series, market: str, start: int) -> list[Bar]:
    bars, times = S[market]
    return bars[bisect_left(times, start) : bisect_left(times, start + SPAN)]


def unit_plan(u: Unit, ref: float, style: str = "nearest") -> Plan:
    c = u.row.call
    if u.confirmed:  # entry at the author's confirmation: market, no stop or target (p. 18)
        return Plan(c.side, "market", None, None, None)
    return plan_entry(c.side, c.zone_low, c.zone_high, ref, disclosure=False, stop=c.stop, target=c.target, style=style)


def run_unit(u: Unit, S: Series, *, cost: float | None = None, style: str = "nearest", start: int | None = None,
             plan: Plan | None = None) -> Trade:  # fmt: skip
    """The registered replay of one call (pp. 3–5); ``no_data`` for a 'missing' row or a hold window not in the data."""
    if u.status == "missing" or u.market not in S:
        return Trade("no_data")
    st = ce.first_hour_after(u.t0) if start is None else start
    w = _window(S, u.market, st)
    if not w or w[0].t > st + 2 * H:
        return Trade("no_data")
    p = plan or unit_plan(u, w[0].open, style)
    tr = simulate(w, p, start_ms=st, fill_ms=FILL_H * H, hold_ms=HOLD_H * H, cost_frac=u.cost if cost is None else cost,
                  sorted_bars=True)  # fmt: skip
    if not ce.short_row_evaluable(tr.fill_t, tr.filled, tr.status, S[u.market][1][-1], HOLD_H * H):
        return Trade("no_data")
    return tr


def strict_fill_plan(u: Unit, S: Series) -> Plan:
    """Exploratory (review B 33в): the registered plan with a limit / stop price moved by 1e-9 (relative) away from the
    side it fills from, so a bar that only touches the order price no longer fills it (the price must pass through)."""
    w = _window(S, u.market, ce.first_hour_after(u.t0))
    p = unit_plan(u, w[0].open)
    if p.kind == "market" or p.entry is None:
        return p
    up = (p.kind == "limit") == (p.side == "short")  # a sell limit / buy stop sits above the price
    return replace(p, entry=p.entry * (1 + 1e-9 if up else 1 - 1e-9))


def value(tr: Trade) -> float | None:
    v = per_call_ret([tr])
    return v[0] if v else None


def placebo(units: list[Unit], S: Series, pools: dict[str, list[int]], mode: str, off: int, n: int) -> tuple[dict[str, list[float]], list[int]]:  # fmt: skip
    """Per-unit placebo values of ``n`` sets (nan = no value) with the call's geometry (p. 9), and [simulated fills with
    a stop, of them with the stop removed by a gap]. Each unit draws from its own generator seeded by the registered
    seed and its id, so a unit's draws do not depend on which other units are evaluated."""
    out, drop = {}, [0, 0]
    for u in units:
        st = ce.first_hour_after(u.t0)
        w = _window(S, u.market, st)
        rel = relative_plan(unit_plan(u, w[0].open), w[0].open)
        pool = pools[u.market]
        a, b = ce.window_starts(pool, st, 30 * D) if mode == "time" else (0, len(pool))
        rnd = random.Random(f"{SEED_FULL + off}|{u.uid}")
        vals = []
        for _ in range(n):
            t = st if mode == "side" or b <= a else pool[rnd.randrange(a, b)]
            side = rnd.choice(("long", "short")) if mode in ("time_side", "side") else u.side
            ww = _window(S, u.market, t)
            if not ww:
                vals.append(NAN)
                continue
            pp = placed_plan(rel, side, ww[0].open)
            tr = simulate(ww, pp, start_ms=t, fill_ms=FILL_H * H, hold_ms=HOLD_H * H, cost_frac=u.cost, sorted_bars=True)
            if pp.stop is not None and tr.filled:
                drop[0] += 1
                drop[1] += tr.risk is None
            x = value(tr)
            vals.append(NAN if x is None else x)
        out[u.uid] = vals
    return out, drop


def build_pools(sample: str, markets: list[str], S: Series, now_ms: int) -> dict[str, list[int]]:
    """Placebo start pool per market, built from this sample's own blocks only (p. 9): the main and combo pools of a
    market present in both samples (BTC) never mix."""
    return {m: ce.pool_starts(S[m][1], pool_blocks(sample, now_ms), MARGIN_MS) for m in sorted(set(markets))}


def with_funding(u: Unit, tr: Trade, x: float | None, funding: dict[str, list[tuple[int, float]]]) -> tuple[float | None, bool]:  # fmt: skip
    """The value net of funding (p. 8) and whether funding was due but not available. Funding is charged only on a
    filled perp; spot and */BTC rows are returned unchanged."""
    if x is None or not tr.filled or not ce.is_perp(u.market):
        return x, False
    if u.market.startswith("binanceusdm:") and u.market in funding:
        return x - funding_paid(funding[u.market], u.side, tr.fill_t, tr.exit_t + H), False  # type: ignore[arg-type]
    return x, True


def set_means(mat: dict[str, list[float]], uids: list[str], n: int) -> list[float]:
    out = []
    for i in range(n):
        xs = [mat[u][i] for u in uids if u in mat and not math.isnan(mat[u][i])]
        out.append(math.fsum(xs) / len(xs) if xs else NAN)
    return out


def stats(units: list[Unit], vals: dict[str, float | None], filled: set[str] | None = None) -> dict:
    pairs = [(u, vals[u.uid]) for u in units if vals.get(u.uid) is not None]
    v = [float(x) for _, x in pairs]  # type: ignore[arg-type]
    out = {"n": len(v), "units": [u for u, _ in pairs], "values": v, "weeks": len({u.week for u, _ in pairs}),
           "filled": sum(u.uid in filled for u, _ in pairs) if filled is not None else None}  # fmt: skip
    if len(v) < 2:
        return out | {"too_few": True, "mean": statistics.fmean(v) if v else NAN, "lo": NAN, "hi": NAN, "sd": NAN}
    lo, hi = cluster_bootstrap_ci(v, [u.week for u, _ in pairs], n_boot=N_BOOT, seed=SEED_FULL)
    return out | {"mean": statistics.fmean(v), "lo": lo, "hi": hi, "sd": statistics.pstdev(v)}


async def extend_for_pools(series: dict[str, tuple[str, list[Bar]]], need: dict[str, set[str]], cache: Path, now_ms: int) -> None:  # fmt: skip
    """Loads the market's bars over the placebo pool of every sample it is used in (p. 9) and merges them."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    by_m = {src: k for k, (src, _) in series.items()}
    _, rt = build_server()
    markets: dict = {}
    try:
        for m, samples in sorted(need.items()):
            venue, sym = m.split(":", 1)
            k = by_m[m]
            got = list(series[k][1])
            t1 = time.time()
            for s in sorted(samples):
                for lo, hi in pool_blocks(s, now_ms):
                    try:
                        got += await fetch_bars(rt, venue, sym, lo, hi, cache, markets)
                    except (ProviderError, KeyError, TypeError, ValueError) as e:
                        logging.warning("pool %s %s: %s", m, s, e)
            series[k] = (m, _series(got)[0])
            print(f"  pool {m:32} {','.join(sorted(samples)):10} n={len(series[k][1]):6} {time.time() - t1:5.1f}s rss={_rss_mb():.0f}MB", flush=True)  # fmt: skip
    finally:
        await rt.aclose()


async def load_funding_bc(markets: list[str], spans: dict[str, tuple[int, int]], cache: Path) -> dict[str, list[tuple[int, float]]]:  # fmt: skip
    """Binance USD-M funding settlements over the holds of the perp rows (p. 8)."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    out: dict[str, list[tuple[int, float]]] = {}
    _, rt = build_server()
    try:
        for m in markets:
            sym = m.split(":", 1)[1]
            lo, hi = spans[m]
            f = cache / "funding" / f"binanceusdm_{sym.replace(':', '-').replace('/', '-')}_{lo}_{hi}.json"
            if f.exists():
                out[m] = [tuple(x) for x in json.loads(f.read_text())]  # type: ignore[misc]
                continue
            try:
                got = (await rt.exchanges.funding_rate_history_range("binanceusdm", sym, lo, hi)).data or {}
            except (ProviderError, KeyError, TypeError, ValueError) as e:
                logging.warning("funding %s: %s", m, e)
                continue
            rows = sorted((int(x["timestamp"]), float(x["fundingRate"])) for x in got.get("rows", []) if x.get("fundingRate") is not None)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(rows))
            out[m] = rows
            print(f"  funding {m}: {len(rows)} settlements", flush=True)
    finally:
        await rt.aclose()
    return out


def stage0_markets(cache: Path) -> dict[str, str]:
    """id -> market from the stage-0 output (the market is fixed there, p. 2)."""
    p = cache / "stage0_bars.txt"
    if not p.exists():
        return {}
    pat = re.compile(r"^  (\d{4}-\d\d-\d\d-\S+)\s+[PIB]*\s+(\S+)\s+(ok|short|missing)\s+holes=")
    return {"BC-" + m[1]: m[2] for line in p.read_text(encoding="utf-8").splitlines() if (m := pat.match(line))}


def synthetic_walk(sel: list[Row], now_ms: int, seed: int = 7) -> dict[str, tuple[str, list[Bar]]]:
    """Random-walk bars that never saw a market, over each market's pool blocks and call windows (--smoke only)."""
    rnd = random.Random(seed)
    out = {}
    by: dict[str, list[Row]] = defaultdict(list)
    for r in sel:
        by[r.key].append(r)
    for k, rs in by.items():
        hours: set[int] = set()
        for s in {r.sample for r in rs}:
            for lo, hi in pool_blocks(s, now_ms):
                hours |= set(range(lo // H * H, hi + 1, H))
        for r in rs:
            hours |= set(range(r.lab.t0_ms // H * H - 31 * D, r.lab.t0_ms // H * H + 46 * D, H))
        anchors = sorted((r.lab.t0_ms, (r.call.zone_low + r.call.zone_high) / 2) for r in rs)
        bars, x, j = [], 0.0, 0
        for t in sorted(hours):
            while j + 1 < len(anchors) and anchors[j + 1][0] <= t:
                j += 1
            x = 0.98 * x + rnd.gauss(0.0, 0.006)
            o = anchors[j][1] * math.exp(x)
            c = o * math.exp(rnd.gauss(0.0, 0.004))
            bars.append(Bar(t, o, max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002))), c))
        out[k] = (f"binance:SYN{k.replace(':', '').replace('/', '')}/BTC", bars)
    return out


def _pct(x: float) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:+.2%}"


def evaluate(sample: str, rows: list[Row], S: Series, src: dict[str, str], avail: dict[str, str], now_ms: int, perms: int,
             funding: dict[str, list[tuple[int, float]]], emit) -> dict:  # fmt: skip
    """The registered test on one sample (main: pp. 1–20; combo: p. 16); writes its section through ``emit``."""
    part = [r for r in rows if r.sample == sample]
    conf_ids = [r.id for r in part if r.id in CONFIRMED_ENTRY]
    pick = [r for r in part if r.lab.primary or r.lab.border_variant or r.id == SECONDARY_PLUS
            or (r.lab.group == "ex_ante" and r.lab.exclusion in ("recovered", "meme") and not r.lab.q2_out)]  # fmt: skip
    units = [Unit(r, r.lab.t0_ms, src.get(r.key, "—"), avail.get(r.id, "missing")) for r in pick]
    units += [Unit(r, confirmed_t0(r.id), src.get(r.key, "—"), avail.get(r.id + "@confirmed", "missing"), True)
              for r in part if r.id in conf_ids]  # fmt: skip
    trades = {u.uid: run_unit(u, S) for u in units}
    vals = {k: value(t) for k, t in trades.items()}
    filled = {k for k, t in trades.items() if t.filled and vals[k] is not None}
    prim_all = [u for u in units if u.row.lab.primary and not u.confirmed]
    prim = [u for u in prim_all if vals[u.uid] is not None]
    no_data = [u for u in prim_all if vals[u.uid] is None]
    st = stats(prim, vals, filled)
    emit(f"## Вибірка {sample} ({SAMPLES[sample][0]} … {SAMPLES[sample][1]})")
    emit()
    emit(f"Первинний набір: {len(prim_all)} рядків; оцінено {len(prim)}; no_data {len(no_data)}: "
         + (", ".join(f"{u.row.id} ({u.status})" for u in no_data) or "—"))  # fmt: skip
    if st.get("too_few"):
        emit(f"n = {st['n']} < 2 — не визначено (◯)")
        return {"mark": "◯", "p": 1.0, "n": st["n"], "mean": NAN}
    # placebo: all evaluated units of every variant (one matrix per mode)
    ev = [u for u in units if vals[u.uid] is not None]
    pools = build_pools(sample, [u.market for u in ev], S, now_ms)
    mats, drops = {}, {}
    for name, mode, off in MODES:
        t1 = time.time()
        mats[name], drops[name] = placebo(ev, S, pools, mode, off, perms)
        print(f"  {sample} placebo {name}: {perms} sets x {len(ev)} units in {time.time() - t1:.0f}s rss={_rss_mb():.0f}MB", flush=True)

    def p_of(us: list[Unit], mean_x: float, name: str = "P1") -> float:
        return permutation_p(mean_x, set_means(mats[name], [u.uid for u in us if vals[u.uid] is not None], perms))

    p = {name: p_of(prim, st["mean"], name) for name, _, _ in MODES}
    # ✓ conditions
    ts = sorted(u.t0 for u in prim)
    split = ts[len(ts) // 2]
    halves = [stats([u for u in prim if (u.t0 < split) == h], vals, filled) for h in (True, False)]
    tr2 = {u.uid: run_unit(u, S, cost=2 * u.cost) for u in prim}
    cost2 = stats(prim, {k: value(t) for k, t in tr2.items()}, filled)
    no_fund, fund_vals = [], {}
    for u in prim:
        fund_vals[u.uid], lacking = with_funding(u, trades[u.uid], vals[u.uid], funding)
        if lacking:
            no_fund.append(u.row.id)
    fund = stats(prim, fund_vals, filled)
    top5 = {u.uid for u, _ in sorted(zip(st["units"], st["values"], strict=True), key=lambda z: -z[1])[:5]}
    wo5 = stats([u for u in prim if u.uid not in top5], vals, filled)
    drift, cover = {}, {}
    for u in ev:
        bars, _ = S[u.market]
        by_t = {b.t: b for b in bars if u.t0 - 31 * D <= b.t <= u.t0 + 38 * D}
        drift[u.uid], cover[u.uid] = ce.local_drift(by_t, u.side, u.t0, cost=u.cost)
    excess = stats(prim, {u.uid: vals[u.uid] - drift[u.uid] for u in prim if not math.isnan(drift[u.uid])}, filled)  # type: ignore[operator]
    checks = {
        "(а) нижня межа CI > 0": st["lo"] > 0,
        "(б) p(П1) < 0.0125": p["P1"] < 0.0125,
        "(в) p(П2) < 0.05": p["P2"] < 0.05,
        "(г) нижня межа CI надлишку над локальним дрейфом > 0": excess["lo"] > 0,
        "(д) обидві половини > 0": all(h["mean"] > 0 for h in halves),
        "(е) > 0 при витратах ×2": cost2["mean"] > 0,
        "(є) > 0 з фандингом": fund["mean"] > 0,
        "(ж) > 0 без 5 найкращих": wo5["mean"] > 0,
    }
    gate = (30, 20) if sample == "main" else (10, 0)
    v = verdict_checks(n_calls=st["n"], n_filled=st["filled"], mean_x=st["mean"], p_placebo=p["P1"], checks=checks,
                       min_calls=gate[0], min_filled=gate[1])  # fmt: skip
    emit()
    emit(f"**Вердикт первинної: {v.mark}** — {v.reason}")
    emit()
    emit(f"- n = {st['n']} (заповнено {st['filled']}), кластерів-тижнів {st['weeks']}; середнє net {_pct(st['mean'])} на номінал на "
         f"заклик; 95 % CI [{_pct(st['lo'])}; {_pct(st['hi'])}] ({st['weeks']} кластерів); sd {st['sd']:.2%}")  # fmt: skip
    emit(f"- плацебо ({perms} наборів): p(П1 час+сторона) = {p['P1']:.4f}; p(П2 та сама сторона, час у t0 ± 30 д) = {p['P2']:.4f}; "
         f"p(П3 той самий час, випадкова сторона) = {p['P3']:.4f}; p(П2 час у всьому пулі, довідково) = {p['P2pool']:.4f}")  # fmt: skip
    for name, _, _ in MODES:
        xs = sorted(x for x in set_means(mats[name], [u.uid for u in prim], perms) if not math.isnan(x))
        emit(f"  - {name}: середнє наборів {_pct(statistics.fmean(xs))}, 95-й перцентиль {_pct(xs[int(0.95 * (len(xs) - 1))])}; "
             f"стоп знято гепом у {drops[name][1]} з {drops[name][0]} заповнень зі стопом ({drops[name][1] / max(1, drops[name][0]):.1%})")  # fmt: skip
    sd, w = st["sd"], st["weeks"]
    emit(f"- MDE за фактичним sd {sd:.2%} і {w} тижнями: {mde(sd, w):.2%} (поріг ≈ / ✗), {mde(sd, w, z_alpha=Z_HOLM):.2%} (✓, p < 0.0125)")
    emit("- умови ✓: " + "; ".join(f"{k}: {'так' if ok else 'ні'}" for k, ok in checks.items()))
    stat_codes = Counter(trades[u.uid].status for u in prim)
    emit(f"- статуси угод: {dict(stat_codes)}; заповнено {st['filled']}/{st['n']}")
    dropped = [u.row.id for u in prim if trades[u.uid].filled and u.row.call.stop is not None and trades[u.uid].risk is None]
    emit(f"- скинутий стоп у факті (геп через стоп у свічці заповнення): {len(dropped)} — {', '.join(dropped) or '—'}")
    emit(f"- перп-рядки без історії фандингу (0): {', '.join(no_fund) or '—'}")
    cov = sorted(cover[u.uid] for u in prim)
    emit(f"- покриття локального дрейфу: мін {cov[0]:.2f}, медіана {cov[len(cov) // 2]:.2f}")
    emit()
    head = "| Варіант | n | заповнено | E[дохід] net / заклик | 95 % CI (кластерів) | p(П1) |"

    def line(name: str, s: dict, pv: float | None = None) -> str:
        if s.get("too_few"):
            return f"| {name} | {s['n']} | — | не визначено (n < 2) | — | — |"
        pp = "—" if pv is None else f"{pv:.4f}"
        f = "—" if s.get("filled") is None else s["filled"]
        return f"| {name} | {s['n']} | {f} | {_pct(s['mean'])} | [{_pct(s['lo'])}; {_pct(s['hi'])}] ({s['weeks']}) | {pp} |"

    def sub(name: str, us: list[Unit], vv: dict | None = None, with_p: bool = True) -> None:
        s = stats(us, vv or vals, filled)
        emit(line(name, s, p_of(us, s["mean"]) if with_p and not s.get("too_few") and vv is None else None))

    emit("### Первинна й умови ✓")
    emit()
    emit(head)
    emit("|---|---|---|---|---|---|")
    emit(line("первинний (усі одиниці разом)", st, p["P1"]))
    for un in ("USD", "BTC"):
        sub(f"частка в {un} (ринки */{'USDT' if un == 'USD' else 'BTC'})", [u for u in prim if quote_units(u.row.call.symbol) == un])
    emit(line(f"половина 1 (t0 < {_fmt(split)})", halves[0]))
    emit(line("половина 2", halves[1]))
    emit(line("витрати ×2", cost2))
    emit(line("з фандингом", fund))
    emit(line("без 5 найкращих", wo5))
    emit(line("надлишок над локальним дрейфом тієї ж сторони (факт − D)", excess))
    emit()
    emit("Найкращі 5: " + ", ".join(f"{u.row.id} {_pct(vals[u.uid])}" for u in sorted(prim, key=lambda u: -vals[u.uid])[:5]))  # type: ignore[operator]
    emit()
    emit("### Варіанти (усі зареєстровані; вердикт — за первинним)")
    emit()
    emit(head)
    emit("|---|---|---|---|---|---|")
    lab = lambda u: u.row.lab  # noqa: E731
    base = [u for u in units if not u.confirmed]
    sub("обов'язковий: виключення за списком id 3.3", [u for u in base if lab(u).idlist_variant])
    sub("обов'язковий: межові за 3.3 у протилежній групі", [u for u in base if lab(u).border_variant])
    sub("обов'язковий: без шаблону Q-20261118-2", [u for u in prim_all if not lab(u).q2_template])
    vids = variant_ids([u.row for u in base], sample)
    sub("строгіша межа ex ante", [u for u in prim_all if u.row.id in set(vids["strict_boundary"])])
    sub("одна ідея (голови кластерів)", [u for u in prim_all if u.row.id in set(vids["one_idea"])])
    sub("умовні як вхід у момент підтвердження (ALGO 25.04)", prim_all + [u for u in units if u.confirmed])
    rec = [u for u in base if lab(u).group == "ex_ante" and lab(u).exclusion == "recovered" and not lab(u).q2_out]
    meme = [u for u in base if lab(u).group == "ex_ante" and lab(u).exclusion == "meme" and not lab(u).q2_out]
    sub(f"+ відновлені ({len(rec)})", prim_all + rec)
    sub(f"+ мем ({len(meme)})", prim_all + meme)
    sub("+ обидва", prim_all + rec + meme)
    sub("лише квартали з ≤ 45 порожніми днями (усі, що увійшли, — тотожно первинному)", prim_all)
    sub("без скальпів", [u for u in prim_all if not lab(u).scalp])
    sub("без межових за 3.3", [u for u in prim_all if not lab(u).border])
    for y in (2020, 2021, 2022) if sample == "main" else (2025, 2026):
        sub(f"рік {y}", [u for u in prim_all if u.row.call.day.year == y])
    for sd_ in ("long", "short"):
        sub(f"сторона: {sd_}", [u for u in prim_all if u.side == sd_])
    strict = [u for u in prim_all if lab(u).ownership == "strict"]
    sub("вторинний: строго власні", strict)
    sub(f"строго власні + {SECONDARY_PLUS}", strict + [u for u in base if u.row.id == SECONDARY_PLUS])
    trl = {u.uid: run_unit(u, S, style="limit") for u in prim}
    sub("стиль `limit` (вхід на ближньому краї зони)", prim, {k: value(t) for k, t in trl.items()})
    r_vals = {u.uid: (per_call_r([trades[u.uid]]) or [None])[0] for u in prim if u.row.call.stop is not None}
    s_r = stats([u for u in prim if u.uid in r_vals], r_vals, filled)
    emit(f"| R net на заклик (лише рядки зі стопом; одиниці R) | {s_r['n']} | {s_r.get('filled')} | "
         + ("не визначено |  — | — |" if s_r.get("too_few") else f"{s_r['mean']:+.3f} R | [{s_r['lo']:+.3f}; {s_r['hi']:+.3f}] ({s_r['weeks']}) | — |"))  # fmt: skip
    # survivorship: no_data = stop; break-even (p. 2)
    p1_by_q: dict[str, list[float]] = defaultdict(list)
    for u in prim:
        p1_by_q[quarter(u.row.call.day)] += [x for x in mats["P1"][u.uid] if not math.isnan(x)]
    nd_vals, nd_note = {}, []
    for u in no_data:
        c = u.row.call
        ent = (c.zone_low + c.zone_high) / 2
        sane = c.stop is not None and (c.stop < ent if c.side == "long" else c.stop > ent)
        if sane:
            x = -abs(ent - c.stop) / ent - ce.COST_SPOT  # type: ignore[operator]
            nd_note.append(f"{c.id}: стоп {x:+.2%} (вхід {ent:g}, стоп {c.stop:g})")
        else:
            x = ce.quantile(p1_by_q.get(quarter(c.day), []), 0.10)
            nd_note.append(f"{c.id}: 10-й перцентиль П1 кварталу {quarter(c.day)} {_pct(x)}")
        nd_vals[u.uid] = x
    if no_data:
        sub("no_data = стоп (рядки без даних як втрата)", prim + no_data, vals | nd_vals, with_p=False)
    short_ev = [u for u in prim if u.status == "short"]
    if short_ev:  # exploratory, added after the first full run: the 'short' row (STORM) is the single top contributor
        emit()
        emit("**Розвідувально (додано після першого повного прогону, не реєстрація):** без рядка `short` "
             f"({', '.join(u.row.id for u in short_ev)}), який оцінено за правилом п. 2 і який є найбільшим внеском.")  # fmt: skip
        emit()
        emit(head)
        emit("|---|---|---|---|---|---|")
        drop_ids = {u.uid for u in short_ev}
        for name, us in (("первинний", prim_all), ("обов'язковий: межові за 3.3 у протилежній групі", [u for u in base if lab(u).border_variant]),
                         ("обов'язковий: без шаблону Q-20261118-2", [u for u in prim_all if not lab(u).q2_template]),
                         ("без межових за 3.3", [u for u in prim_all if not lab(u).border]), ("частка в BTC", [u for u in prim_all if quote_units(u.row.call.symbol) == "BTC"])):  # fmt: skip
            sub(f"{name} без {', '.join(u.row.id[3:] for u in short_ev)}", [u for u in us if u.uid not in drop_ids])
    # exploratory, added after review B 33в (findings 2-3), not the registration: (i) a limit / stop order fills only
    # when the price passes through its price (low < buy limit, high > sell limit; stop orders likewise), not on a touch;
    # (ii) */BTC costs at least one price step per side, the step taken as 1 satoshi (a lower bound of Binance's tick).
    # No p: the placebo matrices use the registered rule and costs.
    ev_vals = {u.uid: vals[u.uid] for u in units if vals[u.uid] is not None}
    strict_tr = {k: run_unit(u, S, plan=strict_fill_plan(u, S)) for u in units if (k := u.uid) in ev_vals}
    strict_vals = {k: value(t) for k, t in strict_tr.items()}
    changed = [k for k, t in strict_tr.items() if t.status != trades[k].status]
    tick_vals, steps = {}, []
    for u in units:
        t = trades[u.uid]
        if u.uid not in ev_vals:
            continue
        if t.filled and quote_units(u.row.call.symbol) == "BTC" and t.fill:
            step = SAT / t.fill
            if step > 0.01:
                steps.append(f"{u.row.id[3:]} {t.fill / SAT:.0f} сат ({step:.1%})")
            if 2 * step > u.cost:
                tick_vals[u.uid] = value(run_unit(u, S, cost=2 * step))
                continue
        tick_vals[u.uid] = ev_vals[u.uid]
    emit()
    emit("**Розвідувально (додано після рев'ю B 33в, не реєстрація):** (i) лімітний / стоп-ордер заповнюється лише, якщо "
         "ціна пройшла крізь ціну ордера (не дотик); (ii) витрати на `*/BTC` ≥ 1 крок ціни (1 сатоші) на бік. p не "
         "наводиться: плацебо пораховано за зареєстрованим правилом і витратами.")  # fmt: skip
    emit()
    emit(f"Рядки, де (i) змінює статус: {', '.join(k[3:] for k in changed) or '—'}. Заповнені `*/BTC` з кроком > 1 % ціни "
         f"входу: {', '.join(steps) or '—'}.")  # fmt: skip
    emit()
    emit(head)
    emit("|---|---|---|---|---|---|")
    border_v = [u for u in base if lab(u).border_variant]
    no_q2 = [u for u in prim_all if not lab(u).q2_template]
    no_border = [u for u in prim_all if not lab(u).border]
    strict_filled = {k for k, t in strict_tr.items() if t.filled and strict_vals[k] is not None}
    for tag, vv, ff in (("(i) заповнення крізь ціну", strict_vals, strict_filled), ("(ii) витрати ≥ 1 крок", tick_vals, filled)):
        for name, us in (("первинний", prim_all), ("обов'язковий: межові за 3.3 у протилежній групі", border_v),
                         ("обов'язковий: без шаблону Q-20261118-2", no_q2), ("без межових за 3.3", no_border)):  # fmt: skip
            emit(line(f"{tag}: {name}", stats(us, vv, ff), None))
    emit()
    emit("### Виживання (рядки no_data)")
    emit()
    if no_data:
        k = len(no_data)
        emit(f"- k = {k}: " + "; ".join(nd_note))
        emit(f"- точка беззбитковості (1) середнє: x = n · mean / k = {st['n']} · {st['mean']:+.4%} / {k} = "
             f"{_pct(ce.breakeven_mean(st['n'], st['mean'], k))} на рядок")  # fmt: skip
        be = ce.breakeven_ci_low(st["values"], [u.week for u in st["units"]], [u.week for u in no_data],
                                 ci=lambda vv, cc: cluster_bootstrap_ci(vv, cc, n_boot=N_BOOT, seed=SEED_FULL))  # fmt: skip
        emit(f"- точка беззбитковості (2) нижня межа CI: x = {'> 100 %' if be is None else f'{be:.1%}'} на рядок (крок 0.1 п. п.)")
    else:
        emit("- рядків no_data нема")
    emit()
    emit("### Бенчмарки й внесок геометрії (довідково)")
    emit()
    bh = {}
    for u in prim:
        t = run_unit(u, S, plan=Plan(u.side, "market", None, None, None))
        bh[u.uid] = None if t.ret_net is None else vals[u.uid] - t.ret_net  # type: ignore[operator]
    emit(line("внесок геометрії: факт − ринкове утримання від t0 (7 д)", stats(prim, bh, filled)))
    gf = {}
    for u in prim:
        t = trades[u.uid]
        if t.filled and t.fill_t is not None:
            b2 = run_unit(u, S, start=t.fill_t, plan=Plan(u.side, "market", None, None, None))
            gf[u.uid] = None if b2.ret_net is None else vals[u.uid] - b2.ret_net  # type: ignore[operator]
    emit(line("внесок геометрії лише на заповнених (від заповнення, 7 д)", stats([u for u in prim if u.uid in gf], gf, filled)))
    dm = statistics.fmean(drift[u.uid] for u in prim if not math.isnan(drift[u.uid]))
    emit(f"\nСередній локальний дрейф D тієї ж сторони: {_pct(dm)} на 7 д (після витрат)")
    emit()
    emit("### За ринками (5a)")
    emit()
    emit("| Ринок | n | E[дохід] net | 95 % CI (кластерів) |")
    emit("|---|---|---|---|")
    by_m: dict[str, list[Unit]] = defaultdict(list)
    for u in prim:
        by_m[u.market].append(u)
    means, ses = [], []
    for m, us in sorted(by_m.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        xs = [vals[u.uid] for u in us]
        if len(us) >= 5:
            s = stats(us, vals)
            means.append(s["mean"])
            ses.append(max(1e-9, statistics.stdev(xs) / math.sqrt(len(xs))))  # type: ignore[arg-type]
            emit(f"| {m} | {len(us)} | {_pct(s['mean'])} | [{_pct(s['lo'])}; {_pct(s['hi'])}] ({s['weeks']}) |")
        else:
            emit(f"| {m} | {len(us)} | {_pct(statistics.fmean(xs))} | — |")  # type: ignore[arg-type]
    if len(means) >= 2:
        q, df, i2 = cochran_q(means, ses)
        labels = [u.market if len(by_m[u.market]) >= 5 else "other" for u in st["units"]]
        pw = ce.label_permutation_p_within(st["values"], labels, [u.week for u in st["units"]], n=2000, seed=SEED_FULL)
        emit(f"\nCochran Q = {q:.2f} (df {df}), I² = {i2:.0%}; перестановка міток ринків у межах тижня (2000): p = {pw:.3f}")
    else:
        emit(f"\nринків з n ≥ 5: {len(means)} — тест неоднорідності не визначено")
    both = [m for m, us in by_m.items() if sum(u.t0 < split for u in us) >= 5 and sum(u.t0 >= split for u in us) >= 5]
    emit(f"Стійкість рангів між половинами: ринків з ≥ 5 рядками в обох половинах — {len(both)} "
         + ("(< 4: тест не має сенсу)" if len(both) < 4 else f"({', '.join(sorted(both))})"))  # fmt: skip
    emit()
    emit("### Рядки")
    emit()
    emit("| id | ринок | ордер | статус | заповнення | вихід | дохід net | D | факт − D |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for u in sorted(units, key=lambda u: (u.t0, u.uid)):
        t, x = trades[u.uid], vals[u.uid]
        sets = "P" if u in prim_all else ("conf" if u.confirmed else "var")
        w = _window(S, u.market, ce.first_hour_after(u.t0)) if u.market in S else []
        kind = unit_plan(u, w[0].open).kind if w else "—"
        d = drift.get(u.uid, NAN)
        emit(f"| {u.uid} [{sets}] | {u.market} | {kind} | {t.status} | {'—' if t.fill is None else f'{t.fill:.6g}'} | "
             f"{'—' if t.exit is None else f'{t.exit:.6g}'} | {_pct(x)} | {_pct(d)} | {_pct(NAN if x is None else x - d)} |")  # fmt: skip
    emit()
    return {"mark": v.mark, "reason": v.reason, "p": p["P1"], "p2": p["P2"], "n": st["n"], "filled": st["filled"],
            "mean": st["mean"], "lo": st["lo"], "hi": st["hi"], "weeks": st["weeks"], "excess_lo": excess["lo"]}  # fmt: skip


def stage_full(rows: list[Row], cache: Path, smoke: bool) -> None:
    now_ms = int(time.time() * 1000) // D * D - H
    sel = bar_rows(rows)
    extra = [r for r in rows if r.id == SECONDARY_PLUS]
    perms = PERMS
    if smoke:  # synthetic bars, 3 placebo sets: exercises every path, computes no real outcome
        raw = synthetic_walk(sel + extra, now_ms)
        perms = 3
    else:
        raw = asyncio.run(load_markets(sel, cache))
    src = {k: m for k, (m, _) in raw.items() if raw[k][1]}
    s0 = {} if smoke else stage0_markets(cache)
    diff = [i for i, m in s0.items() if any(r.id == i for r in sel) and src.get(next(r.key for r in sel if r.id == i), "—") != m]
    print(f"markets fixed by stage 0: {len(s0)} rows read; differing now: {len(diff)} {diff[:5]}", flush=True)
    if diff:  # p. 2: the market is the one stage 0 printed (review B 33в, finding 5)
        raise SystemExit(f"markets differ from stage 0 for {len(diff)} rows: {diff[:10]}")
    need = NEED_H * H
    avail = {}
    for r in sel + extra:
        bars = raw.get(r.key, ("—", []))[1]
        avail[r.id] = availability(bars, r.lab.t0_ms, need)
        if r.id in CONFIRMED_ENTRY:
            avail[r.id + "@confirmed"] = availability(bars, confirmed_t0(r.id), need)
    if not smoke:
        use: dict[str, set[str]] = defaultdict(set)
        for r in sel + extra:
            if r.key in src and avail.get(r.id) != "missing":
                use[src[r.key]].add(r.sample)
        asyncio.run(extend_for_pools(raw, use, cache, now_ms))
    S: Series = {m: _series(b) for m, b in raw.values() if b}
    funding: dict[str, list[tuple[int, float]]] = {}
    if not smoke:
        spans: dict[str, tuple[int, int]] = {}
        for r in sel + extra:
            m = src.get(r.key, "")
            if m.startswith("binanceusdm:") and avail.get(r.id) != "missing":
                lo, hi = spans.get(m, (r.lab.t0_ms, r.lab.t0_ms))
                spans[m] = (min(lo, r.lab.t0_ms // D * D - D), max(hi, r.lab.t0_ms // D * D + 16 * D))
        funding = asyncio.run(load_funding_bc(sorted(spans), spans, cache))
    lines: list[str] = []

    def emit(s: str = "") -> None:
        lines.append(s)

    res = {s: evaluate(s, rows, S, src, avail, now_ms, perms, funding, emit) for s in SAMPLES}
    m = res["main"]
    pv = [p for _, p in HOLM_KNOWN] + [m["p"] if m["mark"] != "◯" else 1.0, 1.0]
    rej = holm(pv)
    c = res["combo"]
    if m["mark"] == "✓":
        final = (
            "✓ «у 2020–2022, поточний стиль не суперечить»" if c["n"] >= 10 and c["mean"] > 0 else "≈ «не відтворилось у поточному стилі»"
        )
    elif m["mark"] == "≈":
        final = "≈ (не вище ≈ за правилом поєднання)"
    else:
        final = f"{m['mark']} (поєднання не змінює)"
    head = [
        f"# btc_charlie: повний прогін за реєстрацією 3.3в, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"Холм (m = 4; KillaXBT {pv[0]}, Trader_XO {pv[1]}, btc_charlie {pv[2]:.4f}, docXBT поки 1.0): btc_charlie відхиляє H0 — "
        f"{'так' if rej[2] else 'ні'}. Підсумок за правилом поєднання 3.3 (б): {final}. Поєднання 2025-Q4 … 2026-Q3: {c['mark']} "
        f"(n {c['n']}, середнє {_pct(c['mean'])}, p(П1) {c['p']:.4f}).",
        "",
        "Період первинної — у 2020–2022 (фактично 2020-Q1 … Q3: 54 з 58 рядків).",
        "",
    ]  # fmt: skip
    text = "\n".join(head + lines) + "\n"
    out = cache / ("smoke_full.md" if smoke else "full_report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    if smoke:
        print(f"smoke ok (synthetic bars, {perms} placebo sets): {len(lines)} report lines -> {out}; peak rss {_rss_mb():.0f} MB")
        return
    print("\n".join(head))
    for s, r in res.items():
        print(f"{s}: {r['mark']} n={r['n']} filled={r.get('filled')} mean={_pct(r['mean'])} CI=[{_pct(r.get('lo', NAN))}; "
              f"{_pct(r.get('hi', NAN))}] weeks={r.get('weeks')} p1={r['p']:.4f} p2={r.get('p2', NAN):.4f}")  # fmt: skip
    print(f"report -> {out}; peak rss {_rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("markup", "zero", "full"), required=True)
    ap.add_argument("--bars", action="store_true", help="stage zero: check 1h candle availability (network)")
    ap.add_argument("--smoke", action="store_true", help="stage zero / full: synthetic bars (no network, no real outcome)")
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--out", type=Path, help="also write the whole output to this file (outside the repo)")
    a = ap.parse_args()
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with a.out.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(_Tee(sys.stdout, fh)):
            run(a)
        print(f"output written to {a.out}")
    else:
        run(a)


class _Tee(io.TextIOBase):
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
            st.flush()
        return len(s)


def run(a: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING)
    rows, errors = load_rows()
    print(f"rows {len(rows)}; refused / untagged {len(errors)}")
    for e in errors:
        print("  ", e)
    outside = [r.id for r in rows if not r.sample]
    print("rows outside both samples (not evaluated):", len(outside))
    rows = [r for r in rows if r.sample]
    if a.stage == "markup":
        stage_markup(rows)
    if a.stage == "full":
        stage_full(rows, a.cache_dir, a.smoke)
        return
    counters(rows)
    if a.stage == "zero" and (a.bars or a.smoke):
        stage_bars(rows, a.cache_dir, a.smoke)


if __name__ == "__main__":
    main()
