"""Post-listing mid-age decay short (age 30 -> 90 days vs a mature-alt basket): step 0, the power gate.

Idea card: docs/ideas/IDEA-20261025-post-listing-midage-decay-short.md ("Попередня реєстрація").

Offline: reads the caches written by ``verify_listing_drift.py`` (events, first-trade times, market-age evidence on
other venues) and ``verify_funding_crowding`` (full monthly daily bars of every eligible Binance USDT-M perp up to
2026-09). No network, no keys, no orders.

Step 0 prints n, the funnel, month clusters, SD of the hedged price result, ICC, n_eff and the MDE, and the gate.
It never computes or prints a mean of the outcome. The registered test (H1-H4, placebo, grid) runs only if the gate
passes; on 2026-10-28 it did not, so that stage is deliberately not written.

    uv run python scripts/verify_post_listing_midage.py --out ~/.cache/tradedesk-agents/post_listing_midage/gate.json
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_listing_drift as ld  # noqa: E402  (events, market-age evidence)

from tradedesk.analytics.listing_drift import (  # noqa: E402
    DAY_MS,
    DailyBars,
    basket_members,
    bench_return,
    entry_day,
    short_leg,
    symbol_eligible,
)
from tradedesk.analytics.post_listing_midage import (  # noqa: E402
    ENTRY_AGE,
    GATE_MDE,
    GATE_N,
    HOLD_DAYS,
    MIN_TURNOVER,
    Gate,
    median_turnover,
    power_gate,
    relative_short,
    window_days,
)

D_FIRST = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp()) // 86400
D_LAST = int(datetime(2026, 6, 15, tzinfo=UTC).timestamp()) // 86400
BTC = "BTCUSDT"


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load_fc(fc: Path, sym: str) -> DailyBars:
    rows: list[list[float]] = []
    for p in sorted((fc / "k1d" / sym).glob("*.json")):
        rows.extend(json.loads(p.read_text()) or [])
    return DailyBars.from_rows(rows)


def month_end_day(ym: str) -> int:
    y, m = map(int, ym.split("-"))
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return int(datetime(y2, m2, 1, tzinfo=UTC).timestamp()) // 86400 - 1


def load_universe(fc: Path) -> tuple[dict[str, DailyBars], dict[str, int]]:
    """Daily bars of every cached eligible symbol and its first day (conservative for pre-2021-10 listings)."""
    info = json.loads((fc / "exchange_info.json").read_text())["info"]
    bars: dict[str, DailyBars] = {}
    first: dict[str, int] = {}
    syms = sorted(p.name for p in (fc / "k1d").iterdir())
    for i, s in enumerate(syms):
        it = info.get(s) or {}
        if not symbol_eligible(s, it.get("contractType"), it.get("underlyingType")):
            continue
        b = load_fc(fc, s)
        if b.first is None:
            continue
        lp = fc / "listing" / f"{s}.json"
        months = json.loads(lp.read_text()) if lp.exists() else None
        fm = months[0] if months else "2021-10"
        bars[s] = b
        first[s] = b.first if fm >= "2021-10" else month_end_day(fm)
        if i % 200 == 0:
            print(f"  loaded {i}/{len(syms)} symbols, peak RSS {rss_mb():.0f} MB", flush=True)
    return bars, first


def fmt_gate(label: str, g: Gate) -> str:
    return (
        f"{label:<34} n={g.n:4d} months={g.clusters:3d} SD={g.sd * 100:6.1f}pp ICC={g.icc:+.3f} deff={g.deff:.2f} "
        f"n_eff={g.n_eff:6.1f} MDE_icc={g.mde_icc * 100:5.2f}pp MDE_cr1={g.mde_cr1 * 100:5.2f}pp "
        f"gate={'PASS' if g.passed else 'FAIL'}"
    )


def gate_dict(g: Gate) -> dict[str, Any]:
    return {
        "n": g.n,
        "months": g.clusters,
        "sd": g.sd,
        "icc": g.icc,
        "deff": g.deff,
        "n_eff": g.n_eff,
        "mde_icc": g.mde_icc,
        "mde_cr1": g.mde_cr1,
        "pass": g.passed,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path.home() / ".cache/tradedesk-agents"
    ap.add_argument("--ld-cache", type=Path, default=base / "listing_drift")
    ap.add_argument("--fc-cache", type=Path, default=base / "funding_crowding")
    ap.add_argument("--out", type=Path, default=base / "post_listing_midage" / "gate.json")
    a = ap.parse_args()
    ldc, fc, out = a.ld_cache.expanduser(), a.fc_cache.expanduser(), a.out.expanduser()

    events, _ = ld.build_events(ldc)
    print(f"events from listing_drift cache (one per asset, t0 2022-01..2026-09-15): {len(events)}", flush=True)
    bars, first = load_universe(fc)
    last_day = max(b.last or 0 for b in bars.values())
    print(f"universe: {len(bars)} eligible symbols with daily bars, last day {ymd(last_day)}, peak RSS {rss_mb():.0f} MB")

    # consistency check: step 0 of new-perp-listing-drift (14 d, 1x, vs BTC, n 541, SD 42.9 pp)
    btc = bars[BTC]
    chk = []
    for e in events:
        E = entry_day(e["t0"])
        if E + 13 > ld.LAST_DAY or e["symbol"] not in bars:
            continue
        leg = short_leg(bars[e["symbol"]], E, 14)
        rb = bench_return(btc, E, leg.exit) if leg else None
        if leg is not None and rb is not None:
            chk.append(leg.pnl + rb)
    print(
        f"consistency: 14 d 1x vs BTC on funding_crowding bars: n={len(chk)} SD={statistics.stdev(chk) * 100:.1f}pp (report: 541, 42.9pp)"
    )

    funnel: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    rel1: list[float] = []
    rel2: list[float] = []
    relb: list[float] = []
    raw: list[float] = []
    rej: Counter[str] = Counter()
    qv_all = {k: v.qv for k, v in bars.items()}
    for e in events:
        s = e["symbol"]
        d0 = e["t0"] // DAY_MS
        if not (D_FIRST <= d0 <= D_LAST):
            funnel["d0 outside 2022-01-01..2026-06-15"] += 1
            continue
        if s not in bars or bars[s].first != d0:
            funnel["no daily bars / first-day mismatch"] += 1
            continue
        E, X = window_days(d0)
        if X > last_day:
            funnel["window not completed"] += 1
            continue
        b = bars[s]
        if E not in b.open:
            funnel["no bar at age 30 (delisted/halted)"] += 1
            continue
        to = median_turnover(b.qv, E)
        if to is None or to < MIN_TURNOVER:
            funnel["median turnover < 5M USD (or < 20 days)"] += 1
            continue
        mem = basket_members(E, first, qv_all, exclude=(BTC, s))
        r1 = relative_short(b, mem, bars, E, HOLD_DAYS)
        r2 = relative_short(b, mem, bars, E, HOLD_DAYS, leverage=2.0)
        if r1 is None or r2 is None:
            funnel["no basket / no leg"] += 1
            continue
        rbtc = bench_return(btc, E, r1.exit)
        age = ld.age_evidence(ldc, e, ld.load_daily(ldc, s))
        for x in age["rejected"]:
            rej[x.split()[0]] += 1
        funnel["IN"] += 1
        gaps = sum(1 for d in range(E, X + 1) if d not in b.close)
        rows.append(
            {
                "symbol": s,
                "d0": ymd(d0),
                "month": ymd(d0)[:7],
                "year": int(ymd(d0)[:4]),
                "stratum": "new" if age["new"] else "old",
                "market_age_days": age["age_days"],
                "turnover_med": round(to),
                "basket_n": len(mem),
                "truncated": r1.truncated,
                "gap_days": gaps,
            }
        )
        rel1.append(r1.rel)
        rel2.append(r2.rel)
        relb.append(r1.short_pnl + rbtc if rbtc is not None else float("nan"))
        raw.append(-r1.short_pnl)
    print(f"funnel: {dict(funnel)}; peak RSS {rss_mb():.0f} MB")
    print(f"price-level rejections of other-venue age evidence (ticker collisions): {dict(rej)}")

    months = [r["month"] for r in rows]
    by_year = Counter(r["year"] for r in rows)
    strata = Counter(r["stratum"] for r in rows)
    sizes = sorted(Counter(months).values(), reverse=True)
    print(f"by year: {dict(sorted(by_year.items()))}; strata: {dict(strata)}")
    print(f"month clusters: {len(sizes)}; largest sizes {sizes[:8]}; median size {statistics.median(sizes) if sizes else 0}")
    print(
        f"data checks: truncated (delisted inside window) {sum(r['truncated'] for r in rows)}; "
        f"events with missing days inside window {sum(r['gap_days'] > 0 for r in rows)}; "
        f"basket size min {min((r['basket_n'] for r in rows), default=0)}"
    )

    g = power_gate(rel1, months)
    print(f"\nGATE (registered: n >= {GATE_N} and both MDE <= {GATE_MDE * 100:.0f} pp; SD of the hedged price result, no means)")
    print(fmt_gate("primary: all, basket hedge, 1x", g))
    extra: dict[str, Gate] = {}
    new_idx = [i for i, r in enumerate(rows) if r["stratum"] == "new"]
    extra["new-only (info)"] = power_gate([rel1[i] for i in new_idx], [months[i] for i in new_idx])
    extra["2x liquidation (info)"] = power_gate(rel2, months)
    ok_b = [i for i, x in enumerate(relb) if x == x]
    extra["BTC hedge (info)"] = power_gate([relb[i] for i in ok_b], [months[i] for i in ok_b])
    extra["unhedged coin return (info)"] = power_gate(raw, months)
    for k, v in extra.items():
        print(fmt_gate(k, v))
    if g.n > 1:
        need = (2.8 * g.sd / GATE_MDE) ** 2 * g.deff
        print(f"events needed for MDE {GATE_MDE * 100:.0f} pp at this SD and deff: {need:.0f}")
    print(
        f"\nVERDICT step 0: gate {'PASSED' if g.passed else 'FAILED'} -> {'run H1-H4 as registered' if g.passed else 'inconclusive, forward; means not computed'}"
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "card": "IDEA-20261025-post-listing-midage-decay-short",
                "entry_age": ENTRY_AGE,
                "hold": HOLD_DAYS,
                "funnel": dict(funnel),
                "gate": gate_dict(g),
                "info": {k: gate_dict(v) for k, v in extra.items()},
                "events": rows,  # metadata only: no outcome values
            },
            indent=1,
        )
    )
    print(f"written {out}; peak RSS {rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
