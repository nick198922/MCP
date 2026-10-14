"""Hyperliquid <-> Binance USDT-M funding spread: does a delta-neutral two-leg position earn after costs?

Idea card: docs/ideas/IDEA-20261014-hyperliquid-binance-funding-spread.md (section "Попередня реєстрація").

Pre-registered (fixed before the first full run):
  signal  S7 = trailing 7d (funding HL - funding Binance), annualised from actual payments, |S7| >= 25 %, both
          3.5d halves with the same sign; checked at every 4h bar close; entry at that close, hold 7 days,
          one trade per coin at a time, next entry >= 1 day after the exit;
  filter  trailing 30 complete UTC days of turnover >= 20 M USD on each venue, point in time;
  prices  HL leg = HL 4h candles (their depth bounds the period), Binance leg = Binance 4h candles;
  stop    2x per leg: close at the first 4h bar where a leg's high/low went 50 % against it, +1 % penalty;
  costs   2 x (0.045 % HL taker + 0.05 % Binance taker) + 4 x 0.10 % slippage = 0.59 % of one leg's notional;
  gate    MDE = 2.8 * SD / sqrt(n weeks) <= 0.5 % before any mean is printed; otherwise descriptive only;
  H1      mean net >= +0.4 % per trade and week-cluster bootstrap 95 % CI > 0, >= 120 trades, >= 25 weeks, >= 15 coins.
Control: the same rule on Bybit - Binance, on coins listed on all three venues, same weeks.

Network only through the runtime providers. Hyperliquid info calls go through ``rt.hyperliquid._info`` because the
public helpers upper-case coin names, which breaks ``kPEPE``-style tickers. Pacing: HL ~1000 weight / min.

Usage:
    uv run python scripts/verify_hl_binance_spread.py fetch --cache-dir ~/.cache/tradedesk-agents/hl_binance_spread [--sample 3]
    uv run python scripts/verify_hl_binance_spread.py report --cache-dir ... [--gate-only] [--out report.md]
    uv run python scripts/verify_hl_binance_spread.py fetch1h --cache-dir ...   # after report: 1h bars for the stop check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.dated_basis import FundingSeries
from tradedesk.analytics.funding_spread import (
    DAY_MS,
    HOUR_MS,
    Leg,
    card_mde,
    cluster_bootstrap_ci,
    cluster_bootstrap_diff,
    entry_direction,
    match_tickers,
    normalize_asset,
    pooled_ar1,
    price_ratio,
    quantile,
    spread_signal,
    trade_pnl,
    trailing_turnover,
    walk_pair,
    week_of,
    weekly_spreads,
)
from tradedesk.server import build_server

HL, BIN, BYB = "hyperliquid", "binanceusdm", "bybit"
FUND_START = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)
DAILY_START = int(datetime(2024, 4, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 14, tzinfo=UTC).timestamp() * 1000)
H4 = 4 * HOUR_MS
GAP_HL, GAP_CEX = 2 * HOUR_MS, 9 * HOUR_MS
TOP20 = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
         "TON", "DOT", "LTC", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "WLD"}  # fmt: skip

# pre-registered parameters
THRESHOLD = 0.25
MIN_TURNOVER = 20e6
COST = 2 * (0.00045 + 0.0005) + 4 * 0.001  # 0.59 %
COST_CTRL = 2 * (0.0005 + 0.00055) + 4 * 0.001  # 0.61 %
STOP, PENALTY = 0.5, 0.01
GATE_MDE, BAR_MEAN = 0.005, 0.004


def ymd(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def fname(*parts: str) -> str:
    return "_".join(p.replace("/", "-").replace(":", "_") for p in parts) + ".json"


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


# ----------------------------------------------------------------------------- fetching


class Pacer:
    """Spend at most ``per_min`` request weight per minute (Hyperliquid limits 1200 weight / min per IP)."""

    def __init__(self, per_min: float) -> None:
        self.per_min, self.next_ok, self.lock = per_min, 0.0, asyncio.Lock()

    async def take(self, weight: float) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next_ok > now:
                await asyncio.sleep(self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + weight * 60.0 / self.per_min


async def retry(go: Any, what: str, tries: int = 6) -> Any:
    for k in range(tries):
        try:
            return await go()
        except Exception as e:  # noqa: BLE001
            wait = 20 * (k + 1)
            print(f"  retry {what} in {wait}s after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError(f"giving up on {what}")


class Fetcher:
    def __init__(self, rt: Any, cache: Path) -> None:
        self.rt, self.cache = rt, cache
        self.hl_pace = Pacer(1100.0)
        self.bin_fund_pace = Pacer(400.0 / 5.0)  # Binance funding endpoint: 500 requests / 5 min per IP
        self.weight = 0.0

    async def hl(self, payload: dict[str, Any], weight: float) -> Any:
        await self.hl_pace.take(weight)
        self.weight += weight
        return (await retry(lambda: self.rt.hyperliquid._info(payload, ttl=0), f"hl {payload.get('type')}")).data

    async def hl_candles(self, coin: str, tf: str, start: int, end: int) -> list[dict[str, Any]]:
        n_est = (end - start) / {"1d": DAY_MS, "4h": H4, "1h": HOUR_MS}[tf]
        rows = await self.hl(
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": start, "endTime": end}},
            20 + min(n_est, 5000) / 60,
        )
        return [
            {
                "ts": int(r["t"]),
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": float(r["v"]),
            }
            for r in rows or []
        ]

    async def hl_funding(self, coin: str) -> dict[str, Any]:
        out: dict[int, float] = {}
        start, complete = FUND_START, False
        for _ in range(300):
            rows = await self.hl({"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": END}, 45)
            batch = [r for r in rows or [] if r.get("time") is not None]
            for r in batch:
                out[int(r["time"])] = float(r["fundingRate"])
            if len(batch) < 500:
                complete = True
                break
            nxt = max(int(r["time"]) for r in batch) + 1
            if nxt <= start:
                break
            start = nxt
        return {"rows": [[t, v] for t, v in sorted(out.items()) if t <= END], "complete": complete}

    async def cex_candles(self, venue: str, sym: str, tf: str, start: int, end: int) -> list[dict[str, Any]]:
        step = {"1d": DAY_MS, "4h": H4, "1h": HOUR_MS}[tf]
        page = 1500 if venue == BIN else 1000
        rows: dict[int, list[float]] = {}
        since = start
        while since < end:
            until = min(end, since + page * step) - 1
            f = await retry(
                lambda s=since, u=until: self.rt.exchanges.ohlcv(venue, sym, tf, page, since_ms=s, until_ms=u), f"{venue} {sym} {tf}"
            )
            batch = [b for b in (f.data or []) if isinstance(b, (list, tuple)) and len(b) >= 6]
            for b in batch:
                rows[int(b[0])] = b
            # a venue may return fewer bars than asked (Binance caps at 1000): continue after the last bar received
            last = max((int(b[0]) for b in batch), default=None)
            since = last + step if last is not None and last + step > since else until + 1
            await asyncio.sleep(0.15)
        return [
            {"ts": t, "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4]), "volume": float(b[5] or 0.0)}
            for t, b in sorted(rows.items())
            if t + step <= end
        ]

    async def cex_funding(self, venue: str, sym: str) -> dict[str, Any]:
        if venue == BYB:
            f = await retry(
                lambda: self.rt.exchanges.funding_rate_history_range(venue, sym, FUND_START, END, max_pages=600), f"{venue} {sym} funding"
            )
            d = f.data
            return {
                "rows": [
                    [int(x["timestamp"]), float(x["fundingRate"])]
                    for x in d["rows"]
                    if x.get("fundingRate") is not None and int(x["timestamp"]) <= END
                ],
                "complete": bool(d["complete"]),
            }
        out: dict[int, float] = {}
        since, complete = FUND_START, False
        for _ in range(400):
            await self.bin_fund_pace.take(1)
            f = await retry(lambda s=since: self.rt.exchanges.funding_rate_history(venue, sym, s, 1000), f"{venue} {sym} funding")
            batch = [x for x in (f.data or []) if x.get("timestamp") is not None and x.get("fundingRate") is not None]
            for x in batch:
                out[int(x["timestamp"])] = float(x["fundingRate"])
            if not batch or len(batch) < 1000 or max(int(x["timestamp"]) for x in batch) >= END:
                complete = True
                break
            since = max(int(x["timestamp"]) for x in batch) + 1
        return {"rows": [[t, v] for t, v in sorted(out.items()) if t <= END], "complete": complete}

    async def cached(self, name: str, go: Any) -> Any:
        p = self.cache / name
        if p.exists():
            return rd(p)
        data = await go()
        wr(p, data)
        return data


async def build_universe(fx: Fetcher) -> dict[str, Any]:
    """HL universe (delisted included), Binance USDT-M crypto perps (inactive included), Bybit linear USDT perps."""

    async def go() -> dict[str, Any]:
        meta = await fx.hl({"type": "metaAndAssetCtxs"}, 20)
        hl = {u["name"]: {"delisted": bool(u.get("isDelisted")), "max_lev": u.get("maxLeverage")} for u in meta[0]["universe"]}
        bm = (await fx.rt.exchanges.markets(BIN)).data or {}
        bn: dict[str, Any] = {}
        tradfi = []
        for sym, m in bm.items():
            info = (m or {}).get("info") or {}
            if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL":
                continue
            if info.get("underlyingType") != "COIN":
                tradfi.append(f"{m.get('base')}:{info.get('underlyingType')}")
                continue
            bn[m["base"]] = {
                "symbol": sym,
                "active": m.get("active") is not False,
                "status": info.get("status"),
                "onboard": info.get("onboardDate"),
            }
        ym = (await fx.rt.exchanges.markets(BYB)).data or {}
        by = {
            m["base"]: {"symbol": sym, "active": m.get("active") is not False}
            for sym, m in ym.items()
            if m and m.get("swap") and m.get("linear") and sym.endswith("/USDT:USDT")
        }
        return {"hl": hl, "binance": bn, "bybit": by, "binance_non_coin": sorted(tradfi), "fetched_at": int(time.time() * 1000)}

    return await fx.cached("universe.json", go)


def pairings(uni: dict[str, Any]) -> dict[str, Any]:
    hl = {n: normalize_asset(n, HL) for n in uni["hl"]}
    bn = {n: normalize_asset(n, BIN) for n in uni["binance"]}
    by = {n: normalize_asset(n, BYB) for n in uni["bybit"]}
    hb, amb_hb = match_tickers(hl, bn)
    bb, amb_bb = match_tickers(bn, by)
    return {"hl": hl, "bn": bn, "by": by, "hl_bin": hb, "bin_byb": bb, "ambiguous": sorted(set(amb_hb) | set(amb_bb))}


def price_checks(cache: Path, pr: dict[str, Any], uni: dict[str, Any]) -> dict[str, Any]:
    """Median daily-close ratio after multipliers; pairs off by more than 2 % are different instruments."""
    ok_hb, bad_hb, ok_bb, bad_bb, no_data = {}, [], {}, [], []
    for coin, base in pr["hl_bin"].items():
        a, b = cache / fname("hl", coin, "1d"), cache / fname("bin", base, "1d")
        if not (a.exists() and b.exists()):
            continue
        da, db = {x["ts"]: x["close"] for x in rd(a)}, {x["ts"]: x["close"] for x in rd(b)}
        r = price_ratio(da, db, pr["hl"][coin][1], pr["bn"][base][1])
        if r is None:
            no_data.append(coin)
        elif abs(r - 1.0) <= 0.02:
            ok_hb[coin] = base
        else:
            bad_hb.append((coin, base, r))
    for base, bbase in pr["bin_byb"].items():
        a, b = cache / fname("bin", base, "1d"), cache / fname("byb", bbase, "1d")
        if not (a.exists() and b.exists()):
            continue
        da, db = {x["ts"]: x["close"] for x in rd(a)}, {x["ts"]: x["close"] for x in rd(b)}
        r = price_ratio(da, db, pr["bn"][base][1], pr["by"][bbase][1])
        if r is not None and abs(r - 1.0) <= 0.02:
            ok_bb[base] = bbase
        elif r is not None:
            bad_bb.append((base, bbase, r))
    return {"hl_bin": ok_hb, "bad_hl_bin": bad_hb, "bin_byb": ok_bb, "bad_bin_byb": bad_bb, "no_overlap": no_data}


def ever_liquid(da: list[dict[str, Any]], db: list[dict[str, Any]]) -> bool:
    """Whether the point-in-time turnover filter passes on both venues on at least one day of the period
    (coins that never pass can never trade, so their hourly data is not fetched)."""
    t = (FUND_START + 8 * DAY_MS) // DAY_MS * DAY_MS
    while t <= END:
        if trailing_turnover(da, t) >= MIN_TURNOVER and trailing_turnover(db, t) >= MIN_TURNOVER:
            return True
        t += DAY_MS
    return False


def liquid_pairs(cache: Path, good: dict[str, str]) -> dict[str, str]:
    return {c: b for c, b in good.items() if ever_liquid(rd(cache / fname("hl", c, "1d")), rd(cache / fname("bin", b, "1d")))}


async def fetch(cache: Path, sample: int) -> None:
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    t0 = time.perf_counter()
    try:
        uni = await build_universe(fx)
        pr = pairings(uni)
        hb = dict(sorted(pr["hl_bin"].items()))
        if sample:
            keep = [c for c in ("BTC", "kPEPE") if c in hb] + [c for c in hb if c not in ("BTC", "kPEPE")][: max(0, sample - 2)]
            hb = {c: hb[c] for c in keep}
        delisted = sum(1 for c in hb if uni["hl"][c]["delisted"])
        print(
            f"universe: HL {len(uni['hl'])} ({sum(v['delisted'] for v in uni['hl'].values())} delisted), Binance crypto perps {len(uni['binance'])}, "
            f"Bybit {len(uni['bybit'])}; HL<->Binance matched {len(pr['hl_bin'])} (working on {len(hb)}, delisted on HL {delisted}); "
            f"ambiguous {pr['ambiguous']}",
            flush=True,
        )
        byb_of = {b: pr["bin_byb"][b] for b in hb.values() if b in pr["bin_byb"]}
        # phase 1: daily candles for the price check and the turnover filter
        t1 = time.perf_counter()

        async def hl_daily() -> None:
            for i, c in enumerate(hb):
                await fx.cached(fname("hl", c, "1d"), lambda c=c: fx.hl_candles(c, "1d", DAILY_START, END))
                if (i + 1) % 25 == 0:
                    print(f"  hl 1d {i + 1}/{len(hb)}  [{time.perf_counter() - t1:.0f}s]", flush=True)

        async def cex_daily(venue: str, tag: str, bases: dict[str, str], table: str) -> None:
            for b in bases:
                await fx.cached(fname(tag, b, "1d"), lambda b=b: fx.cex_candles(venue, uni[table][b]["symbol"], "1d", DAILY_START, END))

        await asyncio.gather(
            hl_daily(),
            cex_daily(BIN, "bin", {b: b for b in hb.values()}, "binance"),
            cex_daily(BYB, "byb", {v: v for v in byb_of.values()}, "bybit"),
        )
        print(f"phase 1 (daily) done in {time.perf_counter() - t1:.0f}s, HL weight so far {fx.weight:.0f}", flush=True)
        pc = price_checks(cache, pr, uni)
        good = {c: b for c, b in hb.items() if c in pc["hl_bin"]}
        print(
            f"price check: {len(good)} HL<->Binance pairs ok; mismatched {[(c, round(r, 4)) for c, _, r in pc['bad_hl_bin'] if c in hb]}",
            flush=True,
        )
        n_good = len(good)
        good = liquid_pairs(cache, good)
        print(f"turnover filter ever passed on both venues: {len(good)} of {n_good} pairs", flush=True)
        # phase 2: funding and 4h prices
        t2 = time.perf_counter()
        done = Counter()

        async def hl_all() -> None:
            for c in good:
                await fx.cached(fname("hl", c, "4h"), lambda c=c: fx.hl_candles(c, "4h", FUND_START, END))
                await fx.cached(fname("hl", c, "funding"), lambda c=c: fx.hl_funding(c))
                done["hl"] += 1
                if done["hl"] % 10 == 0 or done["hl"] == len(good):
                    el = time.perf_counter() - t2
                    print(
                        f"  hl funding+4h {done['hl']}/{len(good)}  [{el:.0f}s, ~{el / done['hl'] * (len(good) - done['hl']) / 60:.0f} min left]",
                        flush=True,
                    )

        async def bin_all() -> None:
            for b in good.values():
                sym = uni["binance"][b]["symbol"]
                await fx.cached(fname("bin", b, "funding"), lambda s=sym: fx.cex_funding(BIN, s))
                # "4hv2": the first fetch stepped 1500-bar windows while Binance returns 1000, leaving holes
                await fx.cached(fname("bin", b, "4hv2"), lambda s=sym: fx.cex_candles(BIN, s, "4h", FUND_START, END))
                done["bin"] += 1
                if done["bin"] % 25 == 0:
                    print(f"  binance {done['bin']}/{len(good)}  [{time.perf_counter() - t2:.0f}s]", flush=True)

        async def byb_all() -> None:
            sem = asyncio.Semaphore(4)

            async def one(bb: str) -> None:
                sym = uni["bybit"][bb]["symbol"]
                async with sem:
                    await fx.cached(fname("byb", bb, "funding"), lambda: fx.cex_funding(BYB, sym))
                    await fx.cached(fname("byb", bb, "4h"), lambda: fx.cex_candles(BYB, sym, "4h", FUND_START, END))
                done["byb"] += 1
                if done["byb"] % 25 == 0:
                    print(f"  bybit {done['byb']}  [{time.perf_counter() - t2:.0f}s]", flush=True)

            await asyncio.gather(*[one(byb_of[b]) for b in good.values() if b in byb_of and b in pc["bin_byb"]])

        await asyncio.gather(hl_all(), bin_all(), byb_all())
        print(
            f"phase 2 (funding + 4h) done in {time.perf_counter() - t2:.0f}s; HL weight {fx.weight:.0f}; total {time.perf_counter() - t0:.0f}s",
            flush=True,
        )
    finally:
        await rt.aclose()
    print("FETCH DONE", flush=True)


async def fetch_1h(cache: Path) -> None:
    """1h bars on both legs for coins with primary trades inside HL's 1h depth (stop-check sensitivity)."""
    p = cache / "trades_primary.json"
    trades = rd(p)
    uni = rd(cache / "universe.json")
    first_1h = END - 200 * DAY_MS
    coins = sorted({t["coin"] for t in trades if t["open_ts"] >= first_1h})
    _, rt = build_server()
    fx = Fetcher(rt, cache)
    try:
        for i, c in enumerate(coins):
            b = next(t["base"] for t in trades if t["coin"] == c)
            await fx.cached(fname("hl", c, "1h"), lambda c=c: fx.hl_candles(c, "1h", first_1h - 8 * DAY_MS, END))
            await fx.cached(
                fname("bin", b, "1h"), lambda b=b: fx.cex_candles(BIN, uni["binance"][b]["symbol"], "1h", first_1h - 8 * DAY_MS, END)
            )
            print(f"  1h {i + 1}/{len(coins)} {c}", flush=True)
    finally:
        await rt.aclose()
    print("FETCH1H DONE", flush=True)


