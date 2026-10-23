"""Registered spot gate (step 0) for IDEA-20261022-catalyst-option-scalp: does fading the morning move predict the
BTC direction over the US-open window often enough (>= 55 %) to price daily options at all?

Data: Binance USD-M 5m klines already cached by scripts/verify_session_stop_hunt.py (data.binance.vision via
rt.binance_vision; one JSON per symbol-month, rows [open_ms, open, high, low, close]). This script reads the cache
only, no network, no keys. Pre-registration: the card's «Попередня реєстрація — крок 0». Option prints are loaded
only if the gate passes (separate registration).

    uv run python scripts/verify_catalyst_scalp.py --out ~/.cache/tradedesk-agents/catalyst_scalp/gate.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import resource
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tradedesk.analytics.catalyst_scalp import (
    DirectionEvent,
    direction_event,
    et_time_utc,
    gate_passes,
    hit_stats,
    is_trading_day,
    us_open_utc,
)

START, END = date(2021, 1, 4), date(2026, 9, 30)
OOS_FROM = date(2024, 1, 1)
H, K, THR = 4, 2, 0.01  # registered

# NYSE full-day closures 2021-2026 (nyse.com holiday calendars; incl. 2025-01-09 national day of mourning).
NYSE_HOLIDAYS = {
    date.fromisoformat(s)
    for s in """
