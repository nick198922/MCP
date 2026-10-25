"""Polymarket dislocation reversion (IDEA-20261025-pm-dislocation-reversion): Step 0 gates G1-G3 (no returns read).

Network only through the runtime providers (``rt.polymarket`` CLOB / data-api Http clients); per-file JSON cache under
``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm_dislocation_reversion``). The market universe is streamed from
the calibration study's ``universe.json`` (``--src-cache``), classified there before any price.

Step 0 never fetches prints after the dislocation end (+ one fidelity step): ``/trades`` windows stop there, so no
entry/exit price exists in the cache. ``/prices-history`` covers the market life (the detector needs it); Step 0 uses
it only for the detector, the placebo SD (random non-event times) and coverage.

Usage:
    uv run python scripts/verify_pm_dislocation_reversion.py --stage universe
    uv run python scripts/verify_pm_dislocation_reversion.py --stage probe --out FILE
    uv run python scripts/verify_pm_dislocation_reversion.py --stage quotes --sample-events 300   # timing first
    uv run python scripts/verify_pm_dislocation_reversion.py --stage trades
    uv run python scripts/verify_pm_dislocation_reversion.py --stage stage0 --out FILE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.pm_dislocation import (
    DAY_S,
    HOUR_S,
    confirmed_by_prints,
    detect_dislocations,
    flow_class,
    last_print,
    mde_clustered,
    notional,
    placebo_times,
    vwap,
    yes_prints,
)
from tradedesk.analytics.pm_longdated import EXCLUDED_CATEGORIES

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_dislocation_reversion").expanduser()
SRC_CACHE = Path("~/.cache/tradedesk-agents/pm_yes_calibration").expanduser()
T_MIN = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
T_MAX = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())
MIN_LIFETIME_VOL = 50_000.0  # necessary condition for any day with >= $50k of prints (prefilter only)
FIDELITY_MIN = 10
STEP_S = FIDELITY_MIN * 60
CHUNK_S = 14 * DAY_S  # set from the probe (API rejects too long windows)
PRE_VOL_USD = 50_000.0
PRE_VOL_S = DAY_S
PRE_VWAP_S = 30 * 60
EXIT_H_MAX = 72  # condition 1: market must stay open past t_end + horizon (24 h) + 72 h; the 72 h curve point too
CLOSE_BUFFER_S = (24 + 72) * HOUR_S
TRADES_LIMIT = 500
TRADES_OFFSET_CAP = 10_000
G1_MIN, G2_MIN, G3_MAX = 1000, 0.80, 0.008
SD_ASSUMED = 0.06


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def _bad(p: Path) -> bool:
    if not p.exists():
        return True
    try:
        return "error" in json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return True


async def _retry(coro_fn, what: str, tries: int = 5):  # noqa: ANN001, ANN202
    from tradedesk.core.errors import ProviderError

    for k in range(tries):
        try:
            return await coro_fn()
        except ProviderError as e:
            d = e.detail or ""
            if e.reason in ("config", "auth", "data_unavailable") or (d.startswith("HTTP 4") and "429" not in d):
                raise
            print(f"  retry {what}: {str(e)[:120]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  retry {what} after {type(e).__name__}: {str(e)[:120]}", flush=True)
        await asyncio.sleep(5 * (k + 1))
    raise RuntimeError(f"giving up on {what}")


def _stream_markets(path: Path, chunk: int = 32 << 20):  # noqa: ANN202
    """Yield the objects of the top-level "markets" array of a ~2 GB JSON file, reading it in chunks."""
    dec = json.JSONDecoder()
    with path.open() as f:
        buf = ""
        while '"markets": [' not in buf:
            more = f.read(chunk)
            if not more:
                return
            buf += more
        buf = buf[buf.index('"markets": [') + len('"markets": [') :]
        i = 0
        while True:
            while i < len(buf) and buf[i] in " ,\n\r\t":
                i += 1
            if i >= len(buf) - 1:
                more = f.read(chunk)
                if not more:
                    return
                buf, i = buf[i:] + more, 0
                continue
            if buf[i] == "]":
                return
            try:
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                more = f.read(chunk)
                if not more:
                    raise
                buf, i = buf[i:] + more, 0
                continue
            yield obj
            i = j
            if i > chunk:
                buf, i = buf[i:], 0


class Out:
    def __init__(self, path: str | None) -> None:
        self.lines: list[str] = []
        self.path = Path(path).expanduser() if path else None

    def __call__(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self.lines) + "\n")


# ----------------------------------------------------------------------------- stage: universe (no prices)

KEEP_FIELDS = ("market_id", "event_id", "event_slug", "condition_id", "yes_token", "yes_no", "T", "start", "closed",
               "category", "neg_risk", "n_event_markets", "volume", "fees_enabled", "fee_rate")  # fmt: skip


def stage_universe(cache: Path, src: Path) -> None:
    t = time.perf_counter()
    why: Counter = Counter()
    keep: list[dict[str, Any]] = []
    for n, m in enumerate(_stream_markets(src / "universe.json"), 1):
        if n % 500_000 == 0:
            print(f"  streamed {n} {time.perf_counter() - t:.0f}s rss {rss_mb():.0f} MB", flush=True)
        if m.get("status") != "ok":
            why["status_" + str(m.get("status"))] += 1
            continue
        if m.get("category") in EXCLUDED_CATEGORIES:
            why["sports"] += 1
            continue
        if not (T_MIN <= int(m["T"]) < T_MAX):
            why["out_of_period"] += 1
            continue
        if float(m.get("volume") or 0) < MIN_LIFETIME_VOL:
            why["lifetime_vol_lt_50k"] += 1
            continue
        why["kept"] += 1
        keep.append({k: m.get(k) for k in KEEP_FIELDS})
    _write(cache / "markets.json", {"why": dict(why), "markets": keep})
    ev = {m["event_id"] for m in keep}
    print(f"universe: kept {len(keep)} markets / {len(ev)} events; why {dict(why)}", flush=True)
    print(f"  by category (markets): {dict(Counter(m['category'] for m in keep).most_common())}", flush=True)
    print(f"  neg_risk markets {sum(bool(m['neg_risk']) for m in keep)}; yes_no {sum(bool(m['yes_no']) for m in keep)}", flush=True)
    life = [(min(m["closed"], m["T"] + 7 * DAY_S) - m["start"]) / DAY_S for m in keep]
    print(f"  life d: median {statistics.median(life):.1f}, total market-days {sum(life):.0f}", flush=True)
    vol = sorted(m["volume"] for m in keep)
    print(f"  lifetime volume: median {vol[len(vol) // 2]:.0f}, p90 {vol[int(len(vol) * 0.9)]:.0f}", flush=True)
    print(f"  {time.perf_counter() - t:.0f}s peak rss {rss_mb():.0f} MB", flush=True)


def load_markets(cache: Path) -> list[dict[str, Any]]:
    return json.loads((cache / "markets.json").read_text())["markets"]


def life_window(m: dict[str, Any]) -> tuple[int, int]:
    return int(m["start"]), int(min(int(m["closed"]), int(m["T"]) + 7 * DAY_S))


# ----------------------------------------------------------------------------- stage: probe (API facts, condition 3)


async def stage_probe(cache: Path, out: Out) -> None:
    from tradedesk.server import build_server

    ms = load_markets(cache)
    rng = random.Random(11)
    big = [m for m in ms if m["volume"] >= 1_000_000 and m["closed"] - m["start"] > 60 * DAY_S]
    _, rt = build_server()
    pm = rt.polymarket
    try:
        m = rng.choice(big)
        a, b = life_window(m)
        out(f"probe market {m['market_id']} {m['event_slug']} vol {m['volume']:.0f} life {(b - a) / DAY_S:.0f} d")
        for days in (3, 7, 14, 21, 30, 60):
            p = {"market": m["yes_token"], "startTs": a, "endTs": a + days * DAY_S, "fidelity": FIDELITY_MIN}
            try:
                f = await pm.clob.get_json("/prices-history", params=p, ttl=0, tool="prices-history", args=p)
                h = f.data.get("history") or []
                st = [y["t"] - x["t"] for x, y in zip(h, h[1:], strict=False)]
                out(f"  prices-history {days} d fidelity {FIDELITY_MIN}: n={len(h)} median step {statistics.median(st) if st else None}")
            except Exception as e:  # noqa: BLE001
                out(f"  prices-history {days} d: ERROR {str(e)[:120]}")
        # trades: max limit, takerOnly semantics on 20 windows (condition 3)
        mid_t = (a + b) // 2
        for lim in (500, 1000):
            p = {"market": m["condition_id"], "limit": lim, "offset": 0, "takerOnly": "true", "end": mid_t}
            try:
                f = await pm.data.get_json("/trades", params=p, ttl=0, tool="trades", args=p)
                out(f"  trades limit {lim}: rows {len(f.data)}")
            except Exception as e:  # noqa: BLE001
                out(f"  trades limit {lim}: ERROR {str(e)[:120]}")
        n_ok = 0
        stats: Counter = Counter()
        for m in rng.sample(big, 20):
            a, b = life_window(m)
            t1 = rng.randint(a + DAY_S, b - DAY_S)
            rows = {}
            for flag in ("true", "false"):
                p = {"market": m["condition_id"], "limit": 500, "offset": 0, "takerOnly": flag, "start": t1 - 6 * HOUR_S, "end": t1}
                f = await pm.data.get_json("/trades", params=p, ttl=0, tool="trades", args=p)
                rows[flag] = f.data
            tk, al = rows["true"], rows["false"]
            hs_t = Counter(r.get("transactionHash") for r in tk)
            hs_a = Counter(r.get("transactionHash") for r in al)
            extra = sum(hs_a.values()) - sum(hs_a[h] for h in hs_t)
            # in the all-rows set, does the same tx appear with a different wallet (maker rows)?
            wal_t = {(r.get("transactionHash"), r.get("proxyWallet")) for r in tk}
            multi = sum(1 for h in hs_t if hs_t[h] > 1)
            stats["taker_rows"] += len(tk)
            stats["all_rows"] += len(al)
            stats["extra_rows_in_all"] += extra
            stats["taker_tx_with_multiple_rows"] += multi
            maker_like = sum(
                1 for r in al if (r.get("transactionHash"), r.get("proxyWallet")) not in wal_t and r.get("transactionHash") in hs_t
            )
            stats["all_rows_same_tx_other_wallet"] += maker_like
            n_ok += 1
            out(
                f"  window {m['market_id']}: takerOnly rows {len(tk)}, all rows {len(al)}, same-tx other-wallet rows {maker_like}, tx with >1 taker row {multi}"
            )
        out(f"takerOnly probe on {n_ok} windows: {dict(stats)}")
        if tk:
            out(f"  sample taker row keys: {sorted(tk[0].keys())}")
    finally:
        await rt.aclose()
    out.save()


# ----------------------------------------------------------------------------- stage: quotes (/prices-history over life)


def qpath(cache: Path, mid: str) -> Path:
    return cache / "quotes" / mid[-2:] / f"{mid}.json"


def sample_events(ms: list[dict[str, Any]], n_events: int | None, seed: int = 7) -> list[dict[str, Any]]:
    if not n_events:
        return ms
    evs = sorted({m["event_id"] for m in ms})
    sel = set(random.Random(seed).sample(evs, min(n_events, len(evs))))
    return [m for m in ms if m["event_id"] in sel]


async def stage_quotes(cache: Path, n_events: int | None, rate: float) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    pool = sample_events(load_markets(cache), n_events)
    todo = [m for m in pool if _bad(qpath(cache, m["market_id"]))]
    n_chunks = sum(-(-(life_window(m)[1] - life_window(m)[0]) // CHUNK_S) for m in todo)
    print(f"quotes: pool {len(pool)} markets, to fetch {len(todo)} ({n_chunks} chunks of {CHUNK_S // DAY_S} d) rate {rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.clob._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = chunks = 0

    async def one(m: dict[str, Any]) -> None:
        nonlocal done, errs, chunks
        a, b = life_window(m)
        pts: dict[int, float] = {}
        try:
            for s in range(a, b, CHUNK_S):
                p = {"market": m["yes_token"], "startTs": s, "endTs": min(b, s + CHUNK_S), "fidelity": FIDELITY_MIN}
                async with sem:
                    f = await _retry(
                        lambda p=p: rt.polymarket.clob.get_json("/prices-history", params=p, ttl=0, tool="prices-history", args=p), "hist"
                    )
                chunks += 1
                for x in (f.data or {}).get("history") or []:
                    if isinstance(x, dict) and "t" in x and "p" in x:
                        pts[int(x["t"])] = float(x["p"])
            obj = {"pts": sorted(pts.items())}
        except Exception as e:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(e).__name__}: {str(e)[:200]}", "pts": sorted(pts.items())}
        _write(qpath(cache, m["market_id"]), obj)
        done += 1
        if done in (50, 200) or done % 500 == 0:
            el = time.perf_counter() - t_start
            print(
                f"  quotes {done}/{len(todo)} chunks {chunks} {el:.0f}s ({chunks / el:.1f} req/s; ETA {(n_chunks - chunks) / max(chunks / el, 1e-9) / 60:.0f} min) errors {errs} rss {rss_mb():.0f} MB",
                flush=True,
            )

    try:
        await asyncio.gather(*(one(m) for m in todo))
    finally:
        await rt.aclose()
    print(
        f"quotes done {done} markets, {chunks} requests in {time.perf_counter() - t_start:.0f}s errors={errs} rss {rss_mb():.0f} MB",
        flush=True,
    )


def load_pts(cache: Path, m: dict[str, Any]) -> list[tuple[int, float]] | None:
    p = qpath(cache, m["market_id"])
    if not p.exists():
        return None
    return [(int(t), float(x)) for t, x in json.loads(p.read_text()).get("pts") or []]


def candidates(cache: Path, pool: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter]:
    """Detector events after condition 1 (market open past t_end + 96 h, scheduled end not inside it)."""
    why: Counter = Counter()
    out = []
    for m in pool:
        pts = load_pts(cache, m)
        if pts is None:
            why["no_quote_file"] += 1
            continue
        if len(pts) < 10:
            why["empty_history"] += 1
            continue
        why["with_history"] += 1
        for e in detect_dislocations(pts):
            t_end = e["t_end"] + STEP_S  # condition 3: shift the boundary by one fidelity step
            if not (T_MIN <= t_end < T_MAX):
                why["ev_out_of_period"] += 1
                continue
            if min(int(m["closed"]), int(m["T"])) <= t_end + CLOSE_BUFFER_S:
                why["ev_closes_within_96h"] += 1
                continue
            why["ev_candidate"] += 1
            out.append({**e, "t_end_eff": t_end, "market_id": m["market_id"]})
    return out, why


# ----------------------------------------------------------------------------- stage: trades (window up to t_end_eff only)


def tpath(cache: Path, mid: str, t_end: int) -> Path:
    return cache / "trades" / mid[-2:] / f"{mid}_{t_end}.json"


async def fetch_pre_window(pm: Any, m: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
    """Taker prints in [t_start - 24 h, t_end_eff], newest first, stopping once pre-volume and the p_pre window are covered."""
    lo, hi = e["t_start"] - PRE_VOL_S, e["t_end_eff"]
    rows: list[dict[str, Any]] = []
    capped = False
    offset = 0
    while True:
        p = {"market": m["condition_id"], "limit": TRADES_LIMIT, "offset": offset, "takerOnly": "true", "start": lo, "end": hi}
        f = await _retry(lambda p=p: pm.data.get_json("/trades", params=p, ttl=0, tool="trades", args=p), "trades")
        page = f.data if isinstance(f.data, list) else []
        rows.extend(page)
        if len(page) < TRADES_LIMIT:
            break
        oldest = min(int(r["timestamp"]) for r in page)
        pre = sum(float(r["price"]) * float(r["size"]) for r in rows if int(r["timestamp"]) < e["t_start"])
        if oldest < e["t_start"] - PRE_VWAP_S and pre >= PRE_VOL_USD:
            break
        offset += TRADES_LIMIT
        if offset > TRADES_OFFSET_CAP:
            capped = True
            break
    keep = ("timestamp", "asset", "side", "price", "size", "proxyWallet", "transactionHash")
    return {"rows": [{k: r.get(k) for k in keep} for r in rows], "capped": capped, "lo": lo, "hi": hi}


async def stage_trades(cache: Path, n_events: int | None, rate: float) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    pool = sample_events(load_markets(cache), n_events)
    by_id = {m["market_id"]: m for m in pool}
    cands, why = candidates(cache, pool)
    todo = [e for e in cands if _bad(tpath(cache, e["market_id"], e["t_end_eff"]))]
    # seeded event order: a partial run completes a random subset of events (stage0 --complete-only uses only those)
    ev_rank = {ev: random.Random(f"{ev}-13").random() for ev in {m["event_id"] for m in pool}}
    todo.sort(key=lambda e: (ev_rank[by_id[e["market_id"]]["event_id"]], e["market_id"], e["t_end_eff"]))
    print(f"trades: candidates {len(cands)} ({dict(why)}), to fetch {len(todo)} rate {rate}/s", flush=True)
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0

    async def one(e: dict[str, Any]) -> None:
        nonlocal done, errs
        m = by_id[e["market_id"]]
        try:
            async with sem:
                obj = await fetch_pre_window(rt.polymarket, m, e)
        except Exception as ex:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(ex).__name__}: {str(ex)[:200]}", "rows": []}
        _write(tpath(cache, e["market_id"], e["t_end_eff"]), obj)
        done += 1
        if done in (50, 200) or done % 500 == 0:
            el = time.perf_counter() - t_start
            print(
                f"  trades {done}/{len(todo)} {el:.0f}s ({done / el:.1f}/s; ETA {(len(todo) - done) / max(done / el, 1e-9) / 60:.0f} min) errors {errs} rss {rss_mb():.0f} MB",
                flush=True,
            )

    try:
        await asyncio.gather(*(one(e) for e in todo))
    finally:
        await rt.aclose()
    print(f"trades done {done} in {time.perf_counter() - t_start:.0f}s errors={errs} rss {rss_mb():.0f} MB", flush=True)


# ----------------------------------------------------------------------------- stage: stage0 (gates)


def classify_event(m: dict[str, Any], e: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    pr = yes_prints(d.get("rows") or [], m["yes_token"])
    pre_vol = notional(pr, e["t_start"] - PRE_VOL_S, e["t_start"])
    p_pre = vwap(pr, e["t_start"] - PRE_VWAP_S, e["t_start"] - 1)
    lp = last_print(pr, e["t_end_eff"])
    fc = flow_class(pr, e["t_start"], e["t_end_eff"], e["sign"])
    return {
        "pre_vol": pre_vol,
        "capped": bool(d.get("capped")),
        "p_pre": p_pre,
        "p_last": lp[1] if lp else None,
        "confirmed": confirmed_by_prints(p_pre, lp[1] if lp else None, e["sign"]),
        "p_pre_in_band": p_pre is not None and 0.10 <= p_pre <= 0.90,
        **fc,
    }


def stage0(cache: Path, n_events: int | None, out: Out, complete_only: bool = False) -> None:
    ms_all = load_markets(cache)
    pool = sample_events(ms_all, n_events)
    cands, why = candidates(cache, pool)
    if complete_only:  # keep only events whose every candidate has a trades file (random subset, see stage_trades)
        mev = {m["market_id"]: m["event_id"] for m in pool}
        bad_ev = {mev[e["market_id"]] for e in cands if _bad(tpath(cache, e["market_id"], e["t_end_eff"]))}
        out(f"complete-only: dropping {len(bad_ev)} events with unfetched candidates")
        pool = [m for m in pool if m["event_id"] not in bad_ev]
        cands, why = candidates(cache, pool)
    frac = len({m["event_id"] for m in pool}) / len({m["event_id"] for m in ms_all})
    by_id = {m["market_id"]: m for m in pool}
    out(f"universe: {len(ms_all)} markets; sample {len(pool)} markets, event fraction {frac:.4f}")
    n_hist = why["with_history"]
    cov = n_hist / max(1, n_hist + why["empty_history"])
    out(f"detector: {dict(why)}")
    out(f"G2 coverage (markets with >= 10 history points / fetched): {cov:.3f}")
    neg_cov = Counter()
    for m in pool:
        pts = load_pts(cache, m)
        if pts is not None:
            neg_cov[(bool(m["neg_risk"]), len(pts) >= 10)] += 1
    out(f"  by neg_risk (neg, has_history): {dict(neg_cov)}")
    funnel: Counter = Counter()
    rows = []
    for e in cands:
        m = by_id[e["market_id"]]
        p = tpath(cache, e["market_id"], e["t_end_eff"])
        if not p.exists():
            funnel["no_trades_file"] += 1
            continue
        d = json.loads(p.read_text())
        if "error" in d:
            funnel["trades_error"] += 1
            continue
        c = classify_event(m, e, d)
        if not (c["pre_vol"] >= PRE_VOL_USD or c["capped"]):
            funnel["pre_vol_lt_50k"] += 1
            continue
        if not c["p_pre_in_band"]:
            funnel["p_pre_missing_or_out_of_band"] += 1
            continue
        if not c["confirmed"]:
            funnel["not_confirmed_by_prints"] += 1
            continue
        funnel["kept_" + c["cls"]] += 1
        rows.append({**e, **c, "event_id": m["event_id"], "category": m["category"], "neg_risk": bool(m["neg_risk"])})
    out(f"funnel after condition 1 (candidates {len(cands)}): {dict(funnel)}")
    by = Counter(r["cls"] for r in rows)
    out(f"classes (liquid, confirmed, p_pre in band): {dict(by)}")
    c_rows = [r for r in rows if r["cls"] == "C"]
    out(f"class C by category: {dict(Counter(r['category'] for r in c_rows).most_common())}; neg_risk {sum(r['neg_risk'] for r in c_rows)}")
    if c_rows:
        out(
            f"  class C top-3 share median {statistics.median(r['top_share'] for r in c_rows):.2f}; wallets median {statistics.median(r['n_wallets'] for r in c_rows):.0f}; |move| median {statistics.median(abs(r['p_end'] - r['p_start']) for r in c_rows):.3f}"
        )
    est_c = len(c_rows) / frac
    est_b = by["B"] / frac
    out(f"G1 class C events: sample {len(c_rows)} -> full-universe estimate {est_c:.0f} (B: {est_b:.0f})")
    # placebo SD (no returns of events read): 24 h quote change at random non-event times, same markets as the events
    ev_by_m: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in cands:
        ev_by_m[e["market_id"]].append(e)
    diffs = []
    for mid in sorted({r["market_id"] for r in rows}):
        pts = load_pts(cache, by_id[mid]) or []
        dct = dict(pts)
        ts = [t for t, _ in pts]
        close = min(int(by_id[mid]["closed"]), int(by_id[mid]["T"]))
        for t in placebo_times(pts, ev_by_m[mid], n=5, seed=int(mid) % 10007):
            if not (T_MIN <= t < T_MAX) or close <= t + CLOSE_BUFFER_S:  # condition 1 for placebo times as well
                continue
            j = next((x for x in ts if x >= t + DAY_S), None)
            if j is not None and j - (t + DAY_S) <= 2 * STEP_S:
                diffs.append(dct[j] - dct[t])
    sd_pl = statistics.pstdev(diffs) if len(diffs) > 1 else float("nan")
    sd_use = max(SD_ASSUMED, sd_pl) if diffs else SD_ASSUMED
    out(f"placebo 24 h quote change SD (random non-event times, n={len(diffs)}): {sd_pl:.4f}; SD used = max(0.06, placebo) = {sd_use:.4f}")
    # MDE: clusters = event (neg-risk event = one cluster), then UTC day; scaled to the full universe by 1/frac
    for key in ("event_id", "day"):
        sizes = Counter((r["event_id"] if key == "event_id" else r["t_end_eff"] // DAY_S) for r in c_rows)
        mc = mde_clustered(sd_use, list(sizes.values()))
        # events are sampled, so event clusters scale with 1/frac; under perfect intra-day correlation the day-cluster
        # n_eff = n^2 / sum(m_d^2) is invariant to scaling every day's count by 1/frac (worst-case sensitivity)
        n_eff_full = mc["n_eff"] / frac if key == "event_id" else mc["n_eff"]
        mde_full = 2.8 * sd_use / (n_eff_full**0.5) if n_eff_full > 0 else float("inf")
        out(
            f"  cluster {key}: n {mc['n']:.0f}, clusters {len(sizes)}, n_eff sample {mc['n_eff']:.0f}, MDE sample {mc['mde'] * 100:.2f} pp; full-universe n_eff ~{n_eff_full:.0f}, MDE ~{mde_full * 100:.2f} pp"
        )
    # sensitivity (descriptive): no clustering, event x day clusters, SD 0.06; concentration of class C by event
    n_full = len(c_rows) / frac if frac > 0 else 0.0
    for sd_v, lab in ((sd_use, "SD used"), (SD_ASSUMED, "SD 0.06")):
        iid = 2.8 * sd_v / n_full**0.5 if n_full > 0 else float("inf")
        ed = mde_clustered(sd_v, list(Counter((r["event_id"], r["t_end_eff"] // DAY_S) for r in c_rows).values()))
        ed_full = 2.8 * sd_v / (ed["n_eff"] / frac) ** 0.5 if ed["n_eff"] > 0 else float("inf")
        ev_c = mde_clustered(sd_v, list(Counter(r["event_id"] for r in c_rows).values()))
        ev_full = 2.8 * sd_v / (ev_c["n_eff"] / frac) ** 0.5 if ev_c["n_eff"] > 0 else float("inf")
        out(
            f"  sensitivity {lab} ({sd_v:.4f}): iid MDE {iid * 100:.2f} pp (n {n_full:.0f}); event x day n_eff full ~{ed['n_eff'] / frac:.0f} MDE {ed_full * 100:.2f} pp; event n_eff full ~{ev_c['n_eff'] / frac:.0f} MDE {ev_full * 100:.2f} pp"
        )
    top_ev = Counter(r["event_id"] for r in c_rows).most_common(5)
    slug = {m["event_id"]: m["event_slug"] for m in pool}
    out(f"  class C top events: {[(slug.get(e), k) for e, k in top_ev]}; top-5 share {sum(k for _, k in top_ev) / max(1, len(c_rows)):.2f}")
    out(
        f"  class C by year-month: {dict(sorted(Counter(datetime.fromtimestamp(r['t_end_eff'], UTC).strftime('%Y-%m') for r in c_rows).items()))}"
    )
    _write(cache / "stage0_rows.json", rows)
    out(f"rss {rss_mb():.0f} MB")
    out.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("universe", "probe", "quotes", "trades", "stage0"))
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--src-cache", default=str(SRC_CACHE))
    ap.add_argument("--sample-events", type=int, default=None)
    ap.add_argument("--rate", type=float, default=6.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--complete-only", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "universe":
        stage_universe(cache, Path(a.src_cache).expanduser())
    elif a.stage == "probe":
        asyncio.run(stage_probe(cache, Out(a.out)))
    elif a.stage == "quotes":
        asyncio.run(stage_quotes(cache, a.sample_events, a.rate))
    elif a.stage == "trades":
        asyncio.run(stage_trades(cache, a.sample_events, a.rate))
    else:
        stage0(cache, a.sample_events, Out(a.out), a.complete_only)


if __name__ == "__main__":
    main()
