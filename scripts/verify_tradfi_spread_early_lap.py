"""Early kill (b) of IDEA-20261024-tradfi-perp-offhours-reopen on the forward spread collection (round-trip costs).

    uv run python scripts/verify_tradfi_spread_early_lap.py --stage smoke [--out FILE]   # coverage and sanity only
    uv run python scripts/verify_tradfi_spread_early_lap.py --stage run [--out FILE]     # registered run

Reads ``tradfi_spread`` (schema 29, collector/tradfi_spread.py) over a read-only connection (``mode=ro``: no migration,
no write), no network, no keys, nothing from ``execution/``. Only the rows inside the as-of windows (5 min up to
09:25 ET and up to 10:30 ET of every NYSE session) are loaded, so memory stays small.

``smoke`` prints coverage (names with a valid quote at both times per session) and sanity counts (NULL / crossed
books, mark vs mid, venue book age); it never prints a spread or a cost. ``run`` applies the rule registered in the card
(«Попередня реєстрація», early kill, 2026-11-20) with the pure ``tradedesk.analytics.tradfi_spread.early_kill``:
per name of the frozen G3 set, the median of cost = taker 0.09 % + full spread at 09:25 ET + full spread at 10:30 ET
over the covered sessions; the card is rejected when that median is >= 0.20 % on >= 70 % of the evaluable names.
Variants after the primary line are descriptive (labelled), never the decision.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)

from tradedesk.analytics import tradfi_spread as ts
from tradedesk.analytics.tradfi_offhours import MIN_MS, nyse_sessions

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_OUT_DIR = Path.home() / ".cache" / "tradedesk-agents" / "tradfi_spread_early_lap"
REGISTERED_START = date(2026, 10, 26)  # first NYSE session after the collector was switched on (24.10, a Saturday)
REGISTERED_END = date(2026, 11, 20)  # the 20th NYSE session of the collection
REGISTERED_CUTOFF = "2026-11-20T16:00"  # UTC, exclusive; after 10:30 ET (15:30 UTC) of the last session

# Frozen G3 set of the step-0 report (docs/verification/2026-10-24-tradfi-perp-offhours-reopen.md): US equity/ETF
# TradFi perps with median daily volume >= $1M and >= 60 nights, from tradfi_offhours_reopen/out/stage0_per_symbol.json.
G3 = tuple(
    f"{s}USDT"
    for s in (
        "AAOI AAPL ALAB AMAT AMD AMZN APP ARM ASML ASTS AVGO AXTI BABA BBX BE BMNR BNC CBRS COHR COIN CRCL CRDO CRWD "
        "CRWV DELL DRAM EWY FLNC GLW GOOGL HIMS HOOD HPE IBM INTC INTW IREN KORU KSTR LITE LLY LRCX META MRVL MSFT "
        "MSTR MU MUU MVLL NBIS NFLX NOK NOW NVDA ORCL PLTR QCOM QNTX QQQ RKLB SHAZ SKHY SMCI SNDK SNXX SOXL SOXS SPCX "
        "SPY SQQQ STRC STXX TQQQ TSLA TSM USAR UVXY WDC"
    ).split()
)
# Exploratory variant only (review of 2026-10-24, item 6): known leveraged / inverse / volatility ETFs in the G3 set.
LEVERAGED = frozenset(f"{s}USDT" for s in ("KORU", "MUU", "SOXL", "SOXS", "SQQQ", "TQQQ", "UVXY"))
MARK_DEV = 0.02  # |mark / mid - 1| above this is flagged (ticker collision / units)


def parse_cutoff(s: str) -> int:
    """UTC ``YYYY-MM-DDTHH:MM`` as epoch ms."""
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def registered_sessions(start: date, end: date, cutoff_ms: int) -> list[Any]:
    """NYSE sessions in [start, end] whose 10:30 ET exit is before the cutoff."""
    return [s for s in nyse_sessions(start, end) if s.open_ms + ts.EXIT_AFTER_OPEN_MIN * MIN_MS < cutoff_ms]


def window_times(sessions: list[Any]) -> list[int]:
    """Entry (09:25 ET) and exit (10:30 ET) times in epoch ms, two per session."""
    out = []
    for s in sessions:
        out += [s.open_ms - ts.ENTRY_BEFORE_OPEN_MIN * MIN_MS, s.open_ms + ts.EXIT_AFTER_OPEN_MIN * MIN_MS]
    return out


def load_quotes(
    con: sqlite3.Connection, times_ms: list[int], names: set[str], cutoff_ms: int, *, max_stale_ms: int = ts.MAX_STALE_MS
) -> tuple[dict[str, list[tuple[int, float | None, float | None]]], dict[str, int]]:
    """Quotes ``(ts * 1000, bid, ask)`` of ``names`` inside each as-of window [t - max_stale, t] (and before the cutoff),
    sorted by time per symbol, plus sanity counts over the loaded rows of those names."""
    q: dict[str, set[tuple[int, float | None, float | None]]] = {}
    sanity = {"rows": 0, "null_side": 0, "crossed": 0, "mark_dev": 0, "book_age_gt5m": 0}
    for t in times_ms:
        hi = min(t, cutoff_ms - 1) // 1000
        lo = -(-(t - max_stale_ms) // 1000)  # ceil: a row older than max_stale is never needed
        cur = con.execute("SELECT ts, symbol, bid, ask, mark_price, book_ms FROM tradfi_spread WHERE ts >= ? AND ts <= ?", (lo, hi))
        for t_s, sym, bid, ask, mark, book_ms in cur:
            if sym not in names:
                continue
            sanity["rows"] += 1
            if bid is None or ask is None:
                sanity["null_side"] += 1
            elif ask < bid:
                sanity["crossed"] += 1
            elif mark and bid > 0 and abs(mark / ((bid + ask) / 2) - 1) > MARK_DEV:
                sanity["mark_dev"] += 1
            if book_ms is not None and t_s * 1000 - book_ms > ts.MAX_STALE_MS:
                sanity["book_age_gt5m"] += 1
            q.setdefault(sym, set()).add((int(t_s) * 1000, bid, ask))
    return {s: sorted(v, key=lambda r: r[0]) for s, v in q.items()}, sanity


def cost_rows(
    quotes: dict[str, list[tuple[int, float | None, float | None]]], names: list[str], sessions: list[Any]
) -> dict[str, dict[date, dict[str, Any]]]:
    """name -> {session day: session_costs row}; a name without quotes gets rows with every field None."""
    if not sessions:
        return {n: {} for n in names}
    a, b = sessions[0].day, sessions[-1].day
    keep = {s.day for s in sessions}
    return {n: {r["day"]: r for r in ts.session_costs(quotes.get(n, []), a, b) if r["day"] in keep} for n in names}


def costs_of(rows: dict[str, dict[date, dict[str, Any]]], *, half: bool = False) -> dict[str, dict[date, float | None]]:
    """Per-name, per-day cost; ``half`` = fee + half spread per leg (descriptive variant, a taker against the mid)."""
    out: dict[str, dict[date, float | None]] = {}
    for n, by in rows.items():
        out[n] = {}
        for d, r in by.items():
            if r["cost"] is None:
                out[n][d] = None
            else:
                out[n][d] = ts.ROUND_TRIP_FEE + (r["entry_spread"] + r["exit_spread"]) / 2 if half else r["cost"]
    return out


def pct(x: float | None, k: int = 3) -> str:
    return "n/a" if x is None else f"{x * 100:.{k}f}%"


def smoke(rows: dict[str, dict[date, dict[str, Any]]], sessions: list[Any], sanity: dict[str, int]) -> tuple[list[str], dict]:
    """Coverage only: per session, how many names have both quotes; no spread, no cost."""
    lines = [f"sessions registered: {len(sessions)} ({sessions[0].day} .. {sessions[-1].day})" if sessions else "sessions: 0"]
    per_day = {s.day: sum(1 for by in rows.values() if by.get(s.day, {}).get("cost") is not None) for s in sessions}
    for d, k in per_day.items():
        lines.append(f"  {d}  names with both quotes {k:>3}/{len(rows)}")
    seen = sorted(n for n, by in rows.items() if any(r["cost"] is not None for r in by.values()))
    never = sorted(set(rows) - set(seen))
    lines.append(f"names with at least one observed session: {len(seen)}/{len(rows)}; never observed: {never}")
    lines.append(f"sanity over loaded window rows: {sanity}")
    return lines, {"per_day": {str(d): k for d, k in per_day.items()}, "never": never, "sanity": sanity}


def _quant(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    qs = statistics.quantiles(xs, n=4) if len(xs) >= 2 else [xs[0]] * 3
    return f"min {pct(min(xs))} q25 {pct(qs[0])} q50 {pct(qs[1])} q75 {pct(qs[2])} max {pct(max(xs))}"


def summarize(label: str, r: dict[str, Any]) -> str:
    return (
        f"[{label}] covered sessions {len(r['covered_sessions'])}, evaluable {r['n_evaluable']}/{r['n_names']}, "
        f"median >= 0.20%: {r['n_fail']} ({pct(r['share_fail'], 1)}), median <= 0.15%: {r['n_cheap']} "
        f"({pct(r['share_cheap'], 1)}), median of medians {pct(r['median_of_medians'])} -> {r['decision']}"
    )


def run(rows: dict[str, dict[date, dict[str, Any]]], sessions: list[Any]) -> tuple[list[str], dict]:
    days = [s.day for s in sessions]
    costs = costs_of(rows)
    prim = ts.early_kill(costs, days)
    cov = prim["covered_sessions"]
    lines = ["PRIMARY (registered): " + summarize("G3 full spread", prim)]
    lines.append(f"  not covered sessions: {sorted(set(days) - set(cov))}")
    lines.append(f"  not evaluable names: {sorted(n for n, p in prim['per_name'].items() if not p['evaluable'])}")
    ev_med = [p["median"] for p in prim["per_name"].values() if p["evaluable"]]
    lines.append(f"  per-name medians: {_quant(ev_med)}")
    all78 = prim["n_fail"] / prim["n_names"] if prim["n_names"] else None
    lines.append(
        f"  [descriptive] share failing with all {prim['n_names']} names as denominator (non-evaluable = not failing): {pct(all78, 1)}"
    )

    # entry / exit spreads separately (medians over names of per-name medians, covered sessions only)
    ent, ext = [], []
    for n, p in prim["per_name"].items():
        if p["evaluable"]:
            e = [rows[n][d]["entry_spread"] for d in cov if rows[n].get(d, {}).get("cost") is not None]
            x = [rows[n][d]["exit_spread"] for d in cov if rows[n].get(d, {}).get("cost") is not None]
            ent.append(statistics.median(e))
            ext.append(statistics.median(x))
    lines.append(f"  [descriptive] full spread at 09:25 ET: {_quant(ent)}")
    lines.append(f"  [descriptive] full spread at 10:30 ET: {_quant(ext)}")

    out: dict[str, Any] = {"primary": _jsonable(prim)}
    half = ts.early_kill(costs_of(rows, half=True), days)
    lines.append(summarize("descriptive: fee + half spread per leg", half))
    out["half_spread"] = _jsonable(half)
    nolev = ts.early_kill({n: c for n, c in costs.items() if n not in LEVERAGED}, days)
    lines.append(summarize(f"exploratory: G3 without {len(LEVERAGED)} leveraged/inverse ETFs", nolev))
    out["no_leveraged"] = _jsonable(nolev)

    # time split over the covered sessions (descriptive; no minimum session count)
    k = len(cov) // 2
    halves = (cov[:k], cov[k:])
    res_h = []
    for i, part in enumerate(halves, 1):
        sub = {n: {d: c.get(d) for d in part} for n, c in costs.items()}
        r = ts.early_kill(sub, part, min_sessions=0)
        res_h.append(r)
        lines.append(summarize(f"descriptive: half {i} ({part[0] if part else '-'} .. {part[-1] if part else '-'})", r))
    # persistence: are the cheap names in half 1 still cheap in half 2?
    m1 = {n: p["median"] for n, p in res_h[0]["per_name"].items() if p["evaluable"]}
    m2 = {n: p["median"] for n, p in res_h[1]["per_name"].items() if p["evaluable"]}
    both = sorted(set(m1) & set(m2), key=lambda n: m1[n])
    if len(both) >= 8:
        q = max(1, len(both) // 4)
        top, rest = both[:q], both[q:]
        lines.append(
            f"  [persistence] cheapest quarter in half 1 ({q} names): half-2 median of medians {pct(statistics.median([m2[n] for n in top]))} "
            f"vs rest {pct(statistics.median([m2[n] for n in rest]))}; Spearman(half1, half2) {_spearman([m1[n] for n in both], [m2[n] for n in both]):.2f}"
        )

    lines.append("per-name (evaluable, sorted by median): name n median")
    for n, p in sorted(prim["per_name"].items(), key=lambda kv: (kv[1]["median"] is None, kv[1]["median"] or 0)):
        lines.append(f"  {n[:-4]:<6} {p['n']:>2} {pct(p['median'])}{'' if p['evaluable'] else '  (not evaluable)'}")
    return lines, out


def _spearman(a: list[float], b: list[float]) -> float:
    def rank(v: list[float]) -> list[float]:
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
                j += 1
            for t in range(i, j + 1):
                r[o[t]] = (i + j) / 2
            i = j + 1
        return r

    ra, rb = rank(a), rank(b)
    if len(set(ra)) < 2 or len(set(rb)) < 2:
        return float("nan")
    return statistics.correlation(ra, rb)


def _jsonable(r: dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    d["covered_sessions"] = [str(x) for x in r["covered_sessions"]]
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"collector SQLite, opened read-only (default {DEFAULT_DB})")
    ap.add_argument("--cutoff", default=REGISTERED_CUTOFF, help=f"UTC YYYY-MM-DDTHH:MM, exclusive (registered {REGISTERED_CUTOFF})")
    ap.add_argument("--out", help="also write the text and a JSON block to this file")
    a = ap.parse_args(argv)
    if a.stage == "run" and a.cutoff != REGISTERED_CUTOFF:
        print(f"run is registered with --cutoff {REGISTERED_CUTOFF}", file=sys.stderr)
        return 2
    cutoff_ms = parse_cutoff(a.cutoff)
    sessions = registered_sessions(REGISTERED_START, REGISTERED_END, cutoff_ms)
    con, why = cmn.open_ro(Path(a.db))
    if con is None:
        print(f"DB {a.db}: {why}", file=sys.stderr)
        return 1
    try:
        if not cmn.has_table(con, "tradfi_spread"):
            print("table tradfi_spread is missing", file=sys.stderr)
            return 1
        quotes, sanity = load_quotes(con, window_times(sessions), set(G3), cutoff_ms)
    finally:
        con.close()
    rows = cost_rows(quotes, list(G3), sessions)
    head = [f"tradfi_spread early lap, stage {a.stage}, cutoff {a.cutoff} UTC, G3 set {len(G3)} names, peak rss {cmn.rss_mb():.0f} MB"]
    lines, data = smoke(rows, sessions, sanity)
    if a.stage == "run":
        more, res = run(rows, sessions)
        lines += more
        data.update(res)
    text = "\n".join(head + lines)
    print(text)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n\nJSON\n" + json.dumps(data, default=str, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
