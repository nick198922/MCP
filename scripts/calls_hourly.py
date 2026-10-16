"""Replay a trader's calls on hourly bars (entry limit, stop, first target, 7-day time exit) and summarise them in R.

    uv run python scripts/calls_hourly.py CALLS.csv --out RESULTS.csv [--md SUMMARY.md]

CALLS.csv columns (extra columns are ignored): id, utc (YYYY-MM-DDTHH:MM, the moment of the call), asset, instrument,
side (long|short), entry_low, entry_high ("mkt" = market at the next bar's open), stop (a price, or LOW / HIGH = beyond
the extreme of the 24 h before the call), tp1, setup, evaluable (y = entry and stop, p = entry only, n = skip), symbol
(optional "venue:SYMBOL" override), entry_type (mkt | limit, as the author worded it; empty = a single price within
0.5 % of the market is taken as market), cancel_utc (optional: the author withdrew a resting limit then; the entry stays a
limit and an unfilled call ends there), author_r, author_depo_pct (what the author reported, optional).

Bars: 1h perps from Binance USD-M, Bybit, Gate, MEXC, OKX, Bitget, then KuCoin spot and Hyperliquid; cached in
~/.cache/tradedesk-agents/calls-1h. Replay rules: src/tradedesk/analytics/trade_replay.py. A zone enters at its near edge (long: top, short: bottom),
as in the daily tracker (docs/calls); a single entry price within 0.5 % of the market at the call is a market order
(authors quote the current price rounded). A call whose market has no bar within 2 h of the call (listed later) or whose
entry is more than 50 % away from the first open (another token under the same ticker) is reported as no_data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path

from tradedesk.analytics.trade_replay import Bar, Replay, replay
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

H = 3_600_000
FILL_H, HOLD_H, COST = 72, 168, 0.0014
M = 60_000
MKT_TOL = 0.005  # a single entry price this close to the market is a market order
CACHE = Path.home() / ".cache/tradedesk-agents/calls-1h"
VENUES = ("binanceusdm", "bybit", "gate", "mexc", "okx", "bitget")
ALIASES = {"XAU": ["XAU", "PAXG"], "FARTCOIN": ["FARTCOIN"], "SPX": ["SPX"], "PEPE": ["1000PEPE"], "BONK": ["1000BONK"]}
STOP_PAD = 0.001  # LOW/HIGH stops sit 0.1 % beyond the 24 h extreme


def _ms(utc: str) -> int:
    return int(datetime.strptime(utc, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def _f(x: str) -> float | None:
    x = (x or "").strip()
    try:
        return float(x)
    except ValueError:
        return None


def candidates(row: dict) -> list[str]:
    if row.get("symbol"):
        return [row["symbol"]]
    names = ALIASES.get(row["asset"], [row["asset"]])
    out = [f"{v}:{n}/USDT:USDT" for n in names for v in VENUES]
    out += [f"kucoin:{n}/USDT" for n in names] + [f"hyperliquid:{n}" for n in names]
    return out


async def fetch(rt, spec: str, since: int, until: int, markets: dict, tf: str = "1h") -> list[Bar]:
    key = CACHE / (spec.replace("/", "_").replace(":", "_") + f"_{tf}_{since}_{until}.json")
    if key.exists():
        return [Bar(*r) for r in json.loads(key.read_text())]
    venue, _, sym = spec.partition(":")
    rows: list[list] = []
    if venue == "hyperliquid":
        raw = (await rt.hyperliquid.candles(sym, tf, since, until)).data or []
        rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])] for c in raw]
    else:
        if venue not in markets:
            markets[venue] = set(((await rt.exchanges.markets(venue)).data or {}).keys())
        if sym not in markets[venue]:
            return []
        step = M if tf == "1m" else H
        t = since
        for _ in range(60):  # page by windows of 300 bars: some venues return the *latest* bars of a longer window
            end = min(until, t + 299 * step)
            got = (await rt.exchanges.ohlcv(venue, sym, tf, 300, since_ms=t, until_ms=end)).data or []
            got = [r for r in got if t <= int(r[0]) <= end]
            rows += got
            t = (int(got[-1][0]) if got else end) + step
            if t > until:
                break
    seen, bars = set(), []
    for r in sorted(rows, key=lambda r: int(r[0])):
        if int(r[0]) not in seen and None not in r[1:5]:
            seen.add(int(r[0]))
            bars.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    if bars:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps([[b.t, b.open, b.high, b.low, b.close] for b in bars]))
    return bars


async def bars_for(rt, row: dict, start: int, markets: dict, minutes_to: int | None = None) -> tuple[list[Bar], str]:
    """Hourly bars wholly before the call (for LOW/HIGH stops), 1m bars of the rest of the call's hour (or up to
    ``minutes_to``, e.g. the moment the author withdrew a limit), then hourly bars."""
    hour = -(-max(start, minutes_to or start) // H) * H  # the first full hour after the minute window
    since, until = start - 25 * H, start + (FILL_H + HOLD_H + 2) * H
    for spec in candidates(row):
        try:
            hourly = await fetch(rt, spec, since, until, markets)
            minutes = await fetch(rt, spec, start // M * M, hour - M, markets, "1m") if hour > start else []
        except (ProviderError, KeyError, TypeError, ValueError) as e:
            logging.warning("%s %s: %s", row["id"], spec, e)
            continue
        if any(b.t >= start for b in hourly + minutes):
            bars = [b for b in hourly if b.t + H <= start] + [b for b in minutes if b.t >= start // M * M]
            return bars + [b for b in hourly if b.t >= hour], spec
    return [], ""


def plan(row: dict, bars: list[Bar], start: int) -> tuple[float | None, float | None, float | None]:
    """(entry, stop, target) of the row; entry None = market."""
    first = next((b.open for b in sorted(bars, key=lambda b: b.t) if b.t >= start), None)
    lo, hi = (row["entry_low"] or "").strip(), (row["entry_high"] or "").strip()
    lo_v = first if lo == "mkt" else _f(lo)
    hi_v = first if hi == "mkt" else _f(hi)
    if lo == hi == "mkt":
        entry = None
    elif lo_v is None or hi_v is None:
        entry = lo_v if hi_v is None else hi_v
    else:
        entry = max(lo_v, hi_v) if row["side"] == "long" else min(lo_v, hi_v)
    st = (row["stop"] or "").strip().upper()
    before = [b for b in bars if start - 25 * H <= b.t and b.t + H <= start]
    if st == "LOW" and before:
        stop = min(b.low for b in before) * (1 - STOP_PAD)
    elif st == "HIGH" and before:
        stop = max(b.high for b in before) * (1 + STOP_PAD)
    else:
        stop = _f(row["stop"])
    return entry, stop, _f(row.get("tp1", ""))


def fill_window(row: dict, start: int) -> int | None:
    """How long a limit may wait when the author withdrew it (``cancel_utc``), capped at FILL_H; None if not withdrawn."""
    c = (row.get("cancel_utc") or "").strip()
    return max(0, min(FILL_H * H, _ms(c) - start)) if c else None


def boot_ci(xs: list[float], n: int = 4000, seed: int = 7) -> tuple[float, float]:
    rnd = random.Random(seed)
    means = sorted(statistics.fmean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def _r(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


def summarise(results: list[tuple[dict, Replay, str, float | None, float | None, Replay]]) -> str:
    y = [(row, r) for row, r, *_ in results if row["evaluable"] == "y" and r.r_net is not None and r.status != "open"]
    y_be = [(row, be) for row, r, _, _, _, be in results if row["evaluable"] == "y" and be.r_net is not None and be.status != "open"]
    out = ["# Заклики на годинних свічках: підсумок", ""]
    out.append(
        f"Правила: вхід — ліміт на ближньому краї зони (чекаємо {FILL_H} год; година заклику — хвилинні свічки; одна ціна в межах 0.5 % від ринку = вхід за ринком), стоп від "
        f"бару входу, перша ціль з наступного бару, обидва в одному барі = стоп, вихід за часом через {HOLD_H} год; "
        f"R — від запланованого ризику; витрати {COST * 100:.2f} % за коло."
    )
    out.append("")

    def block(title: str, rows: list[tuple[dict, Replay]]) -> list[str]:
        if not rows:
            return []
        rn = [r.r_net for _, r in rows]
        lo, hi = boot_ci(rn) if len(rn) >= 5 else (float("nan"), float("nan"))
        st = {k: sum(r.status == k for _, r in rows) for k in ("target", "stop", "time")}
        st["stop"] += sum(r.status == "breakeven" for _, r in rows)
        return [
            f"| {title} | {len(rows)} | {st['target']} | {st['stop']} | {st['time']} | "
            f"{sum(x > 0 for x in rn) / len(rn) * 100:.0f} % | {statistics.fmean(rn):+.2f} | [{lo:+.2f}; {hi:+.2f}] | "
            f"{statistics.median(rn):+.2f} | {sum(rn):+.1f} |"
        ]

    out += [
        "| Група | Угод | Ціль | Стоп | Час | R>0 | Сер. R net | 95 % CI | Медіана | Сума R |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    out += block("усі (y)", y)
    out += block("усі (y), стоп у БУ після +1R", y_be)
    for key in sorted({row["setup"] for row, _ in y}):
        out += block(f"setup: {key}", [(a, b) for a, b in y if a["setup"] == key])
    for side in ("long", "short"):
        out += block(f"side: {side}", [(a, b) for a, b in y if a["side"] == side])
    out += block("BTC/ETH", [(a, b) for a, b in y if a["asset"] in ("BTC", "ETH")])
    out += block("альти", [(a, b) for a, b in y if a["asset"] not in ("BTC", "ETH")])
    return "\n".join(out) + "\n"


async def run(rows: list[dict]) -> list[tuple[dict, Replay, str, float | None, float | None, Replay]]:
    _, rt = build_server()
    markets: dict = {}
    res = []
    try:
        for row in rows:
            if row["evaluable"] not in ("y", "p"):
                continue
            start = _ms(row["utc"]) + M  # the next minute: the call is stamped to the minute
            cancel = fill_window(row, start)
            bars, spec = await bars_for(rt, row, start, markets, None if cancel is None else start + cancel)
            entry, stop, target = plan(row, bars, start)
            kind = (row.get("entry_type") or "").strip()  # mkt | limit | "" (decide by MKT_TOL)
            if kind == "mkt":
                entry = None
            first = min((b for b in bars if b.t >= start), key=lambda b: b.t, default=None)
            if first is None or first.t > start + 2 * H:
                bars, spec = [], (spec + " (listed later)") if spec else ""
            elif entry is not None and not 0.5 <= first.open / entry <= 1.5:
                bars, spec = [], spec + f" (price {first.open:.6g} far from entry)"
            elif (
                not kind
                and cancel is None
                and entry is not None
                and row["entry_low"] == row["entry_high"]
                and abs(first.open / entry - 1) <= MKT_TOL
            ):
                entry = None  # one price within MKT_TOL of the market at the call: the author's «current price»
            r = (
                replay(
                    bars,
                    side=row["side"],
                    start_ms=start,
                    entry=entry,
                    stop=stop,
                    target=target,
                    fill_ms=FILL_H * H if cancel is None else cancel,
                    hold_ms=HOLD_H * H,
                    cost_frac=COST,
                )
                if bars
                else Replay(status="no_data")
            )
            be = (
                replay(
                    bars,
                    side=row["side"],
                    start_ms=start,
                    entry=entry,
                    stop=stop,
                    target=target,
                    fill_ms=FILL_H * H if cancel is None else cancel,
                    hold_ms=HOLD_H * H,
                    cost_frac=COST,
                    be_at_r=1.0,
                )
                if bars
                else Replay(status="no_data")
            )
            res.append((row, r, spec, entry, stop, be))
            print(f"{row['id']:16} {spec or '—':40} {r.status:8} R={_r(r.r_net)}", flush=True)
    finally:
        await rt.aclose()
    return res


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("calls", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--md", type=Path)
    a = ap.parse_args()
    rows = list(csv.DictReader(a.calls.open(encoding="utf-8")))
    res = asyncio.run(run(rows))
    cols = [
        "id",
        "utc",
        "asset",
        "side",
        "setup",
        "evaluable",
        "venue",
        "entry",
        "stop",
        "status",
        "fill_utc",
        "fill",
        "exit_utc",
        "exit",
        "ret_pct",
        "r_gross",
        "r_net",
        "mfe_r",
        "mae_r",
        "hours",
        "paper_status",
        "paper_r",
        "paper_mae_r",
        "target_after_stop",
        "be_status",
        "be_r_net",
        "author_r",
        "author_depo_pct",
    ]

    def ts(ms: int | None) -> str:
        return "" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M")

    def num(x: float | None, d: int = 3) -> str:
        return "" if x is None else f"{x:.{d}f}"

    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row, r, spec, entry, stop, be in res:
            w.writerow(
                [
                    row["id"],
                    row["utc"],
                    row["asset"],
                    row["side"],
                    row["setup"],
                    row["evaluable"],
                    spec,
                    num(r.fill if r.fill is not None else entry, 8),
                    num(stop, 8),
                    r.status,
                    ts(r.fill_t),
                    num(r.fill, 8),
                    ts(r.exit_t),
                    num(r.exit, 8),
                    num(None if r.ret is None else r.ret * 100, 2),
                    num(r.r_gross, 2),
                    num(r.r_net, 2),
                    num(r.mfe_r, 2),
                    num(r.mae_r, 2),
                    num(None if r.exit_t is None else (r.exit_t - r.fill_t) / H, 1),
                    r.paper_status or "",
                    num(r.paper_r, 2),
                    num(r.paper_mae_r, 2),
                    "" if r.target_after_stop is None else int(r.target_after_stop),
                    be.status,
                    num(be.r_net, 2),
                    row.get("author_r", ""),
                    row.get("author_depo_pct", ""),
                ]
            )
    if a.md:
        a.md.write_text(summarise(res), encoding="utf-8")
    print(f"{len(res)} calls replayed -> {a.out}")


if __name__ == "__main__":
    main()
