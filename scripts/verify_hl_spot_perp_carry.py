"""Same-venue cash-and-carry on Hyperliquid (long spot UBTC / UETH / HYPE / USOL + short the perp) vs the same metric on
Binance over the same window.

Idea card: docs/ideas/IDEA-20261029-hl-spot-perp-carry.md (section "Попередня реєстрація").

Modes:
  fetch   HL spotMeta, 4h candles of the spot pairs and perps, hourly fundingHistory; Binance 4h spot / perp candles and
          funding; a few live order-book snapshots of every leg (the only real spread source: the collector stores HL
          perp books only).
  gate    step 0: coverage, gates G1-G3, Binance metric on the same window, measured slippage. HL carry means are
          not printed (only their block SD for the power gate).
  report  the pre-registered H1 / H2 run plus robustness.

Network only through the runtime providers: Hyperliquid info calls go through ``rt.hyperliquid._info`` (the provider
has no spot-candle method), Binance through ``rt.exchanges``. Pacing: HL <= 1100 weight / min.

Usage:
    uv run python scripts/verify_hl_spot_perp_carry.py fetch --cache-dir ~/.cache/tradedesk-agents/hl_spot_perp_carry
    uv run python scripts/verify_hl_spot_perp_carry.py gate --cache-dir ... --out gate.md
    uv run python scripts/verify_hl_spot_perp_carry.py report --cache-dir ... --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import spot_perp_carry as spc
from tradedesk.server import build_server

H4 = 4 * spc.HOUR_MS
START = int(datetime(2025, 10, 29, tzinfo=UTC).timestamp() * 1000)  # primary window: last 12 months
END = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp() * 1000)
FETCH_FUNDING = int(datetime(2024, 12, 1, tzinfo=UTC).timestamp() * 1000)
FETCH_CANDLES = int(datetime(2024, 11, 1, tzinfo=UTC).timestamp() * 1000)
N_BLOCKS = 12

# pre-registered parameters (card section "Попередня реєстрація")
SPOT_BASE = {"BTC": "UBTC", "ETH": "UETH", "HYPE": "HYPE", "SOL": "USOL"}
PRIMARY = ("BTC", "ETH")
FEES = {"hl": (0.0007, 0.00045), "bin": (0.0010, 0.0005)}  # (spot, perp) taker per side
NOTIONAL = 10_000.0
REBAL_BAND = 0.20
TBILL = 0.040
BAR_NET, BAR_EXCESS, BAR_CI = 0.06, 0.02, 0.03
BAR_MDD = 0.03
CAPITAL_MULT = 1.0 + 1.0 / 3.0  # spot fully paid + perp margin at 3x


def ymd(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None or x != x else f"{x * 100:+.{d}f}%"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


# ----------------------------------------------------------------------------- fetching


class Pacer:
    def __init__(self, per_min: float) -> None:
        self.per_min, self.next_ok, self.lock = per_min, 0.0, asyncio.Lock()

    async def take(self, weight: float) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next_ok > now:
                await asyncio.sleep(self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + weight * 60.0 / self.per_min


async def retry(go: Any, what: str, tries: int = 5) -> Any:
    for k in range(tries):
        try:
            return await go()
        except Exception as e:  # noqa: BLE001
            wait = 15 * (k + 1)
            print(f"  retry {what} in {wait}s after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError(f"giving up on {what}")


class Fetcher:
    def __init__(self, rt: Any, cache: Path) -> None:
        self.rt, self.cache, self.pace, self.calls = rt, cache, Pacer(1100.0), 0

    async def hl(self, payload: dict[str, Any], weight: float) -> Any:
        await self.pace.take(weight)
        self.calls += 1
        return (await retry(lambda: self.rt.hyperliquid._info(payload, ttl=0), f"hl {payload.get('type')}")).data

    async def hl_candles(self, coin: str) -> list[list[float]]:
        req = {"coin": coin, "interval": "4h", "startTime": FETCH_CANDLES, "endTime": END}
        rows = await self.hl({"type": "candleSnapshot", "req": req}, 100)
        return [[int(r["t"]), float(r["c"]), float(r["v"])] for r in rows or [] if int(r["t"]) + H4 <= END]

    async def hl_funding(self, coin: str) -> dict[str, Any]:
        out: dict[int, list[float]] = {}
        start, complete = FETCH_FUNDING, False
        for _ in range(200):
            rows = await self.hl({"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": END}, 45)
            batch = [r for r in rows or [] if r.get("time") is not None]
            for r in batch:
                out[int(r["time"])] = [float(r["fundingRate"]), float(r.get("premium") or 0.0)]
            if len(batch) < 500:
                complete = True
                break
            nxt = max(int(r["time"]) for r in batch) + 1
            if nxt <= start:
                break
            start = nxt
        return {"rows": [[t, *v] for t, v in sorted(out.items()) if t <= END], "complete": complete}

    async def cex_candles(self, venue: str, sym: str) -> list[list[float]]:
        rows: dict[int, list[float]] = {}
        since = FETCH_CANDLES
        while since < END:
            until = min(END, since + 1000 * H4) - 1
            f = await retry(lambda s=since, u=until: self.rt.exchanges.ohlcv(venue, sym, "4h", 1000, since_ms=s, until_ms=u), sym)
            batch = [b for b in (f.data or []) if isinstance(b, (list, tuple)) and len(b) >= 6]
            for b in batch:
                rows[int(b[0])] = [int(b[0]), float(b[4]), float(b[5] or 0.0)]
            last = max((int(b[0]) for b in batch), default=None)
            since = last + H4 if last is not None and last + H4 > since else until + 1
            await asyncio.sleep(0.2)
        return [v for t, v in sorted(rows.items()) if t + H4 <= END]

    async def cex_funding(self, sym: str) -> dict[str, Any]:
        out: dict[int, float] = {}
        since, complete = FETCH_FUNDING, False
        for _ in range(100):
            f = await retry(lambda s=since: self.rt.exchanges.funding_rate_history("binanceusdm", sym, s, 1000), sym)
            batch = [x for x in (f.data or []) if x.get("timestamp") is not None and x.get("fundingRate") is not None]
            for x in batch:
                out[int(x["timestamp"])] = float(x["fundingRate"])
            if not batch or len(batch) < 1000 or max(int(x["timestamp"]) for x in batch) >= END:
                complete = True
                break
            since = max(int(x["timestamp"]) for x in batch) + 1
            await asyncio.sleep(0.5)
        return {"rows": [[t, v, 0.0] for t, v in sorted(out.items()) if t <= END], "complete": complete}

    async def cached(self, name: str, go: Any) -> Any:
        p = self.cache / name
        if p.exists():
            return rd(p)
        t0 = time.monotonic()
        try:
            data = await go()
        except Exception as e:  # noqa: BLE001
            print(f"skip {name}: {type(e).__name__}: {str(e)[:160]}", flush=True)
            return None
        wr(p, data)
        n = len(data["rows"]) if isinstance(data, dict) and "rows" in data else len(data) if isinstance(data, list) else 1
        print(f"{name}: {n} rows, {time.monotonic() - t0:.1f}s, hl calls {self.calls}, rss {rss_mb():.0f} MB", flush=True)
        return data


def spot_pairs(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Base token name -> {pair, quote, index} for the USDC-quoted spot pair of every token in ``SPOT_BASE``."""
    toks = {t["index"]: t["name"] for t in meta.get("tokens", [])}
    want = set(SPOT_BASE.values())
    out: dict[str, dict[str, Any]] = {}
    for u in meta.get("universe", []):
        b, q = (toks.get(i) for i in u["tokens"][:2])
        if b in want and q == "USDC" and b not in out:
            out[b] = {"pair": u["name"], "quote": q, "index": u["index"]}
    return out