# ----------------------------------------------------------------------------- analysis


def fseries(obj: dict[str, Any]) -> FundingSeries:
    # HL stamps payments a few ms after the hour (e.g. ...00:00.073); snap to the minute so the payment for hour t is
    # at t: it then belongs to the signal at t and not to a trade opened at t.
    return FundingSeries([(int(t) // 60_000 * 60_000, float(r)) for t, r in obj["rows"]])


class Pair:
    def __init__(self, coin: str, base: str, a: Leg, b: Leg, da: list[dict[str, Any]], db: list[dict[str, Any]], delisted: bool) -> None:
        self.coin, self.base, self.a, self.b, self.da, self.db, self.delisted = coin, base, a, b, da, db, delisted
        self.asset = coin[1:] if coin.startswith("k") and coin[1:2].isupper() else coin
        self._liq: dict[int, bool] = {}

    def liquid(self, t: int) -> bool:
        # the set of closed daily bars is the same for every t within one UTC day
        day = t // DAY_MS
        if day not in self._liq:
            self._liq[day] = trailing_turnover(self.da, t) >= MIN_TURNOVER and trailing_turnover(self.db, t) >= MIN_TURNOVER
        return self._liq[day]


def load_pairs(cache: Path) -> tuple[dict[str, Pair], dict[str, Pair], dict[str, Any]]:
    """HL<->Binance pairs and Bybit<->Binance control pairs (A = HL / Bybit, B = Binance)."""
    uni = rd(cache / "universe.json")
    pr = pairings(uni)
    pc = price_checks(cache, pr, uni)
    liquid = liquid_pairs(cache, pc["hl_bin"])
    info: dict[str, Any] = {
        "uni": uni,
        "pr": pr,
        "pc": pc,
        "depth": {},
        "missing": [],
        "never_liquid": sorted(set(pc["hl_bin"]) - set(liquid)),
    }
    hlb: dict[str, Pair] = {}
    ctrl: dict[str, Pair] = {}
    for coin, base in sorted(liquid.items()):
        paths = {
            k: cache / fname(*k.split("|")) for k in (f"hl|{coin}|funding", f"hl|{coin}|4h", f"bin|{base}|funding", f"bin|{base}|4hv2")
        }
        if not all(p.exists() for p in paths.values()):
            info["missing"].append(coin)
            continue
        fh, fb = rd(paths[f"hl|{coin}|funding"]), rd(paths[f"bin|{base}|funding"])
        bh, bb = rd(paths[f"hl|{coin}|4h"]), rd(paths[f"bin|{base}|4hv2"])
        dh, dbn = rd(cache / fname("hl", coin, "1d")), rd(cache / fname("bin", base, "1d"))
        info["depth"][coin] = {
            "hl_first": fh["rows"][0][0] if fh["rows"] else None,
            "hl_complete": fh["complete"],
            "bin_first": fb["rows"][0][0] if fb["rows"] else None,
            "bin_complete": fb["complete"],
            "hl_4h_first": bh[0]["ts"] if bh else None,
            "hl_n": len(fh["rows"]),
            "hl_max_abs": max((abs(r) for _, r in fh["rows"]), default=0.0),
            "holes_4h": sum(1 for bars in (bh, bb) for x, y in zip(bars, bars[1:], strict=False) if y["ts"] - x["ts"] > H4),
        }
        if not (fh["rows"] and fb["rows"] and bh and bb):
            continue
        hlb[coin] = Pair(coin, base, Leg(fseries(fh), bh, H4), Leg(fseries(fb), bb, H4), dh, dbn, uni["hl"][coin]["delisted"])
        bbase = pc["bin_byb"].get(base)
        if bbase:
            py, pyd = cache / fname("byb", bbase, "funding"), cache / fname("byb", bbase, "4h")
            if py.exists() and pyd.exists():
                fy, by_ = rd(py), rd(pyd)
                if fy["rows"] and by_:
                    ctrl[coin] = Pair(coin, base, Leg(fseries(fy), by_, H4), hlb[coin].b, rd(cache / fname("byb", bbase, "1d")), dbn, False)
                    info["depth"][coin]["byb_complete"] = fy["complete"]
    return hlb, ctrl, info


def run_rule(pairs: dict[str, Pair], *, cost: float, gap_a: int = GAP_HL, **kw: Any) -> list[dict[str, Any]]:
    out = []
    a_px = kw.pop("a_from_b", False)
    for coin, p in pairs.items():
        trs = walk_pair(
            p.a,
            p.b,
            start_ms=FUND_START,
            end_ms=END,
            threshold=THRESHOLD,
            gap_a_ms=gap_a,
            gap_b_ms=GAP_CEX,
            eligible=p.liquid,
            cost=cost,
            stop_penalty=PENALTY,
            a_prices=p.b if a_px else None,
            **kw,
        )
        for t in trs:
            t.update(coin=coin, base=p.base, asset=p.asset, week=week_of(t["open_ts"]), delisted=p.delisted)
        out.extend(trs)
    return sorted(out, key=lambda t: t["open_ts"])


def gate(trades: list[dict[str, Any]]) -> dict[str, Any]:
    xs = [t["net"] for t in trades]
    weeks = {t["week"] for t in trades}
    sd = statistics.stdev(xs) if len(xs) > 1 else float("nan")
    m = card_mde(sd, len(weeks)) if len(xs) > 1 else float("inf")
    return {"n": len(xs), "weeks": len(weeks), "coins": len({t["coin"] for t in trades}), "sd": sd, "mde": m, "pass": m <= GATE_MDE}


def pct(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


DHDR = "| Варіант | угод | тижнів | монет | p10 | p25 | медіана | p75 | p90 | частка > 0 | стопів |\n|---|---|---|---|---|---|---|---|---|---|---|"
MHDR = "| Варіант | угод | тижнів | монет | середнє | 95% CI (кластери-тижні) | медіана | частка > 0 | funding | ціна | витрати | стопів |\n|---|---|---|---|---|---|---|---|---|---|---|---|"


def drow(label: str, trs: list[dict[str, Any]]) -> str:
    if not trs:
        return f"| {label} | 0 | — | — | — | — | — | — | — | — | — |"
    xs = [t["net"] for t in trs]
    return (
        f"| {label} | {len(xs)} | {len({t['week'] for t in trs})} | {len({t['coin'] for t in trs})} | "
        + " | ".join(pct(quantile(xs, q)) for q in (0.10, 0.25))
        + f" | {pct(statistics.median(xs))} | "
        + " | ".join(pct(quantile(xs, q)) for q in (0.75, 0.90))
        + f" | {sum(1 for x in xs if x > 0) / len(xs):.0%} | {sum(1 for t in trs if t['stopped'])} |"
    )


def mrow(label: str, trs: list[dict[str, Any]]) -> str:
    if len(trs) < 2:
        return f"| {label} | {len(trs)} | — | — | — | — | — | — | — | — | — | — |"
    xs = [t["net"] for t in trs]
    ci = cluster_bootstrap_ci(xs, [t["week"] for t in trs])
    f = statistics.fmean(t["funding"] for t in trs)
    p = statistics.fmean(t["price"] for t in trs)
    c = statistics.fmean(t["cost"] for t in trs)
    return (
        f"| {label} | {len(xs)} | {len({t['week'] for t in trs})} | {len({t['coin'] for t in trs})} | {pct(statistics.fmean(xs))} | "
        f"{'—' if ci is None else f'{pct(ci[0])} … {pct(ci[1])}'} | {pct(statistics.median(xs))} | {sum(1 for x in xs if x > 0) / len(xs):.0%} | "
        f"{pct(f)} | {pct(p)} | {pct(-c)} | {sum(1 for t in trs if t['stopped'])} |"
    )


def signal_frequency(pairs: dict[str, Pair], gap_a: int) -> dict[str, Any]:
    """Share of liquid coin x 4h points with a pre-registered entry signal (|S7| >= 25 %, same-sign halves) and >= 31 %."""
    n = n25 = n31 = 0
    by_coin: Counter[str] = Counter()
    for coin, p in pairs.items():
        t = (FUND_START + 8 * DAY_MS) // H4 * H4
        while t <= END:
            if p.a.price(t) is not None and p.b.price(t) is not None:
                sig = spread_signal(p.a.funding, p.b.funding, t, gap_a_ms=gap_a, gap_b_ms=GAP_CEX)
                if sig is not None and p.liquid(t):
                    n += 1
                    if entry_direction(sig, THRESHOLD):
                        n25 += 1
                        by_coin[coin] += 1
                    if entry_direction(sig, 0.31):
                        n31 += 1
            t += H4
    return {"n": n, "f25": n25 / n if n else None, "f31": n31 / n if n else None, "coins25": len(by_coin), "top": by_coin.most_common(8)}


def report(cache: Path, gate_only: bool) -> str:
    hlb, ctrl, info = load_pairs(cache)
    uni, pr, pc = info["uni"], info["pr"], info["pc"]
    L: list[str] = ["# Спред funding Hyperliquid ↔ Binance USDT-M: перевірка картки IDEA-20261014\n"]
    L.append(
        f"Період funding {ymd(FUND_START)} … {ymd(END)}; ціни 4h; знімок всесвіту {ymd(uni['fetched_at'])}. Доходи — частки номіналу однієї ноги "
        "(обидві ноги мають однаковий номінал на вході). Капітал при 2x на ногу ≈ номіналу однієї ноги ×1 (дві маржі по 50 %) + буфер на міст.\n"
    )
    # ---- data and sanity
    L.append("## Дані і перевірки\n")
    hl_del = [c for c in pc["hl_bin"] if uni["hl"][c]["delisted"]]
    L.append(
        f"- Всесвіт HL: {len(uni['hl'])} перпів, з них делістнуто {sum(v['delisted'] for v in uni['hl'].values())}. Binance USDT-M крипто-перпів: "
        f"{len(uni['binance'])} (неактивних {sum(1 for v in uni['binance'].values() if not v['active'])}); виключено не-COIN (TradFi тощо): "
        f"{len(uni['binance_non_coin'])}. Bybit лінійних USDT-перпів: {len(uni['bybit'])}."
    )
    L.append(
        f"- Зіставлено за активом HL ↔ Binance: {len(pr['hl_bin'])}; неоднозначні активи (виключено): {', '.join(pr['ambiguous']) or 'немає'}."
    )
    L.append(
        f"- Перевірка ціною (медіана денних закриттів після множника, допуск 2 %): пройшло {len(pc['hl_bin'])}; різні інструменти під однаковим активом: "
        + (", ".join(f"{c}↔{b} ({r:.4f})" for c, b, r in pc["bad_hl_bin"]) or "немає")
        + f"; без спільних днів (≥ 20): {', '.join(pc['no_overlap']) or 'немає'}."
    )
    L.append(
        f"- Делістнуті на HL монети з даними, що пройшли перевірку: {len(hl_del)} ({', '.join(sorted(hl_del)) or '—'}); у вибірці з повними даними: "
        f"{sum(1 for p in hlb.values() if p.delisted)}."
    )
    L.append(
        f"- Контроль Bybit: пар Binance ↔ Bybit з перевіркою ціною {len(pc['bin_byb'])}; з них на всіх трьох площадках і з даними: {len(ctrl)}. "
        f"Не пройшли ціну: {', '.join(f'{b}↔{y} ({r:.4f})' for b, y, r in pc['bad_bin_byb']) or 'немає'}."
    )
    nl = info["never_liquid"]
    L.append(
        f"- Ніколи не проходили фільтр обігу (≥ 20 млн USD за 30 днів на обох біржах одночасно) за весь період: {len(nl)} пар; "
        f"їх годинні дані не завантажено, бо угод за правилом бути не може. Делістнутих на HL серед них: {sum(1 for c in nl if uni['hl'][c]['delisted'])}."
    )
    dep = info["depth"]
    inc = [c for c, d in dep.items() if not (d["hl_complete"] and d["bin_complete"] and d.get("byb_complete", True))]
    L.append(
        f"- Повнота історії funding (`complete`): неповних монет {len(inc)} ({', '.join(inc) or '—'}); без даних: {', '.join(info['missing']) or '—'}."
    )
    holes = {c: d["holes_4h"] for c, d in dep.items() if d["holes_4h"]}
    L.append(
        f"- Дірки в 4h-рядах (HL або Binance, кількість розривів > 4 год): {len(holes)} монет; найбільше: {sorted(holes.items(), key=lambda x: -x[1])[:8]}."
    )
    h4 = Counter(ymd(d["hl_4h_first"]) for d in dep.values())
    L.append(f"- Перша 4h-свічка HL (глибина ціни ноги HL): найчастіше {h4.most_common(3)}.")
    # consistency against known numbers: HL baseline 0.01 %/8h = 0.00125 %/h; Binance 0.01 % per 8h
    hlr: Counter[float] = Counter()
    bnr: Counter[float] = Counter()
    ivals: Counter[int] = Counter()
    for p in hlb.values():
        hlr.update(round(r, 9) for r in p.a.funding.rate)
        bnr.update(round(r, 9) for r in p.b.funding.rate)
        ts = p.b.funding.ts
        ivals.update(round((b - a) / HOUR_MS) for a, b in zip(ts, ts[1:], strict=False))
    L.append(
        f"- Еталон: модальна ставка HL {hlr.most_common(1)[0][0]:.7f}/год (очікується 0.0000125 = 0.01 %/8 год), частка {hlr.most_common(1)[0][1] / sum(hlr.values()):.0%}; "
        f"модальна ставка Binance {bnr.most_common(1)[0][0]:.6f} (очікується 0.0001), частка {bnr.most_common(1)[0][1] / sum(bnr.values()):.0%}."
    )
    L.append(f"- Інтервали Binance між виплатами (год → кількість): {dict(sorted(ivals.most_common(6)))}.")
    capped = sorted(c for c, d in dep.items() if d["hl_max_abs"] >= 0.01)
    L.append(f"- Монети, де funding HL торкався ≥ 1 %/год: {', '.join(capped) or 'немає'}.")
    L.append("\nГлибина історії по монетах (перший запис funding HL / Binance, перша 4h-свічка HL, повнота):\n")
    L.append(
        "| Монета HL | Binance | funding HL з | funding Binance з | 4h HL з | complete HL/BIN/BYB | делістнута на HL |\n|---|---|---|---|---|---|---|"
    )
    for c, d in sorted(dep.items()):
        L.append(
            f"| {c} | {pc['hl_bin'][c]} | {ymd(d['hl_first'])} | {ymd(d['bin_first'])} | {ymd(d['hl_4h_first'])} | "
            f"{d['hl_complete']}/{d['bin_complete']}/{d.get('byb_complete', '—')} | {'так' if uni['hl'][c]['delisted'] else ''} |"
        )

    # ---- primary and gate
    prim = run_rule(hlb, cost=COST, stop_move=STOP)
    wr(cache / "trades_primary.json", prim)
    g = gate(prim)
    L.append("\n## Ворота потужності (друкуються до будь-яких середніх)\n")
    L.append(
        f"Основний варіант: угод {g['n']}, тижнів-кластерів {g['weeks']}, монет {g['coins']}; SD чистого доходу за угоду {pct(g['sd'])}; "
        f"MDE = 2.8 · SD / √n_тижнів = {pct(g['mde'])} (поріг {pct(GATE_MDE)}). Мінімум вибірки (≥ 120 угод, ≥ 25 тижнів, ≥ 15 монет): "
        f"{'так' if g['n'] >= 120 and g['weeks'] >= 25 and g['coins'] >= 15 else 'ні'}. **Ворота: {'ПРОЙДЕНО' if g['pass'] else 'НЕ ПРОЙДЕНО'}.**\n"
    )
    if gate_only:
        return "\n".join(L)
    descriptive = not (g["pass"] and g["n"] >= 120 and g["weeks"] >= 25 and g["coins"] >= 15)

    # ---- pre-registered variants
    ctrl_coins = set(ctrl)
    variants: list[tuple[str, list[dict[str, Any]]]] = [
        ("**Основний** (поріг 25 %, 7 д, стоп 2x, витрати 0.59 %)", prim),
        ("витрати ×2 (1.18 %)", run_rule(hlb, cost=2 * COST, stop_move=STOP)),
        ("оптимістично: maker на HL (0.43 %)", run_rule(hlb, cost=2 * (0.00015 + 0.0005) + 2 * 0.0005 + 2 * 0.001, stop_move=STOP)),
        ("проковзування 0.30 %/нога/сторона (1.39 %)", run_rule(hlb, cost=2 * (0.00045 + 0.0005) + 4 * 0.003, stop_move=STOP)),
        ("міст USDC: вхід на 4 год пізніше + 0.02 % фіксовано", run_rule(hlb, cost=COST + 0.0002, stop_move=STOP, entry_delay_ms=H4)),
        ("нога HL за цінами Binance (без розбіжності цін)", run_rule(hlb, cost=COST, stop_move=STOP, a_from_b=True)),
        ("без стопу", run_rule(hlb, cost=COST, stop_move=None)),
    ]
    prim_by: dict[str, list[dict[str, Any]]] = {}
    for t in prim:
        prim_by.setdefault(t["coin"], []).append(t)
    contrib = sorted(((sum(t["net"] for t in v), c) for c, v in prim_by.items()), reverse=True)
    top5 = [c for _, c in contrib[:5]]
    mid = (FUND_START + END) // 2
    subsets: list[tuple[str, list[dict[str, Any]]]] = [
        ("мейджори (топ-20)", [t for t in prim if t["asset"] in TOP20]),
        ("решта", [t for t in prim if t["asset"] not in TOP20]),
        (f"без топ-5 монет за внеском ({', '.join(top5)})", [t for t in prim if t["coin"] not in top5]),
        ("без монет, де HL торкався ≥ 1 %/год", [t for t in prim if t["coin"] not in capped]),
        ("без делістнутих на HL", [t for t in prim if not t["delisted"]]),
        ("лише делістнуті на HL", [t for t in prim if t["delisted"]]),
        (f"перша половина (до {ymd(mid)})", [t for t in prim if t["open_ts"] < mid]),
        (f"друга половина (з {ymd(mid)})", [t for t in prim if t["open_ts"] >= mid]),
        ("останні 6 місяців", [t for t in prim if t["open_ts"] >= END - 182 * DAY_MS]),
        ("шорт на HL", [t for t in prim if t["dir"] == -1]),
        ("шорт на Binance", [t for t in prim if t["dir"] == 1]),
        ("без угод, обрізаних кінцем даних", [t for t in prim if not t["truncated"]]),
    ]
    # control on the three-venue intersection and the same weeks
    hl3 = [t for t in prim if t["coin"] in ctrl_coins]
    ctl = run_rule(ctrl, cost=COST_CTRL, gap_a=GAP_CEX, stop_move=STOP)
    weeks = {t["week"] for t in hl3} | {t["week"] for t in ctl}
    ctl = [t for t in ctl if t["week"] in weeks]
    control: list[tuple[str, list[dict[str, Any]]]] = [
        ("HL ↔ Binance на перетині трьох площадок", hl3),
        ("контроль Bybit ↔ Binance, те саме правило (витрати 0.61 %)", ctl),
    ]
    # 1h stop check where HL 1h exists
    oneh = []
    for c, p in hlb.items():
        a1, b1 = cache / fname("hl", c, "1h"), cache / fname("bin", p.base, "1h")
        if a1.exists() and b1.exists():
            oneh.append((c, p, rd(a1), rd(b1)))
    if oneh:
        first_1h = END - 200 * DAY_MS
        t4 = [t for t in prim if t["open_ts"] >= first_1h and t["coin"] in {c for c, *_ in oneh}]
        t1 = []
        for c, p, a1, b1 in oneh:
            la, lb = Leg(p.a.funding, a1, HOUR_MS), Leg(p.b.funding, b1, HOUR_MS)
            for t in prim_by.get(c, []):
                if t["open_ts"] >= first_1h:
                    r = trade_pnl(la, lb, t["open_ts"], t["dir"], cost=COST, stop_move=STOP, stop_penalty=PENALTY)
                    if r is not None:
                        r.update(coin=c, week=t["week"])
                        t1.append(r)
        subsets += [("ті самі угоди, стоп за 4h (останні 200 днів)", t4), ("ті самі угоди, стоп за 1h (останні 200 днів)", t1)]

    L.append("## Розподіл чистого доходу за угоду (на номінал однієї ноги)\n")
    L.append(DHDR)
    for label, trs in variants + subsets + control:
        L.append(drow(label, trs))
    if not descriptive:
        L.append("\n## Середні (ворота пройдено)\n")
        L.append(MHDR)
        for label, trs in variants + subsets + control:
            L.append(mrow(label, trs))
        d = cluster_bootstrap_diff([t["net"] for t in hl3], [t["week"] for t in hl3], [t["net"] for t in ctl], [t["week"] for t in ctl])
        L.append(
            f"\nHL ↔ Binance мінус контроль (перетин, ті самі тижні): {pct(d['diff'])}, 95% CI {d['ci'] and (pct(d['ci'][0]) + ' … ' + pct(d['ci'][1]))}."
        )
    else:
        L.append("\nВорота або мінімум вибірки не пройдено: середні, CI і порівняння середніх не друкуються (умова 7 критика).")

    # ---- descriptive: frequency and persistence
    L.append("\n## Описове: частота сигналу і персистентність (H2)\n")
    fh = signal_frequency(hlb, GAP_HL)
    fh3 = signal_frequency({c: hlb[c] for c in ctrl_coins}, GAP_HL)
    fc = signal_frequency(ctrl, GAP_CEX)
    L.append("| Пара | точок (монета × 4h, ліквідні) | частка з сигналом ≥ 25 % | ≥ 31 % | монет із сигналом |\n|---|---|---|---|---|")
    for label, f in (("HL ↔ Binance, усі", fh), ("HL ↔ Binance, перетин трьох", fh3), ("Bybit ↔ Binance, перетин трьох", fc)):
        L.append(f"| {label} | {f['n']} | {pct(f['f25'])} | {pct(f['f31'])} | {f['coins25']} |")
    L.append(f"\nНайчастіше з сигналом (HL ↔ Binance, точок): {fh['top']}.\n")
    wk = list(range(week_of(FUND_START) + 1, week_of(END)))
    s_hl, s_by = [], []
    for c in ctrl_coins:
        a = weekly_spreads(hlb[c].a.funding, hlb[c].b.funding, wk, gap_a_ms=GAP_HL, gap_b_ms=GAP_CEX)
        b = weekly_spreads(ctrl[c].a.funding, ctrl[c].b.funding, wk, gap_a_ms=GAP_CEX, gap_b_ms=GAP_CEX)
        common = a.keys() & b.keys()
        s_hl.append({w: a[w] for w in common})
        s_by.append({w: b[w] for w in common})
    for rank in (False, True):
        r1, n1 = pooled_ar1(s_hl, rank=rank)
        r2, n2 = pooled_ar1(s_by, rank=rank)
        name = "Спірмен (чутливість)" if rank else "Пірсон (основний)"
        diff = None if r1 is None or r2 is None else r1 - r2
        L.append(
            f"- AR(1) тижневого спреду, {name}: HL ↔ Binance {r1 and f'{r1:.3f}'} (пар тижнів {n1}), Bybit ↔ Binance {r2 and f'{r2:.3f}'} ({n2}); "
            f"різниця {diff and f'{diff:+.3f}'} (поріг H2 ≥ +0.10)."
        )
    absd = [abs(v) for s in s_hl for v in s.values()]
    absc = [abs(v) for s in s_by for v in s.values()]
    if absd and absc:
        L.append(
            f"- |тижневий спред| медіана / p90: HL ↔ Binance {pct(statistics.median(absd))} / {pct(quantile(absd, 0.9))}; "
            f"Bybit ↔ Binance {pct(statistics.median(absc))} / {pct(quantile(absc, 0.9))} (річних)."
        )

    # ---- risk
    L.append("\n## Ризик основного варіанта\n")
    if prim:
        adv = [t["max_adverse"] for t in prim]
        L.append(
            f"- Найбільший рух проти ноги за угоду: медіана {quantile(adv, 0.5):.0%}, p90 {quantile(adv, 0.9):.0%}, максимум {max(adv):.0%}."
        )
        L.append(
            f"- Закрито стопом (нога ≥ 50 % проти): {sum(t['stopped'] for t in prim)} з {len(prim)}; обрізано кінцем даних: {sum(t['truncated'] for t in prim)}."
        )
        worst = min(prim, key=lambda t: t["net"])
        best = max(prim, key=lambda t: t["net"])
        L.append(
            f"- Найгірша угода {worst['coin']} {ymd(worst['open_ts'])} {pct(worst['net'])}; найкраща {best['coin']} {ymd(best['open_ts'])} {pct(best['net'])}."
        )
        stops = [t for t in prim if t["stopped"]]
        if stops:
            w = min(stops, key=lambda t: t["net"])
            L.append(f"- Найбільша втрата за стопом: {w['coin']} {ymd(w['open_ts'])} {pct(w['net'])}.")
        cnt = Counter(t["coin"] for t in prim)
        L.append(f"- Угод на монету, найбільше: {cnt.most_common(10)}.")
        L.append(f"- Шорт на HL / шорт на Binance: {sum(t['dir'] == -1 for t in prim)} / {sum(t['dir'] == 1 for t in prim)}.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "fetch1h", "report"])
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache/tradedesk-agents/hl_binance_spread"))
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--gate-only", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.cmd == "fetch":
        asyncio.run(fetch(cache, a.sample))
    elif a.cmd == "fetch1h":
        asyncio.run(fetch_1h(cache))
    else:
        rep = report(cache, a.gate_only)
        print(rep)
        if a.out:
            Path(a.out).write_text(rep + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
