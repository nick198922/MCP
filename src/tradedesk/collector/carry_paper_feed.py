"""Live market-data feed for the paper carry engine: read-only public data through ``rt.exchanges`` (ccxt providers).

Universe rules are the backtest scripts' (``scripts/verify_carry_alts.py`` / ``verify_carry_perp_perp.py``):
Binance USDT perpetuals on COIN underlyings; A additionally needs a Binance USDT spot market (1000x / 10000x /
1000000x perp prefixes are mapped to spot with a quantity multiplier); B needs the same symbol on Bybit and
drops pairs whose recent median price ratio is off by more than 0.5 % (ticker collision).

Cost note: funding history is cached incrementally in the store (``cp_funding``): a steady-state cycle fetches only
the payments after the last stored one, one request per symbol and venue, and Binance's ``fundingRate`` calls go
through a ``Pacer`` (the endpoint allows 500 requests / 5 min per IP, shared with the whole collector; the pacer
keeps this engine at 400). Price bars are fetched only for entry candidates and held positions.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from ..analytics.carry import DAY_MS, _Leg, _Market
from ..analytics.carry_rules import DECISION_DELAY_MS
from ..core.envelope import Builder
from ..core.errors import ProviderError
from ..runtime import Runtime
from ..tools.carry_paper import _bars, _entries
from ..tools.perps import current_funding
from .carry_paper_engine import BYBIT, PERP_VENUE, SPOT_VENUE, CarryParams, FeedData

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "USD1", "RLUSD", "EUR", "XUSD", "BFUSD"}
MULT = re.compile(r"^(1000000|10000|1000)([A-Z].*)$")
BOOK_LEVELS = 50
COLLISION_GAP = 0.005
PACE_MAX_CALLS = 400  # Binance fundingRate: 500 / 5 min / IP; leave headroom for fundingInfo and other collector jobs
PACE_WINDOW_S = 300.0


class Pacer:
    """Sliding-window request budget: ``acquire`` waits until ``n`` more calls fit into ``max_calls`` per ``window_s``."""

    def __init__(
        self,
        max_calls: int = PACE_MAX_CALLS,
        window_s: float = PACE_WINDOW_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.max_calls, self.window_s, self.clock, self.sleep = max_calls, window_s, clock, sleep
        self.calls: deque[float] = deque()
        self.total = 0

    def _purge(self, now: float) -> None:
        while self.calls and now - self.calls[0] >= self.window_s:
            self.calls.popleft()

    async def acquire(self, n: int = 1) -> None:
        while True:
            now = self.clock()
            self._purge(now)
            if len(self.calls) + n <= self.max_calls:
                self.calls.extend([now] * n)
                self.total += n
                return
            await self.sleep(max(self.window_s - (now - self.calls[0]), 0.01))

    def note(self, n: int) -> None:
        """Account for calls already made (extra pages) without waiting; the next ``acquire`` pays for them."""
        now = self.clock()
        self.calls.extend([now] * n)
        self.total += n


SHARED_PACER = Pacer()  # one budget for A and B and every feed instance in the process


def _perp_ok(sym: str, m: dict[str, Any] | None) -> tuple[bool, str | None]:
    """(eligible, exclusion reason) for a Binance USDT perp market."""
    info = (m or {}).get("info") or {}
    if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL" or (m or {}).get("active") is False:
        return False, None  # not a candidate at all (dated future, inactive, other quote): not worth listing
    if info.get("underlyingType") != "COIN":
        return False, "tradfi"
    return True, None


def universe_a(perps: dict[str, Any], spots: dict[str, Any]) -> tuple[dict[str, tuple[str, int]], list[dict[str, Any]]]:
    spot_ok = {s for s, m in spots.items() if (m or {}).get("type") == "spot" and (m or {}).get("active") is not False}
    uni: dict[str, tuple[str, int]] = {}
    excluded: list[dict[str, Any]] = []
    for sym, m in perps.items():
        ok, why = _perp_ok(sym, m)
        if not ok:
            if why:
                excluded.append({"symbol": sym, "reason": why})
            continue
        base = sym.split("/")[0]
        mult, spot_base = 1, base
        if (g := MULT.match(base)) is not None:
            mult, spot_base = int(g.group(1)), g.group(2)
        if spot_base in STABLES or f"{spot_base}/USDT" not in spot_ok:
            excluded.append({"symbol": sym, "reason": "no_spot"})
            continue
        uni[sym] = (f"{spot_base}/USDT", mult)
    return uni, excluded


def universe_b(perps: dict[str, Any], other: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    uni: list[str] = []
    excluded: list[dict[str, Any]] = []
    for sym, m in perps.items():
        ok, why = _perp_ok(sym, m)
        if not ok:
            if why:
                excluded.append({"symbol": sym, "reason": why})
            continue
        mb = other.get(sym)
        if mb and mb.get("swap") and mb.get("active") is not False:
            uni.append(sym)
        else:
            excluded.append({"symbol": sym, "reason": "no_second_venue"})
    return sorted(uni), excluded


RELOAD_BACK_MS = 8 * 3_600_000  # the longest regular funding interval: (last - interval, t] always contains the last payment


def _reload_from(open_positions: list[dict[str, Any]]) -> dict[str, int]:
    """Held positions the engine flagged with a missing funding payment (``gap_cycles`` > 0) -> window start for a re-read."""
    out: dict[str, int] = {}
    for p in open_positions:
        s = p["state"]
        if s.get("gap_cycles") and s.get("last") is not None:
            out[s["symbol"]] = int(s["last"]) - RELOAD_BACK_MS + 1
    return out


class LiveFeed:
    def __init__(self, rt: Runtime, *, workers: int = 4, pacer: Pacer | None = None) -> None:
        self.rt = rt
        self.workers = workers
        self.pacer = pacer or SHARED_PACER

    # ---- helpers ----

    async def _fetch_range(self, b: Builder, venue: str, sym: str, since: int, until: int) -> dict[str, Any] | None:
        """One history request (paged), paced when the venue is Binance; None on a provider failure."""
        paced = venue == PERP_VENUE
        if paced:
            await self.pacer.acquire(1)
        try:
            f = await self.rt.exchanges.funding_rate_history_range(venue, sym, since, until, trust_short_page=paced)
        except ProviderError as e:
            b.miss(f"funding_history:{venue}:{sym}", e.provider or f"ccxt:{venue}", e.reason, e.detail)
            return None
        b.add(f)
        if paced:
            self.pacer.note(max(int(f.evidence.args.get("pages", 1)) - 1, 0))
        return f.data

    async def _history(
        self, b: Builder, venue: str, sym: str, since: int, until: int, flags: list[dict[str, Any]], reload_from: int | None = None
    ) -> list[dict[str, Any]] | None:
        store = self.rt.store
        st = await store.cp_funding_state(venue, sym) if store is not None else None
        if reload_from is not None and store is not None:
            # A held position has a missing payment (engine R2): the stored coverage claims the range is complete, so the
            # incremental path would never see a hole inside it. Re-read the window from the store-bypassing request.
            got = await self._fetch_range(b, venue, sym, reload_from, until)
            if got is None:
                flags.append({"symbol": sym, "venue": venue, "issue": "funding_history_unavailable"})
                return None
            entries = _entries(got["rows"])
            if not got["complete"]:
                flags.append({"symbol": sym, "venue": venue, "issue": "funding_history_incomplete"})
            await store.cp_funding_put(venue, sym, [(e["ts_ms"], e["rate"]) for e in entries], covered_from=None, covered_to=None)
            rows = await store.cp_funding_rows(venue, sym, since, until)
            return [{"ts_ms": ts, "rate": r} for ts, r in rows] or None
        if st and st["covered_from"] <= since and st["last_ts"] >= st["covered_from"]:
            if st["covered_to"] >= until and st["last_ts"] >= until - DECISION_DELAY_MS - 60_000:
                got: dict[str, Any] | None = {"rows": [], "complete": True}  # the scheduled payment is already stored
            else:
                got = await self._fetch_range(b, venue, sym, st["last_ts"] + 1, until)  # incremental
            cov_from = st["covered_from"]
        else:
            got = await self._fetch_range(b, venue, sym, since, until)
            cov_from = since
        if got is None:
            flags.append({"symbol": sym, "venue": venue, "issue": "funding_history_unavailable"})
            return None
        entries = _entries(got["rows"])
        if not got["complete"]:
            flags.append({"symbol": sym, "venue": venue, "issue": "funding_history_incomplete"})
        if store is None:
            return entries or None
        await store.cp_funding_put(
            venue,
            sym,
            [(e["ts_ms"], e["rate"]) for e in entries],
            covered_from=cov_from if got["complete"] else None,
            covered_to=until if got["complete"] else None,
        )
        rows = await store.cp_funding_rows(venue, sym, since, until)
        return [{"ts_ms": ts, "rate": r} for ts, r in rows] or None

    async def _current(self, b: Builder, venue: str, markets: dict[str, Any]) -> dict[str, dict[str, Any]]:
        cur = await current_funding(self.rt, b, venue, markets)
        bulk = await b.guard(f"funding_rates:{venue}", f"ccxt:{venue}", self.rt.exchanges.funding_rates(venue)) or {}
        for sym, x in cur.items():
            raw = bulk.get(sym) or {}
            x["index"] = float(raw["indexPrice"]) if raw.get("indexPrice") else None
            x["floor"] = None
        return cur

    async def _gather(self, coros: list[Any], workers: int) -> list[Any]:
        sem = asyncio.Semaphore(workers)

        async def run(c: Any) -> Any:
            async with sem:
                return await c

        return await asyncio.gather(*[run(c) for c in coros])

    # ---- feed interface ----

    async def book(self, venue: str, symbol: str) -> dict[str, Any] | None:
        try:
            return (await self.rt.exchanges.order_book(venue, symbol, BOOK_LEVELS)).data
        except ProviderError:
            return None

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        return await (self._load_a if params.strategy == "A" else self._load_b)(params, decision_ts, open_positions)

    async def _load_a(self, params: CarryParams, t: int, open_positions: list[dict[str, Any]]) -> FeedData:
        b = Builder()
        lb = int(params.lookback_days * DAY_MS)
        perps = await b.guard(f"markets:{PERP_VENUE}", f"ccxt:{PERP_VENUE}", self.rt.exchanges.markets(PERP_VENUE))
        spots = await b.guard(f"markets:{SPOT_VENUE}", f"ccxt:{SPOT_VENUE}", self.rt.exchanges.markets(SPOT_VENUE))
        if not perps or not spots:
            raise RuntimeError("Binance market lists unavailable: " + "; ".join(m.field for m in b.missing))
        uni, excluded = universe_a(perps, spots)
        current = await self._current(b, PERP_VENUE, perps)
        flags: list[dict[str, Any]] = []
        opened = {p["state"]["symbol"]: int(p["state"]["open_ts"]) for p in open_positions}
        reload = _reload_from(open_positions)
        for s in opened:  # a held symbol must stay in the universe even if the listing changed
            if s not in uni and s in perps:
                base = s.split("/")[0]
                m = MULT.match(base)
                uni[s] = (f"{m.group(2) if m else base}/USDT", int(m.group(1)) if m else 1)

        async def hist(sym: str) -> tuple[str, list[dict[str, Any]] | None]:
            since = min(t - lb - DAY_MS, opened.get(sym, t) - 3_600_000)
            return sym, await self._history(b, PERP_VENUE, sym, since, t, flags, reload.get(sym))

        funding = dict(await self._gather([hist(s) for s in sorted(uni)], self.workers))
        want = set(opened)
        for sym, ev in funding.items():
            apr = _Market({"funding": ev, "spot": [], "perp": []}, params.bar_ms).apr(t, lb) if ev else None
            if apr is not None and apr >= params.entry_apr:
                want.add(sym)

        async def bars(sym: str) -> tuple[str, dict[str, Any] | None]:
            since = opened.get(sym, t) - 3 * params.bar_ms
            spot_sym, mult = uni[sym]
            perp_bars = await _bars(self.rt, b, PERP_VENUE, sym, since, t)
            spot_bars = await _bars(self.rt, b, SPOT_VENUE, spot_sym, since, t)
            if not perp_bars or not spot_bars or not funding.get(sym):
                flags.append({"symbol": sym, "issue": "bars_or_funding_unavailable"})
                return sym, None
            spot_bars = [dict(x, close=x["close"] * mult) for x in spot_bars]
            return sym, {"funding": funding[sym], "spot": spot_bars, "perp": perp_bars}

        markets = {s: d for s, d in await self._gather([bars(s) for s in sorted(want & set(uni))], self.workers) if d}
        for m in b.missing:
            flags.append({"issue": "provider_missing", "field": m.field, "reason": m.reason})
        return FeedData(markets=markets, excluded=excluded, current=current, spot_symbols=uni, universe_size=len(uni), flags=flags)

    async def _load_b(self, params: CarryParams, t: int, open_positions: list[dict[str, Any]]) -> FeedData:
        b = Builder()
        lb = int(params.lookback_days * DAY_MS)
        pa = await b.guard(f"markets:{PERP_VENUE}", f"ccxt:{PERP_VENUE}", self.rt.exchanges.markets(PERP_VENUE))
        pb = await b.guard(f"markets:{BYBIT}", f"ccxt:{BYBIT}", self.rt.exchanges.markets(BYBIT))
        if not pa or not pb:
            raise RuntimeError("market lists unavailable: " + "; ".join(m.field for m in b.missing))
        uni, excluded = universe_b(pa, pb)
        cur_a = await self._current(b, PERP_VENUE, pa)
        cur_b = await self._current(b, BYBIT, pb)
        flags: list[dict[str, Any]] = []
        opened = {p["state"]["symbol"]: int(p["state"]["open_ts"]) for p in open_positions}
        uni = sorted(set(uni) | {s for s in opened if s in pa and s in pb})
        reload = _reload_from(open_positions)

        async def hist(sym: str) -> tuple[str, list[Any] | None, list[Any] | None]:
            since = min(t - lb - DAY_MS, opened.get(sym, t) - 3_600_000)
            rf = reload.get(sym)
            return (
                sym,
                await self._history(b, PERP_VENUE, sym, since, t, flags, rf),
                await self._history(b, BYBIT, sym, since, t, flags, rf),
            )

        h = {s: (fa, fb) for s, fa, fb in await self._gather([hist(s) for s in uni], self.workers)}
        want = set(opened)
        for sym, (fa, fb) in h.items():
            if not fa or not fb:
                continue
            ra = _Leg({"funding": fa, "bars": []}, params.bar_ms).apr(t, lb)
            rb = _Leg({"funding": fb, "bars": []}, params.bar_ms).apr(t, lb)
            if ra is not None and rb is not None and abs(rb - ra) >= params.entry_apr:
                want.add(sym)

        async def bars(sym: str) -> tuple[str, dict[str, Any] | None]:
            fa, fb = h[sym]
            since = min(opened.get(sym, t) - 3 * params.bar_ms, t - lb)
            ba = await _bars(self.rt, b, PERP_VENUE, sym, since, t)
            bb = await _bars(self.rt, b, BYBIT, sym, since, t)
            if not fa or not fb or not ba or not bb:
                flags.append({"symbol": sym, "issue": "bars_or_funding_unavailable"})
                return sym, None
            if sym not in opened:
                pbx = {x["ts"]: x["close"] for x in bb}
                ratios = [x["close"] / pbx[x["ts"]] for x in ba if pbx.get(x["ts"])]
                if len(ratios) < 20 or abs(statistics.median(ratios) - 1.0) > COLLISION_GAP:
                    excluded.append({"symbol": sym, "reason": "collision" if ratios else "gap"})
                    return sym, None
            return sym, {"a": {"funding": fa, "bars": ba}, "b": {"funding": fb, "bars": bb}}

        markets = {s: d for s, d in await self._gather([bars(s) for s in sorted(want & set(uni))], self.workers) if d}
        current = {s: {"a": cur_a.get(s) or {}, "b": cur_b.get(s) or {}} for s in uni}
        for m in b.missing:
            flags.append({"issue": "provider_missing", "field": m.field, "reason": m.reason})
        return FeedData(markets=markets, excluded=excluded, current=current, universe_size=len(uni), flags=flags)