def levels(side: Any) -> list[tuple[float, float]]:
    out = []
    for lv in side or []:
        if isinstance(lv, dict):
            out.append((float(lv["px"]), float(lv["sz"])))
        else:
            out.append((float(lv[0]), float(lv[1])))
    return out


async def books(fx: Fetcher, pairs: dict[str, dict[str, Any]], n: int, gap_s: float) -> None:
    snaps = rd(fx.cache / "books.json") if (fx.cache / "books.json").exists() else []
    for k in range(n):
        snap: dict[str, Any] = {"ts": int(time.time() * 1000), "legs": {}}
        for asset, base in SPOT_BASE.items():
            legs = {f"hl_perp_{asset}": asset}
            if base in pairs:
                legs[f"hl_spot_{asset}"] = pairs[base]["pair"]
            for key, coin in legs.items():
                d = await fx.hl({"type": "l2Book", "coin": coin}, 2)
                lv = (d or {}).get("levels") or [[], []]
                snap["legs"][key] = {"bids": levels(lv[0]), "asks": levels(lv[1])}
            for key, venue, sym in (
                (f"bin_spot_{asset}", "binance", f"{asset}/USDT"),
                (f"bin_perp_{asset}", "binanceusdm", f"{asset}/USDT:USDT"),
            ):
                try:
                    ob = (await fx.rt.exchanges.order_book(venue, sym, 100)).data or {}
                    snap["legs"][key] = {"bids": levels(ob.get("bids"))[:100], "asks": levels(ob.get("asks"))[:100]}
                except Exception as e:  # noqa: BLE001
                    snap["legs"][key] = {"bids": [], "asks": [], "error": f"{type(e).__name__}"}
        snaps.append(snap)
        wr(fx.cache / "books.json", snaps)
        print(f"book snapshot {k + 1}/{n} at {datetime.fromtimestamp(snap['ts'] / 1000, UTC):%H:%M:%S}", flush=True)
        if k + 1 < n:
            await asyncio.sleep(gap_s)


