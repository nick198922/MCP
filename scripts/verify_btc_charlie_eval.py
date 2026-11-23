"""Evaluation of @btc_charlie's calls (card IDEA-20261113-x-traders, item 3.3 / 3.3б): markup and stage 0 only.

    uv run python scripts/verify_btc_charlie_eval.py --stage markup          # labels -> docs/calls/x/btc_charlie-eval.csv
    uv run python scripts/verify_btc_charlie_eval.py --stage zero            # counters from the markup, no network
    uv run python scripts/verify_btc_charlie_eval.py --stage zero --bars     # + 1h candle availability (loads candles,
                                                                             #   prints only availability; after the
                                                                             #   markup commit)
    uv run python scripts/verify_btc_charlie_eval.py --stage zero --smoke    # availability path on synthetic bars

Labels come only from the pre-registered tags in the CSV ``note`` (``tradedesk.analytics.call_labels``); this script never
decides a label. Stage 0 prints counters by group and candle availability; it prints no return, P&L or price outcome
(the only price it reads is the open at t0, compared with the call's zone to catch ticker / unit collisions). The full
run (metric, placebo, verdict) needs its own registration commit first (card, 3.3б).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import resource
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from tradedesk.analytics.call_labels import Labels, parse_tag, quote_units
from tradedesk.analytics.calls import Call, parse_calls
from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import week_of

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


def stage_bars(rows: list[Row], cache: Path, smoke: bool) -> None:
    sel = [r for r in rows if r.lab.group == "ex_ante" and r.lab.exclusion not in ("tradfi", "collision")]
    sel += [r for r in rows if r.lab.border_variant and r not in sel]
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("markup", "zero"), required=True)
    ap.add_argument("--bars", action="store_true", help="stage zero: check 1h candle availability (network)")
    ap.add_argument("--smoke", action="store_true", help="stage zero: availability on synthetic bars (no network)")
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
    counters(rows)
    if a.stage == "zero" and (a.bars or a.smoke):
        stage_bars(rows, a.cache_dir, a.smoke)


if __name__ == "__main__":
    main()