2021-01-01 2021-01-18 2021-02-15 2021-04-02 2021-05-31 2021-07-05 2021-09-06 2021-11-25 2021-12-24
2022-01-17 2022-02-21 2022-04-15 2022-05-30 2022-06-20 2022-07-04 2022-09-05 2022-11-24 2022-12-26
2023-01-02 2023-01-16 2023-02-20 2023-04-07 2023-05-29 2023-06-19 2023-07-04 2023-09-04 2023-11-23 2023-12-25
2024-01-01 2024-01-15 2024-02-19 2024-03-29 2024-05-27 2024-06-19 2024-07-04 2024-09-02 2024-11-28 2024-12-25
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27
2025-12-25 2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07
""".split()
}

OUT: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    OUT.append(s)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load_bars(src: Path) -> dict[int, tuple[float, float]]:
    bars: dict[int, tuple[float, float]] = {}
    for f in sorted(src.glob("*.json")):
        for b in json.loads(f.read_text()) or []:
            bars[int(b[0]) // 1000] = (float(b[1]), float(b[4]))
    return bars


def days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def pz(hits: int, n: int) -> float:
    """Two-sided normal-approximation p-value of hits/n vs 50 %."""
    if not n:
        return math.nan
    z = (hits - n / 2) / math.sqrt(n / 4)
    return math.erfc(abs(z) / math.sqrt(2))


def row(label: str, evs: list[DirectionEvent]) -> str:
    s = hit_stats(evs)
    if not s.n:
        return f"{label:44s} n=   0"
    return (
        f"{label:44s} n={s.n:4d} hit={s.rate:6.1%} CI95=[{s.lo:5.1%};{s.hi:5.1%}] p={pz(s.hits, s.n):.3f} "
        f"meanR={s.mean_signed * 100:+.3f}% [{s.mean_lo * 100:+.3f};{s.mean_hi * 100:+.3f}]"
    )


def as_long(evs: list[DirectionEvent]) -> list[DirectionEvent]:
    return [DirectionEvent(e.event, e.entry, e.exit, e.morning, e.ret, 1, e.ret > 0) for e in evs]


def collect(bars, ds, h, k, thr, at=us_open_utc) -> tuple[list[DirectionEvent], int]:
    evs, missing = [], 0
    for d in ds:
        ev = direction_event(bars, at(d), h, k, thr)
        if ev is None:
            missing += 1
        else:
            evs.append(ev)
    return evs, missing


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k5m-dir", default="~/.cache/tradedesk-agents/session_stop_hunt/k5m")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/catalyst_scalp")
    ap.add_argument("--macro-csv", default="docs/verification/2026-10-15-us-macro-events.csv")
    ap.add_argument("--out", default="~/.cache/tradedesk-agents/catalyst_scalp/gate.txt")
    a = ap.parse_args()
    k5m = Path(a.k5m_dir).expanduser()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)

    src = k5m / "BTCUSDT"
    if not any(src.glob("*.json")):
        say(f"no 5m cache at {src}: run scripts/verify_session_stop_hunt.py --stage fetch first")
        return
    bars = load_bars(src)
    say(
        f"BTCUSDT 5m bars: {len(bars)}  {datetime.fromtimestamp(min(bars), UTC):%Y-%m-%d} .. "
        f"{datetime.fromtimestamp(max(bars), UTC):%Y-%m-%d %H:%M}  peak RSS {rss_mb():.0f} MB"
    )

    trade_days = [d for d in days(START, END) if is_trading_day(d, NYSE_HOLIDAYS)]
    plc_days = [d for d in days(START, END) if not is_trading_day(d, NYSE_HOLIDAYS)]

    # sanity: one known price level and DST clock
    t = int(datetime(2024, 3, 14, 13, 30, tzinfo=UTC).timestamp())
    say(
        f"sanity: BTC 2024-03-14 13:30 UTC open {bars[t][0]:.0f} (ATH week, ~71-73k expected); "
        f"US open 2024-03-08 {us_open_utc(date(2024, 3, 8)):%H:%M} UTC, 2024-03-11 {us_open_utc(date(2024, 3, 11)):%H:%M} UTC"
    )

    # ---- primary (registered) ----
    evs, miss = collect(bars, trade_days, H, K, THR)
    is_ = [e for e in evs if e.event.date() < OOS_FROM]
    oos = [e for e in evs if e.event.date() >= OOS_FROM]
    plc, pmiss = collect(bars, plc_days, H, K, THR)
    say(
        f"\nPRIMARY S1 morning reversion |m|>=1%, US open, h={H} k={K}: trading days {len(trade_days)} "
        f"(missing bars {miss}), placebo days {len(plc_days)} (missing {pmiss})"
    )
    s_is, s_oos, s_all = hit_stats(is_), hit_stats(oos), hit_stats(evs)
    say(row("S1 IS 2021-2023", is_))
    say(row("S1 OOS 2024-2026-09", oos))
    say(row("S1 all", evs))
    say(row("S1 placebo (weekends+holidays, same clock)", plc))
    ok, why = gate_passes(s_is, s_oos, s_all)
    say(f"GATE: {'PASS' if ok else 'FAIL'}" + ("" if ok else " - " + "; ".join(why)))

    with (cache / "events_primary.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_utc", "entry_utc", "exit_utc", "morning", "ret", "direction", "hit"])
        for e in evs:
            w.writerow(
                [e.event.isoformat(), e.entry.isoformat(), e.exit.isoformat(), f"{e.morning:.6f}", f"{e.ret:.6f}", e.direction, int(e.hit)]
            )

    # ---- descriptive (Bonferroni alpha = 0.05/3) ----
    say("\nDESCRIPTIVE candidates (alpha 0.05/3 = 0.0167)")
    say(row("S1 long side only = S2 owner case (m<=-1%)", [e for e in evs if e.direction == 1]))
    say(row("S1 short side only (m>=+1%)", [e for e in evs if e.direction == -1]))
    say(row("S2 OOS", [e for e in oos if e.direction == 1]))
    say(row("S3 unconditional long, all trading days", as_long(evs)))
    say(row("S3 unconditional long, OOS", as_long(oos)))
    say("by year (S1):")
    for y in range(2021, 2027):
        say(row(f"  {y}", [e for e in evs if e.event.year == y]))

    # ---- sensitivity grid (descriptive, every variant) ----
    say("\nGRID (descriptive): threshold x h x k, all / OOS")
    for thr in (0.005, 0.01, 0.02):
        for h in (1, 4):
            for k in (1, 2):
                g, _ = collect(bars, trade_days, h, k, thr)
                go = [e for e in g if e.event.date() >= OOS_FROM]
                say(row(f"thr={thr:.3f} h={h} k={k} all", g))
                say(row(f"thr={thr:.3f} h={h} k={k} OOS", go))
    say(f"peak RSS {rss_mb():.0f} MB")

    # ---- ETH, same rule (descriptive) ----
    eth_src = k5m / "ETHUSDT"
    if any(eth_src.glob("*.json")):
        del bars
        bars = load_bars(eth_src)
        e_evs, e_miss = collect(bars, trade_days, H, K, THR)
        say(f"\nETHUSDT same rule (descriptive), missing {e_miss}")
        say(row("ETH S1 IS", [e for e in e_evs if e.event.date() < OOS_FROM]))
        say(row("ETH S1 OOS", [e for e in e_evs if e.event.date() >= OOS_FROM]))
        say(row("ETH S1 all", e_evs))
        bars = load_bars(src)

    # ---- macro releases (descriptive only, no conclusion) ----
    mpath = Path(a.macro_csv)
    if mpath.exists():
        say("\nMACRO releases (descriptive only): S1 rule with T = release time, h=4 k=2")
        with mpath.open() as fh:
            rows_ = [r for r in csv.DictReader(fh) if r.get("status") == "actual"]
        for kind in ("cpi", "nfp", "fomc"):
            m_evs = []
            for r in rows_:
                d = date.fromisoformat(r["date"])
                if r["kind"] != kind or not (START <= d <= END):
                    continue
                hh, mm = (int(x) for x in r["time_et"].split(":"))
                ev = direction_event(bars, et_time_utc(d, hh, mm), H, K, THR)
                if ev is not None:
                    m_evs.append(ev)
            say(row(f"{kind} S1", m_evs))
            say(row(f"{kind} unconditional long", as_long(m_evs)))
    say(f"\ndone; peak RSS {rss_mb():.0f} MB; events CSV {cache / 'events_primary.csv'}")

    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