async def fetch(cache: Path, only: list[str], n_books: int) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    try:
        meta = await fx.cached("hl_spotmeta.json", lambda: fx.hl({"type": "spotMeta"}, 20))
        pairs = spot_pairs(meta or {})
        print(f"spot pairs: {pairs}", flush=True)
        for asset in only:
            base = SPOT_BASE[asset]
            if base in pairs:
                await fx.cached(f"hl_spot_{asset}.json", lambda c=pairs[base]["pair"]: fx.hl_candles(c))
            await fx.cached(f"hl_perp_{asset}.json", lambda a=asset: fx.hl_candles(a))
            await fx.cached(f"hl_funding_{asset}.json", lambda a=asset: fx.hl_funding(a))
            await fx.cached(f"bin_spot_{asset}.json", lambda a=asset: fx.cex_candles("binance", f"{a}/USDT"))
            await fx.cached(f"bin_perp_{asset}.json", lambda a=asset: fx.cex_candles("binanceusdm", f"{a}/USDT:USDT"))
            await fx.cached(f"bin_funding_{asset}.json", lambda a=asset: fx.cex_funding(f"{a}/USDT:USDT"))
        if n_books:
            await books(fx, pairs, n_books, 15.0)
    finally:
        await rt.aclose()
    print(f"fetch done: hl calls {fx.calls}, peak rss {rss_mb():.0f} MB", flush=True)


# ----------------------------------------------------------------------------- analysis


def load(cache: Path, venue: str, asset: str) -> dict[str, Any] | None:
    ps, pp, pf = (cache / f"{venue}_{k}_{asset}.json" for k in ("spot", "perp", "funding"))
    if not (ps.exists() and pp.exists() and pf.exists()):
        return None
    spot, perp, fund = rd(ps), rd(pp), rd(pf)
    if not spot or not perp or not fund or not fund.get("rows"):
        return None
    marks = spc.align_marks([(int(t) + H4, c) for t, c, _ in spot], [(int(t) + H4, c) for t, c, _ in perp])
    return {
        "marks": marks,
        "funding": [(int(r[0]), r[1]) for r in fund["rows"]],
        "premium": [(int(r[0]), r[2]) for r in fund["rows"]],
        "complete": fund.get("complete"),
        "spot_ts": [int(t) for t, _, _ in spot],
        "perp_ts": [int(t) for t, _, _ in perp],
        "spot_vol_usd": [v * c for t, c, v in spot if START <= t < END],
    }


def slippage(cache: Path) -> dict[str, dict[str, Any]]:
    p = cache / "books.json"
    if not p.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    snaps = rd(p)
    keys = sorted({k for s in snaps for k in s["legs"]})
    for key in keys:
        row: dict[str, Any] = {"n": 0}
        for notional in (NOTIONAL, 50_000.0):
            vals = [spc.book_slippage(s["legs"][key]["bids"], s["legs"][key]["asks"], notional) for s in snaps if key in s["legs"]]
            avg = [v["avg"] for v in vals if v["avg"] is not None]
            hs = [v["half_spread"] for v in vals if v["half_spread"] is not None]
            row[int(notional)] = statistics.median(avg) if avg else None
            row["half_spread"] = statistics.median(hs) if hs else None
            row["n"] = len(vals)
        out[key] = row
    return out


def costs(
    venue: str, asset: str, slip: dict[str, dict[str, Any]], fee_mult: float = 1.0, slip_mult: float = 1.0, slip_fixed: float | None = None
) -> tuple[float, float]:
    fs, fp = FEES[venue]
    ss = (slip.get(f"{venue}_spot_{asset}") or {}).get(int(NOTIONAL))
    sp = (slip.get(f"{venue}_perp_{asset}") or {}).get(int(NOTIONAL))
    if slip_fixed is not None:  # assumption scenario: the same slippage on every leg and side
        ss = sp = slip_fixed
    side = fee_mult * (fs + fp) + slip_mult * ((ss or 0.0) + (sp or 0.0))
    return 2 * side, side  # round trip, rebalancing rate


def run(d: dict[str, Any], venue: str, asset: str, slip: dict[str, Any], start: int = START, end: int = END, n_blocks: int = N_BLOCKS,
        fee_mult: float = 1.0, slip_mult: float = 1.0, band: float = REBAL_BAND, rotate: bool = False,
        slip_fixed: float | None = None, max_abs_basis: float | None = None) -> dict[str, Any]:  # fmt: skip
    rt_cost, rebal = costs(venue, asset, slip, fee_mult, slip_mult, slip_fixed)
    marks = d["marks"]
    if max_abs_basis is not None:  # exploratory data cleaning: drop bad spot prints (listing-day quotes such as 6 969 696)
        marks = [m for m in marks if abs(m[2] / m[1] - 1) <= max_abs_basis]
    steps = spc.carry_steps(d["funding"], marks, start, end, rebalance_band=band, rebalance_cost=rebal)
    s = spc.summarize(steps, start, end, n_blocks, rt_cost * (n_blocks if rotate else 1))
    s["rt_cost"], s["steps"] = rt_cost, steps
    s["boot"] = spc.block_bootstrap_ci(s["blocks"])
    return s


def gate(cache: Path) -> str:
    slip = slippage(cache)
    out = ["## Крок 0: покриття, ворота, Binance за те саме вікно", ""]
    out.append(f"Вікно основне: {ymd(START)} … {ymd(END)} (12 блоків по 365/12 дн.). Свічки 4h, funding HL щогодини, Binance 8h.")
    meta = rd(cache / "hl_spotmeta.json") if (cache / "hl_spotmeta.json").exists() else {}
    pairs = spot_pairs(meta)
    out += ["", "| актив | спот-пара HL | 1-а спот-свічка | 1-а перп-свічка | спільна історія до кінця, міс | покриття 4h спот / перп у вікні | медіана |базис| | funding HL: 1-й запис, покриття 1h у вікні, повна | обіг спота у вікні, $/добу (медіана) |",
            "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    gates: dict[str, dict[str, bool]] = {}
    for asset, base in SPOT_BASE.items():
        d = load(cache, "hl", asset)
        if d is None:
            out.append(f"| {asset} | {pairs.get(base, {}).get('pair', 'нема')} | — | — | — | — | — | — | — |")
            gates[asset] = {"G1": False, "G2": False}
            continue
        first_common = max(d["spot_ts"][0], d["perp_ts"][0])
        months = (END - first_common) / (spc.YEAR_MS / 12)
        cs = spc.coverage(d["spot_ts"], START, END, H4)
        cp = spc.coverage(d["perp_ts"], START, END, H4)
        cf = spc.coverage([t for t, _ in d["funding"]], START, END, spc.HOUR_MS)
        basis = [abs(p / s - 1) for t, s, p in d["marks"] if START <= t <= END]
        vol = statistics.median(d["spot_vol_usd"]) * 6 if d["spot_vol_usd"] else 0.0
        gates[asset] = {"G1": first_common <= START and months >= 12, "G2": cf >= 0.95}
        out.append(f"| {asset} | {pairs[base]['pair']} ({base}/USDC) | {ymd(d['spot_ts'][0])} | {ymd(d['perp_ts'][0])} | {months:.1f} | "
                   f"{cs:.1%} / {cp:.1%} | {statistics.median(basis):.3%} | {ymd(d['funding'][0][0])}, {cf:.1%}, {d['complete']} | {vol:,.0f} |")  # fmt: skip
    out += ["", "### Проковзування з книги (живі знімки, медіана; частка від mid на одну сторону, одна нога)", "",
            "| нога | знімків | півспреду | $10k | $50k |", "|---|---|---|---|---|"]  # fmt: skip
    for k, v in slip.items():
        out.append(f"| {k} | {v['n']} | {pct(v.get('half_spread'), 4)} | {pct(v.get(10000), 4)} | {pct(v.get(50000), 4)} |")
    out += [
        "",
        "### Ворота",
        "",
        "| актив | G1 ≥12 міс. спільної історії | G2 funding ≥95 % | SD блоків HL, п.п. річних | MDE = 2.8·SD/√12 | G3 MDE ≤3 п.п. |",
        "|---|---|---|---|---|---|",
    ]
    for asset in SPOT_BASE:
        d = load(cache, "hl", asset)
        sd = mdev = None
        if d is not None and gates[asset]["G1"]:
            s = run(d, "hl", asset, slip)
            sd = s["sd"]
            mdev = spc.mde(sd, len(s["blocks"]))
        g3 = mdev is not None and mdev <= 0.03
        gates[asset]["G3"] = g3
        out.append(f"| {asset} | {'так' if gates[asset]['G1'] else 'ні'} | {'так' if gates[asset]['G2'] else 'ні'} | "
                   f"{'—' if sd is None else f'{sd * 100:.1f}'} | {'—' if mdev is None else f'{mdev * 100:.1f}'} | {'так' if g3 else 'ні'} |")  # fmt: skip
    out += ["", "Середні HL на кроці 0 не друкуються (лише SD для G3).", "",
            "### Binance: той самий метрик, те саме вікно (умова 2)", "", table_header()]  # fmt: skip
    for asset in SPOT_BASE:
        d = load(cache, "bin", asset)
        if d is None or d["spot_ts"][0] > START:
            first = "—" if d is None else ymd(d["spot_ts"][0])
            out.append(f"| {asset} Binance | спот з {first}, вікно не покрите | | | | | | | | |")
            continue
        out.append(table_row(f"{asset} Binance", run(d, "bin", asset, slip)))
    return "\n".join(out)


def table_header() -> str:
    return ("| ряд | net APR | funding | basis | витрати | сер. блоків | 95 % t-CI | bootstrap CI (блок 3) | блоків > 0 | max DD базису |\n"
            "|---|---|---|---|---|---|---|---|---|---|")  # fmt: skip


def table_row(label: str, s: dict[str, Any]) -> str:
    return (f"| {label} | {pct(s['net_apr'])} | {pct(s['funding_apr'])} | {pct(s['basis_apr'])} | {pct(s['cost_apr'])} | "
            f"{pct(s['block_mean'])} | {pct(s['ci'][0])} … {pct(s['ci'][1])} | {pct(s['boot'][0])} … {pct(s['boot'][1])} | "
            f"{s['positive_blocks']}/{len(s['blocks'])} | {s['basis_mdd']:.2%} |")  # fmt: skip


def report(cache: Path) -> str:
    slip = slippage(cache)
    hl = {a: load(cache, "hl", a) for a in SPOT_BASE}
    bn = {a: load(cache, "bin", a) for a in SPOT_BASE}
    hl = {a: d for a, d in hl.items() if d is not None and d["spot_ts"][0] <= START}
    bn = {a: d for a, d in bn.items() if d is not None and d["spot_ts"][0] <= START}
    base = {a: run(d, "hl", a, slip) for a, d in hl.items()}
    bbase = {a: run(d, "bin", a, slip) for a, d in bn.items()}
    out = [gate(cache), "", "## Основний прогін (зареєстровано): завжди в позиції, одне коло, taker + проковзування з книги", ""]
    out += [
        "Витрати на коло: "
        + ", ".join(f"HL {a} {base[a]['rt_cost']:.3%}" for a in base)
        + "; "
        + ", ".join(f"Binance {a} {bbase[a]['rt_cost']:.3%}" for a in bbase),
        "",
    ]
    out.append(table_header())
    for a in base:
        out.append(table_row(f"**HL {a}**" if a in PRIMARY else f"HL {a} (описово)", base[a]))
    for a in bbase:
        out.append(table_row(f"Binance {a}", bbase[a]))

    out += ["", "### H1 / H2 по активах", "", f"Пороги: net ≥ {BAR_NET:.0%}, надлишок над T-bill ({TBILL:.1%}) ≥ {BAR_EXCESS * 100:.0f} п.п., нижня межа t-CI блоків > {BAR_CI:.0%}; "
            f"H2: обидві половини > 0 і max DD базису ≤ {BAR_MDD:.0%}.", "",
            "| актив | net | надлишок над T-bill | t-CI низ | H1 | 1-а половина | 2-а половина | max DD базису | H2 |", "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    verdict: dict[str, tuple[bool, bool]] = {}
    for a, s in base.items():
        h1 = s["net_apr"] >= BAR_NET and s["net_apr"] - TBILL >= BAR_EXCESS and s["ci"][0] > BAR_CI
        b = s["blocks"]
        h1a, h2a = statistics.fmean(b[: len(b) // 2]), statistics.fmean(b[len(b) // 2 :])
        h2 = h1a > 0 and h2a > 0 and s["basis_mdd"] <= BAR_MDD
        verdict[a] = (h1, h2)
        out.append(f"| {a}{'' if a in PRIMARY else ' (описово)'} | {pct(s['net_apr'])} | {pct(s['net_apr'] - TBILL)} | {pct(s['ci'][0])} | "
                   f"{'✓' if h1 else '✗'} | {pct(h1a)} | {pct(h2a)} | {s['basis_mdd']:.2%} | {'✓' if h2 else '✗'} |")  # fmt: skip

    out += [
        "",
        "### HL мінус Binance по блоках (парні різниці, той самий актив)",
        "",
        "| актив | HL сер. | Binance сер. | різниця | 95 % t-CI різниці | funding HL − Binance, п.п. річних |",
        "|---|---|---|---|---|---|",
    ]
    for a in base:
        if a not in bbase:
            continue
        diff = [x - y for x, y in zip(base[a]["blocks"], bbase[a]["blocks"], strict=False)]
        m, ci = spc.mean_ci(diff)
        out.append(f"| {a} | {pct(base[a]['block_mean'])} | {pct(bbase[a]['block_mean'])} | {pct(m)} | {pct(ci[0])} … {pct(ci[1])} | "
                   f"{pct(base[a]['funding_apr'] - bbase[a]['funding_apr'])} |")  # fmt: skip

    out += ["", "### Блоки (місяці), net APR на номінал", "", "| блок | початок | " + " | ".join(f"HL {a}" for a in base) + " | " + " | ".join(f"Bin {a}" for a in bbase) + " |",
            "|---|---|" + "---|" * (len(base) + len(bbase))]  # fmt: skip
    bms = (END - START) // N_BLOCKS
    for i in range(N_BLOCKS):
        cells = [pct(base[a]["blocks"][i]) if i < len(base[a]["blocks"]) else "—" for a in base]
        cells += [pct(bbase[a]["blocks"][i]) if i < len(bbase[a]["blocks"]) else "—" for a in bbase]
        out.append(f"| {i + 1} | {ymd(START + i * bms)} | " + " | ".join(cells) + " |")

    out += ["", "## Стійкість (усі варіанти; жоден не обраний після результатів)", "", table_header()]
    for a in [x for x in PRIMARY if x in hl]:
        d = hl[a]
        for label, kw in (("комісії ×2", {"fee_mult": 2.0}), ("проковзування ×3", {"slip_mult": 3.0}), ("коло щомісяця (ротація)", {"rotate": True}),
                          ("ребаланс-смуга 10 %", {"band": 0.10}),
                          ("проковзування 0.05 %/нога/сторона (припущення, стрес)", {"slip_fixed": 0.0005}),
                          ("ротація + проковзування 0.05 % (припущення, стрес)", {"rotate": True, "slip_fixed": 0.0005})):  # fmt: skip
            out.append(table_row(f"HL {a}: {label}", run(d, "hl", a, slip, **kw)))
        s = base[a]
        rest = spc.drop_top(s["blocks"], 3)
        m, ci = spc.mean_ci(rest)
        out.append(f"| HL {a}: без 3 найкращих блоків | {pct(m)} (сер. 9 блоків) | | | | | {pct(ci[0])} … {pct(ci[1])} | | | |")
        out.append(f"| HL {a}: на капітал (÷{CAPITAL_MULT:.2f}) | {pct(s['net_apr'] / CAPITAL_MULT)} | | | | | | | | |")
        out.append(f"| HL {a}: T-bill 4.5 % → надлишок | {pct(s['net_apr'] - 0.045)} | | | | | | | | |")
    for label, assets in (("кошик BTC+ETH", [a for a in PRIMARY if a in base]), ("кошик BTC+ETH+HYPE", [a for a in ("BTC", "ETH", "HYPE") if a in base]),
                          ("кошик усі HL", list(base))):  # fmt: skip
        if not assets:
            continue
        vals = [statistics.fmean(base[a]["blocks"][i] for a in assets) for i in range(N_BLOCKS)]
        m, ci = spc.mean_ci(vals)
        net = statistics.fmean(base[a]["net_apr"] for a in assets)
        out.append(
            f"| {label} (рівні ваги) | {pct(net)} | | | | {pct(m)} | {pct(ci[0])} … {pct(ci[1])} | | {sum(v > 0 for v in vals)}/{len(vals)} | |"
        )

    out += ["", "### Довше вікно: уся спільна історія спота й перпа HL (описово)", "", table_header()]
    for a in SPOT_BASE:
        d = load(cache, "hl", a)
        if d is None:
            continue
        st = max(d["spot_ts"][0], d["perp_ts"][0], d["funding"][0][0]) + H4
        nb = int((END - st) // (spc.YEAR_MS / 12))
        if nb < 3:
            continue
        s = run(d, "hl", a, slip, start=st, n_blocks=nb)
        out.append(table_row(f"HL {a} {ymd(st)} … ({nb} блоків)", s))
        # exploratory, added after seeing listing-day bad prints: skip the first 30 days and drop marks with |basis| > 2 %
        st2 = st + 30 * spc.DAY_MS
        nb2 = int((END - st2) // (spc.YEAR_MS / 12))
        n_bad = sum(1 for t, sp, pp in d["marks"] if t >= st2 and abs(pp / sp - 1) > 0.02)
        s2 = run(d, "hl", a, slip, start=st2, n_blocks=nb2, max_abs_basis=0.02)
        out.append(table_row(f"HL {a} {ymd(st2)} …, без |базису|>2 % ({n_bad} барів), розвідувально", s2))
        if a in bn and bn[a] is not None:
            out.append(table_row(f"Binance {a} {ymd(st2)} …", run(bn[a], "bin", a, slip, start=st2, n_blocks=nb2)))
    out += [
        "",
        "Основне вікно з тим самим фільтром |базису| ≤ 2 % (розвідувально, перевірка, що 1 поганий бар не змінює висновку):",
        "",
        table_header(),
    ]
    for a in hl:
        out.append(table_row(f"HL {a}, фільтр", run(hl[a], "hl", a, slip, max_abs_basis=0.02)))

    out += [
        "",
        "### Режим funding у вікні (описово)",
        "",
        "| ряд | сер. ставка, річних | частка виплат = база 0.01 %/8 год | частка < 0 | частка > бази |",
        "|---|---|---|---|---|",
    ]
    for venue, data in (("HL", hl), ("Binance", bn)):
        for a, d in data.items():
            rs = [r for t, r in d["funding"] if START < t <= END]
            if not rs:
                continue
            per = spc.HOUR_MS if venue == "HL" else 8 * spc.HOUR_MS
            basev = 0.0001 / 8 if venue == "HL" else 0.0001
            # HL funding is per hour; Binance may change interval, so annualize the sum over the window
            yrs = (END - START) / spc.YEAR_MS
            out.append(f"| {venue} {a} | {pct(sum(rs) / yrs)} | {sum(abs(r - basev) < 1e-9 for r in rs) / len(rs):.0%} | "
                       f"{sum(r < 0 for r in rs) / len(rs):.0%} | {sum(r > basev + 1e-9 for r in rs) / len(rs):.0%} |")  # fmt: skip
            del per

    out += ["", "## Перевірка по активах (5a)", ""]
    means = [base[a]["block_mean"] for a in base]
    ses = [base[a]["sd"] / len(base[a]["blocks"]) ** 0.5 for a in base]
    q = spc.cochran_q(means, ses)
    out.append(
        f"Cochran's Q по {len(base)} активах HL: Q = {q['q']:.2f}, df = {q['df']}, I² = {q['i2']:.0%}, p ≈ {q['p']:.3f}; зважене середнє {pct(q.get('pooled'))}."
    )
    first = {a: statistics.fmean(base[a]["blocks"][:6]) for a in base}
    second = {a: statistics.fmean(base[a]["blocks"][6:]) for a in base}
    rank = sorted(base, key=lambda a: -first[a])
    out += ["", "| актив (ранг за 1-ю половиною) | 1-а половина | 2-а половина |", "|---|---|---|"]
    out += [f"| {a} | {pct(first[a])} | {pct(second[a])} |" for a in rank]
    top = rank[: max(1, len(rank) // 4)]
    rest = [a for a in rank if a not in top]
    if rest:
        out.append(
            f"\nТоп-чверть ({', '.join(top)}) у 2-й половині: {pct(statistics.fmean(second[a] for a in top))}; решта: {pct(statistics.fmean(second[a] for a in rest))}."
        )
    out.append(f"\nПіковий RSS аналізу: {rss_mb():.0f} MB.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["fetch", "gate", "report"])
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out")
    ap.add_argument("--assets", default=",".join(SPOT_BASE))
    ap.add_argument("--books", type=int, default=6)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.mode == "fetch":
        asyncio.run(fetch(cache, [x for x in a.assets.split(",") if x], a.books))
        return
    text = gate(cache) if a.mode == "gate" else report(cache)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
        print(f"wrote {a.out} ({len(text.splitlines())} lines), peak rss {rss_mb():.0f} MB")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
