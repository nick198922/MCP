"""Polymarket daily crypto threshold markets vs outcomes and a vol model (IDEA-20261015-polymarket-threshold-vs-vol-model).

The pre-registration is in the idea card ("Попередня реєстрація"). Network only through the runtime providers
(``rt.polymarket`` Gamma / CLOB / data-api, ``rt.binance_vision``, ``rt.deribit``); per-file JSON cache under
``--cache-dir`` (default ``~/.cache/tradedesk-agents/polymarket_threshold``).

Usage:
    uv run python scripts/verify_pm_threshold.py --stage list        # paginated Gamma listing -> events/<asset>.json
    uv run python scripts/verify_pm_threshold.py --stage semantics   # /prices-history vs /trades on a few markets
    uv run python scripts/verify_pm_threshold.py --stage fetch       # quotes + last trade at t0 per market (long)
    uv run python scripts/verify_pm_threshold.py --stage spot        # Binance spot 1h klines + DVOL
    uv run python scripts/verify_pm_threshold.py --stage stage0      # counts, coverage, SDs, MDE gates (no means)
    uv run python scripts/verify_pm_threshold.py --stage full        # only if stage0 gates pass
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.pm_threshold import (
    HOUR_S,
    bootstrap_mean,
    calib_contract,
    classify_market,
    day_baskets,
    digital_above,
    history_alignment,
    last_trade_before,
    mde,
    no_trade_return,
    quote_at,
    realized_vol,
    value_at,
)
from tradedesk.core.errors import ProviderError

ASSETS = {
    # asset: (Gamma series slug, Binance spot symbol, Deribit DVOL currency or None)
    "bitcoin": ("btc-multi-strikes-weekly", "BTCUSDT", "BTC"),
    "ethereum": ("ethereum-multi-strikes-weekly", "ETHUSDT", "ETH"),
    "solana": ("solana-multi-strikes-weekly", "SOLUSDT", None),
    "xrp": ("xrp-multi-strikes-weekly", "XRPUSDT", None),
}
T0_OFFSET_H = 12  # t0 = T - 12 h (pre-registered)
SENS_OFFSETS_H = (6, 24)
WINDOW_S = 2 * HOUR_S  # history window [t0 - 2h, t0]
EVENT_RE = re.compile(r"^(bitcoin|ethereum|solana|xrp)-above-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$")
DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/polymarket_threshold").expanduser()


def ymd(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------- stage: list


async def stage_list(cache: Path) -> None:
    from tradedesk.server import build_server

    edir = cache / "events"
    edir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    try:
        # 1) Primary listing: every closed event tagged multi-strikes (keyset cursor pagination, no offset cap).
        t = time.perf_counter()
        f = await rt.polymarket.events_all({"tag_slug": "multi-strikes", "closed": "true"})
        tag_rows = f.data["rows"]
        print(
            f"tag multi-strikes: closed events={len(tag_rows)} pages={f.data['pages']} complete={f.data['complete']} {time.perf_counter() - t:.0f}s",
            flush=True,
        )
        by_id: dict[str, dict[str, Any]] = {}
        series_ids: dict[str, str] = {}
        other = Counter()
        for ev in tag_rows:
            m = EVENT_RE.match(str(ev.get("slug") or ""))
            if not m:
                other[re.sub(r"-on-.*$", "-on-*", str(ev.get("slug")))] += 1
                continue
            by_id[str(ev["id"])] = ev
            for s in ev.get("series") or []:
                series_ids[str(s.get("id"))] = str(s.get("slug"))
        print(f"  daily threshold events by slug: {len(by_id)}; other tagged slugs (top): {other.most_common(8)}", flush=True)
        others = sorted(
            (str(ev.get("slug")), str(ev.get("endDate")), str(ev.get("title")))
            for ev in tag_rows
            if not EVENT_RE.match(str(ev.get("slug") or "")) and re.match(r"^(bitcoin|ethereum|solana|xrp)-", str(ev.get("slug") or ""))
        )
        (edir / "_other_crypto_slugs.json").write_text(json.dumps(others, indent=0))
        print(f"  series seen on them: {sorted(series_ids.items())}", flush=True)
        # 2) Cross-check: walk each of those series in full and report daily events the tag listing missed.
        only_series = 0
        for sid, sslug in sorted(series_ids.items()):
            f2 = await rt.polymarket.events_all({"series_id": sid, "closed": "true"})
            n_match = 0
            for ev in f2.data["rows"]:
                if EVENT_RE.match(str(ev.get("slug") or "")):
                    n_match += 1
                    if str(ev["id"]) not in by_id:
                        only_series += 1
                        by_id[str(ev["id"])] = ev
            print(
                f"  series {sslug} id={sid}: closed events={len(f2.data['rows'])} daily-threshold={n_match} complete={f2.data['complete']}",
                flush=True,
            )
        print(f"  daily events found only via series walk: {only_series}", flush=True)
        per: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in by_id.values():
            per[EVENT_RE.match(ev["slug"]).group(1)].append(ev)  # type: ignore[union-attr]
        for asset in ASSETS:
            rows = sorted(per.get(asset, []), key=lambda e: str(e.get("endDate")))
            (edir / f"{asset}.json").write_text(json.dumps({"complete": f.data["complete"], "rows": rows}))
            ends = [str(e.get("endDate"))[:10] for e in rows]
            print(f"{asset}: daily events={len(rows)} first={ends[0] if ends else None} last={ends[-1] if ends else None}", flush=True)
    finally:
        await rt.aclose()
    print("list done", flush=True)


def load_markets(cache: Path) -> tuple[list[dict[str, Any]], Counter]:
    """Flatten events -> markets with the classification of `classify_market` (rules check before prices)."""
    out: list[dict[str, Any]] = []
    why: Counter = Counter()
    for asset in ASSETS:
        p = cache / "events" / f"{asset}.json"
        if not p.exists():
            continue
        for ev in json.loads(p.read_text())["rows"]:
            for m in ev.get("markets") or []:
                c = classify_market(asset, ev, m)
                why[(asset, c["status"])] += 1
                if c["status"] == "ok":
                    out.append(c)
    out.sort(key=lambda r: (r["T"], r["asset"], r["strike"]))
    return out, why


def t0_of(m: dict[str, Any], off_h: int = T0_OFFSET_H) -> int:
    return int(m["T"]) - off_h * HOUR_S


def listed_by(m: dict[str, Any], t: int) -> bool:
    return m.get("start") is not None and int(m["start"]) <= t


_NO_RETRY_REASONS = frozenset({"config", "auth", "provider_tier", "data_unavailable"})


def _is_permanent(e: Exception) -> bool:
    """True for errors a retry cannot fix: caller/config errors and any HTTP 4xx except 429.

    `Http` maps 404 -> data_unavailable, 401/403 -> auth, 402 -> provider_tier and other non-retryable 4xx
    (400, 422, ...) -> backend_error with detail "HTTP <code>: ..."; so check the reason, then the detail prefix.
    """
    if not isinstance(e, ProviderError):
        return False
    if e.reason in _NO_RETRY_REASONS:
        return True
    m = re.match(r"HTTP (\d{3})\b", e.detail or "")
    return bool(m) and 400 <= int(m.group(1)) < 500 and m.group(1) != "429"


async def _retry(coro_fn, what: str, tries: int = 5):  # noqa: ANN001, ANN202
    for k in range(tries):
        try:
            return await coro_fn()
        except Exception as e:  # noqa: BLE001
            if _is_permanent(e):
                raise
            print(f"  retry {what} after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(5 * (k + 1))
    raise RuntimeError(f"giving up on {what}")


def _write(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


# ----------------------------------------------------------------------------- stage: semantics (condition 2)


async def stage_semantics(cache: Path, n_markets: int = 12, basket: bool = False) -> None:
    """Fetch 1m history and ALL trades in [t0 - 2h, t0] for a seeded random sample and compare (no outcomes used).

    ``basket``: sample only contracts whose q at t0 falls in a tail/center basket (needs the quotes cache)."""
    from tradedesk.server import build_server

    markets, _ = load_markets(cache)
    if basket:
        qdir = cache / f"quotes_{T0_OFFSET_H}h"
        pool = []
        for m in markets:
            p = qdir / f"{m['market_id']}.json"
            qa = quote_at(json.loads(p.read_text())["hist"], t0_of(m)) if p.exists() else None
            if qa and in_baskets(qa[0]):
                pool.append(m)
        markets = pool
    rng = random.Random(7)
    sample = rng.sample(markets, n_markets)
    sdir = cache / ("semantics_basket" if basket else "semantics")
    sdir.mkdir(exist_ok=True)
    _, rt = build_server()
    agg = []
    try:
        for m in sample:
            t0 = t0_of(m)
            p = sdir / f"{m['market_id']}.json"
            if p.exists():
                d = json.loads(p.read_text())
            else:
                h = await _retry(lambda m=m, t0=t0: rt.polymarket.price_history_range(m["yes_token"], t0 - WINDOW_S, t0, 1), "hist")
                trades: list[dict[str, Any]] = []
                off = 0
                while off <= 9500:
                    f = await _retry(
                        lambda m=m, t0=t0, off=off: rt.polymarket.trades(
                            m["condition_id"], limit=500, offset=off, start=t0 - WINDOW_S - 3600, end=t0 + 120
                        ),
                        "trades",
                    )
                    trades += f.data
                    if len(f.data) < 500:
                        break
                    off += 500
                d = {"hist": h.data, "trades": trades, "truncated": off > 9500}
                _write(p, d)
            al = history_alignment(d["hist"], d["trades"], m["yes_token"])
            agg.append(al)
            print(
                f"{m['asset']:8s} {m['date']} K={m['strike']:g} pts={len(d['hist'])} trades={len(d['trades'])} n={al['n']} "
                f"match_prev={_pct(al['match_prev_trade'])} match_last_in_next60s={_pct(al['match_last_in_bucket'])} "
                f"match_first_in_next60s={_pct(al['match_first_in_bucket'])} half_cent={_pct(al['half_cent_share'])} "
                f"|p-prev|={_f3(al['mean_abs_to_prev'])} |p-next|={_f3(al['mean_abs_to_next'])}",
                flush=True,
            )
    finally:
        await rt.aclose()
    tot = sum(a["n"] for a in agg)

    def w(k: str) -> float | None:
        num = sum(a[k] * a["n"] for a in agg if a[k] is not None)
        den = sum(a["n"] for a in agg if a[k] is not None)
        return num / den if den else None

    print(
        f"POOLED points={tot} match_prev={_pct(w('match_prev_trade'))} match_last_in_next60s={_pct(w('match_last_in_bucket'))} "
        f"match_first_in_next60s={_pct(w('match_first_in_bucket'))} half_cent={_pct(w('half_cent_share'))} "
        f"|p-prev|={_f3(w('mean_abs_to_prev'))} |p-next|={_f3(w('mean_abs_to_next'))}",
        flush=True,
    )
    print("semantics done", flush=True)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _f3(x: float | None) -> str:
    return "—" if x is None else f"{x:.4f}"


# ----------------------------------------------------------------------------- stage: fetch (quotes at t0, then trades)


def in_baskets(q: float) -> bool:
    return 0.02 <= q <= 0.15 or 0.85 <= q <= 0.98 or 0.35 <= q <= 0.65


async def stage_fetch(cache: Path, off_h: int, limit: int | None) -> None:
    from tradedesk.server import build_server

    markets, _ = load_markets(cache)
    qdir = cache / f"quotes_{off_h}h"
    tdir = cache / f"trades_{off_h}h"
    qdir.mkdir(exist_ok=True)
    tdir.mkdir(exist_ok=True)
    todo = [m for m in markets if listed_by(m, t0_of(m, off_h) - WINDOW_S) and not (qdir / f"{m['market_id']}.json").exists()]
    if limit:
        todo = todo[:limit]
    print(f"quotes T-{off_h}h: {len(markets)} markets, {len(todo)} to fetch", flush=True)
    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    t_start = time.perf_counter()
    done = 0

    async def one_q(m: dict[str, Any]) -> None:
        nonlocal done
        t0 = t0_of(m, off_h)
        async with sem:
            h = await _retry(lambda: rt.polymarket.price_history_range(m["yes_token"], t0 - WINDOW_S, t0, 1), "hist")
        _write(qdir / f"{m['market_id']}.json", {"hist": h.data})
        done += 1
        if done == 40:
            el = time.perf_counter() - t_start
            print(f"  sample: 40 in {el:.1f}s -> estimate {el / 40 * len(todo) / 60:.0f} min for quotes", flush=True)
        if done % 1000 == 0:
            print(f"  quotes {done}/{len(todo)} {time.perf_counter() - t_start:.0f}s", flush=True)

    try:
        await asyncio.gather(*(one_q(m) for m in todo))
        print(f"quotes done {done} in {time.perf_counter() - t_start:.0f}s", flush=True)
        # Trades only for contracts whose q falls in a basket (stale filter + NO-entry print), one call each.
        todo_t = []
        for m in markets:
            p = qdir / f"{m['market_id']}.json"
            if not p.exists() or (tdir / f"{m['market_id']}.json").exists():
                continue
            qa = quote_at(json.loads(p.read_text())["hist"], t0_of(m, off_h))
            if qa and in_baskets(qa[0]):
                todo_t.append(m)
        if limit:
            todo_t = todo_t[:limit]
        print(f"trades T-{off_h}h: {len(todo_t)} to fetch", flush=True)
        done = 0
        t_start = time.perf_counter()

        async def one_t(m: dict[str, Any]) -> None:
            nonlocal done
            t0 = t0_of(m, off_h)
            async with sem:
                f = await _retry(lambda: rt.polymarket.trades(m["condition_id"], limit=5, end=t0), "trades")
            _write(tdir / f"{m['market_id']}.json", {"trades": f.data})
            done += 1
            if done == 40:
                el = time.perf_counter() - t_start
                print(f"  sample: 40 in {el:.1f}s -> estimate {el / 40 * len(todo_t) / 60:.0f} min for trades", flush=True)
            if done % 1000 == 0:
                print(f"  trades {done}/{len(todo_t)} {time.perf_counter() - t_start:.0f}s", flush=True)

        await asyncio.gather(*(one_t(m) for m in todo_t))
        print(f"trades done {done} in {time.perf_counter() - t_start:.0f}s", flush=True)
    finally:
        await rt.aclose()
    print(f"fetch T-{off_h}h done", flush=True)


# ----------------------------------------------------------------------------- stage: spot (Binance 1h klines) + DVOL


async def stage_spot(cache: Path) -> None:
    from tradedesk.providers.binance_vision import parse_klines_zip
    from tradedesk.server import build_server

    kdir = cache / "klines"
    kdir.mkdir(exist_ok=True)
    jobs: list[tuple[Path, str]] = []
    for _, sym, _ in ASSETS.values():
        for y, mo in [(2025, m) for m in range(6, 13)] + [(2026, m) for m in range(1, 10)]:
            name = f"{sym}-1h-{y:04d}-{mo:02d}"
            jobs.append((kdir / f"{name}.json", f"/data/spot/monthly/klines/{sym}/1h/{name}.zip"))
        for d in range(1, 15):
            name = f"{sym}-1h-2026-10-{d:02d}"
            jobs.append((kdir / f"{name}.json", f"/data/spot/daily/klines/{sym}/1h/{name}.zip"))
    todo = [j for j in jobs if not j[0].exists()]
    print(f"klines: {len(jobs)} files, {len(todo)} to fetch", flush=True)
    _, rt = build_server()
    try:
        for p, url in todo:
            f = await _retry(lambda url=url: rt.binance_vision.http.get_bytes(url, tool="spot_klines"), url)
            _write(p, parse_klines_zip(f.data))
        for _, _, cur in ASSETS.values():
            if cur is None:
                continue
            p = cache / f"dvol_{cur}_1h.json"
            if p.exists():
                continue
            start = int(datetime(2025, 6, 1, tzinfo=UTC).timestamp() * 1000)
            end = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp() * 1000)
            rows: dict[int, float] = {}
            for _ in range(200):
                f = await _retry(lambda cur=cur, start=start, end=end: rt.deribit.volatility_index_data(cur, start, end, 3600), "dvol")
                data = f.data or {}
                for x in data.get("data") or []:
                    rows[int(x[0]) // 1000 + HOUR_S] = float(x[4]) / 100.0  # keyed by the moment the hourly close is known
                cont = data.get("continuation")
                if not cont or int(cont) <= start or int(cont) >= end:
                    break
                end = int(cont)
            _write(p, sorted(rows.items()))
            print(f"dvol {cur}: {len(rows)} hourly values", flush=True)
    finally:
        await rt.aclose()
    print("spot done", flush=True)


def load_spot(cache: Path) -> dict[str, list[tuple[int, float]]]:
    """asset -> [(close_time_s, close)] and asset+'@open' -> [(open_time_s, open)] from cached 1h klines."""
    out: dict[str, list[tuple[int, float]]] = {}
    for asset, (_, sym, _) in ASSETS.items():
        rows: dict[int, list[float]] = {}
        for p in sorted((cache / "klines").glob(f"{sym}-1h-*.json")):
            for r in json.loads(p.read_text()):
                rows[int(r[0]) // 1000] = r
        ks = sorted(rows)
        out[asset] = [(k + HOUR_S, float(rows[k][4])) for k in ks]
        out[asset + "@open"] = [(k, float(rows[k][1])) for k in ks]
    return out


def load_dvol(cache: Path) -> dict[str, list[tuple[int, float]]]:
    out = {}
    for asset, (_, _, cur) in ASSETS.items():
        p = cache / f"dvol_{cur}_1h.json"
        if cur and p.exists():
            out[asset] = [(int(t), float(v)) for t, v in json.loads(p.read_text())]
    return out


# ----------------------------------------------------------------------------- shared row builder


def build_rows(cache: Path, off_h: int = T0_OFFSET_H, stale_s: int = 2 * HOUR_S) -> list[dict[str, Any]]:
    """One row per classified market: quote at t0, last trade, filters, spot/vol inputs. No statistics here."""
    markets, _ = load_markets(cache)
    spot = load_spot(cache)
    dvol = load_dvol(cache)
    qdir = cache / f"quotes_{off_h}h"
    tdir = cache / f"trades_{off_h}h"
    opens = {a: dict(spot.get(a + "@open") or []) for a in ASSETS}
    rows = []
    for m in markets:
        t0 = t0_of(m, off_h)
        r = dict(m)
        r["t0"] = t0
        r["listed"] = listed_by(m, t0 - WINDOW_S)
        qp = qdir / f"{m['market_id']}.json"
        qa = quote_at(json.loads(qp.read_text())["hist"], t0) if qp.exists() else None
        r["fetched"] = qp.exists()
        r["q"] = qa[0] if qa else None
        tp = tdir / f"{m['market_id']}.json"
        lt = last_trade_before(json.loads(tp.read_text())["trades"], t0, m["yes_token"]) if tp.exists() else None
        r["trades_fetched"] = tp.exists()
        r["last_trade"] = lt
        r["fresh"] = lt is not None and lt["age"] <= stale_s
        r["S0"] = value_at(spot[m["asset"]], t0) if spot.get(m["asset"]) else None
        r["ST"] = opens[m["asset"]].get(int(m["T"]))  # open of the 1h bar starting exactly at T
        r["dvol"] = value_at(dvol[m["asset"]], t0) if m["asset"] in dvol else None
        rows.append(r)
    return rows


def tail_side(q: float) -> str | None:
    if 0.02 <= q <= 0.15:
        return "low"
    if 0.85 <= q <= 0.98:
        return "high"
    if 0.35 <= q <= 0.65:
        return "center"
    return None


def h2_select(r: dict[str, Any]) -> float | None:
    """Pre-registered H2 filter (BTC/ETH, DVOL model); returns p_model if the contract qualifies."""
    if r["asset"] not in ("bitcoin", "ethereum") or r["q"] is None or not r["fresh"]:
        return None
    if not (0.02 <= r["q"] <= 0.15) or r["S0"] is None or r["dvol"] is None:
        return None
    pm = digital_above(r["S0"], r["strike"], r["dvol"], (r["T"] - r["t0"]) / (365 * 86400))
    if pm <= 0.15 and r["q"] - pm >= 0.02:
        return pm
    return None


def calib_values(rows: list[dict[str, Any]], assets: tuple[str, ...] = tuple(ASSETS)) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        if r["asset"] in assets and r["q"] is not None and r["fresh"]:
            c = calib_contract(r["q"], r["outcome"])
            if c:
                out.append((r["date"], c["resid"]))
    return out


def h2_values(rows: list[dict[str, Any]], half_spread: float = 0.01, fee_all: float | None = None) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        if h2_select(r) is None:
            continue
        lt = r["last_trade"]
        rate, exp = (fee_all, 1.0) if fee_all is not None else (r["fee_rate"], r["fee_exp"])
        x = no_trade_return(
            r["q"], r["outcome"], half_spread=half_spread, last_no_px=lt["no_px"] if lt else None, fee_rate=rate, fee_exp=exp
        )
        out.append((r["date"], x["ret"]))
    return out


def h2mf_values(rows: list[dict[str, Any]], half_spread: float = 0.01, fee_all: float | None = None) -> list[tuple[str, float]]:
    """Secondary H2-mf: buy the favourite in either tail (NO when q low, YES when q high), all assets, no model."""
    out = []
    for r in rows:
        if r["q"] is None or not r["fresh"]:
            continue
        side = tail_side(r["q"])
        if side not in ("low", "high"):
            continue
        lt = r["last_trade"]
        rate, exp = (fee_all, 1.0) if fee_all is not None else (r["fee_rate"], r["fee_exp"])
        if side == "low":
            x = no_trade_return(
                r["q"], r["outcome"], half_spread=half_spread, last_no_px=lt["no_px"] if lt else None, fee_rate=rate, fee_exp=exp
            )
        else:  # buying YES at q is buying "NO of the mirrored contract" with price 1 - q
            x = no_trade_return(
                1 - r["q"], 1 - r["outcome"], half_spread=half_spread, last_no_px=lt["yes_px"] if lt else None, fee_rate=rate, fee_exp=exp
            )
        out.append((r["date"], x["ret"]))
    return out


# ----------------------------------------------------------------------------- stage 0 (no means)


def stage0(cache: Path, out: str) -> dict[str, Any]:
    rows = build_rows(cache)
    _, why = load_markets(cache)
    L: list[str] = []
    P = L.append
    P("## Стадія 0 (без середніх)\n")
    P("### Перелік і виключення до цін\n")
    P(
        "| Актив | ринків ok | виключено: правила | виключено: endDate ≠ 12:00 ET | виключено: дата ≠ дата в slug | невирішені "
        "| подій (днів) | перша дата | остання дата | пропущених дат |"
    )
    P("|---|---|---|---|---|---|---|---|---|---|")
    for asset in ASSETS:
        rs = [r for r in rows if r["asset"] == asset]
        ds = sorted({r["date"] for r in rs})
        d0, d1 = date.fromisoformat(ds[0]), date.fromisoformat(ds[-1])
        gaps = (d1 - d0).days + 1 - len(ds)
        P(
            f"| {asset} | {len(rs)} | {why.get((asset, 'rules'), 0)} | {why.get((asset, 'end_mismatch'), 0)} | {why.get((asset, 'slug_date'), 0)} | "
            f"{why.get((asset, 'unresolved'), 0)} | "
            f"{len(ds)} | {ds[0]} | {ds[-1]} | {gaps} |"
        )
    P("\n### Ринки за роком\n")
    by_y = Counter((r["asset"], r["date"][:4]) for r in rows)
    P("| Актив | " + " | ".join(["2025", "2026"]) + " |\n|---|---|---|")
    for asset in ASSETS:
        P(f"| {asset} | {by_y.get((asset, '2025'), 0)} | {by_y.get((asset, '2026'), 0)} |")

    # consistency check against Binance: outcome vs the 1h bar open at T (price at 12:00:00 ET, rule uses 12:00 close)
    agree = n_chk = near = 0
    for r in rows:
        if r["ST"] is None:
            continue
        if abs(math.log(r["ST"] / r["strike"])) < 0.005:
            near += 1
            continue
        n_chk += 1
        agree += int((r["ST"] > r["strike"]) == bool(r["outcome"]))
    P(
        f"\n**Звірка результату з Binance** (відкриття 1h-бару о T проти страйку, без страйків ближче 0.5 %): {agree}/{n_chk} збігів ({agree / max(n_chk, 1):.2%}); близьких до страйку пропущено {near}.\n"
    )

    listed = [r for r in rows if r["listed"]]
    withq = [r for r in listed if r["q"] is not None]
    P("### Покриття котирувань на t0 = T − 12 год\n")
    P("| Актив | ринків | приймали ордери до t0 − 2 год | є q (точка ≤ t0 − 60 с, вік ≤ 15 хв) | покриття |\n|---|---|---|---|---|")
    for asset in [*ASSETS, "усі"]:
        a_rows = rows if asset == "усі" else [r for r in rows if r["asset"] == asset]
        a_l = [r for r in a_rows if r["listed"]]
        a_q = [r for r in a_l if r["q"] is not None]
        P(f"| {asset} | {len(a_rows)} | {len(a_l)} | {len(a_q)} | {len(a_q) / max(len(a_l), 1):.1%} |")
    cov = len(withq) / max(len(listed), 1)

    P("\n### Кошики за q і фільтр давності останньої угоди\n")
    P("| Кошик | контрактів з q | з угодою ≤ t0 | свіжих ≤ 30 хв | свіжих ≤ 2 год (основний) | свіжих ≤ 6 год |\n|---|---|---|---|---|---|")
    for side in ("low", "high", "center"):
        rs = [r for r in withq if tail_side(r["q"]) == side]
        lt = [r for r in rs if r["last_trade"] is not None]

        def fr(s: int, rs: list[dict[str, Any]] = rs) -> str:
            k = sum(1 for r in rs if r["last_trade"] is not None and r["last_trade"]["age"] <= s)
            return f"{k} ({k / max(len(rs), 1):.0%})"

        P(f"| {side} | {len(rs)} | {len(lt)} | {fr(1800)} | {fr(7200)} | {fr(21600)} |")

    cal = calib_values(rows)
    days_assets = {(r["asset"], r["date"]) for r in rows if r["q"] is not None and r["fresh"] and calib_contract(r["q"], r["outcome"])}
    dates = day_baskets(cal)
    per_da = Counter((r["asset"], r["date"]) for r in rows if r["q"] is not None and r["fresh"] and calib_contract(r["q"], r["outcome"]))
    ge3 = sum(1 for v in per_da.values() if v >= 3)
    sd_c = statistics.stdev([v for _, v, _ in dates]) if len(dates) > 1 else float("nan")
    sd_contract = statistics.stdev([v for _, v in cal]) if len(cal) > 1 else float("nan")
    mde_c = mde(sd_c, len(dates))

    h2 = day_baskets(h2_values(rows))
    sd_h2 = statistics.stdev([v for _, v, _ in h2]) if len(h2) > 1 else float("nan")
    mde_h2 = mde(sd_h2, len(h2)) if h2 else math.inf
    h2mf = day_baskets(h2mf_values(rows))
    sd_mf = statistics.stdev([v for _, v, _ in h2mf]) if len(h2mf) > 1 else float("nan")
    mde_mf = mde(sd_mf, len(h2mf)) if h2mf else math.inf

    P("\n### Потужність (SD з фактичних результатів; середні не рахувались)\n")
    P(
        f"- Хвостових контрактів після фільтра (2 год): {len(cal)}; днів-активів: {len(days_assets)}; дат (кластерів): {len(dates)}; днів-активів із ≥ 3 хвостовими: {ge3}."
    )
    P(f"- Калібрування: SD на контракт {sd_contract:.3f}; SD значень дат {sd_c:.3f}; MDE = 2.8·SD/√n = {mde_c * 100:.2f} п.п.")
    P(
        f"- H2 (BTC+ETH, DVOL-фільтр, дохід на капітал): днів {len(h2)}, контрактів {sum(n for _, _, n in h2)}; SD дня {sd_h2:.3f}; MDE = {mde_h2 * 100:.2f} п.п."
    )
    P(f"- H2-mf (другорядний, 4 активи, обидва хвости): днів {len(h2mf)}; SD дня {sd_mf:.3f}; MDE = {mde_mf * 100:.2f} п.п.")
    gates = {
        "G1 днів-активів ≥ 300": (len(days_assets) >= 300, f"{len(days_assets)}"),
        "G2 покриття q ≥ 70 %": (cov >= 0.70, f"{cov:.1%}"),
        "G3 MDE калібрування ≤ 1.0 п.п.": (mde_c <= 0.01, f"{mde_c * 100:.2f} п.п."),
        "G4 MDE H2 ≤ 1.0 п.п.": (mde_h2 <= 0.01, f"{mde_h2 * 100:.2f} п.п."),
    }
    P("\n### Ворота\n")
    P("| Ворота | Значення | Пройдено |\n|---|---|---|")
    for k, (ok, v) in gates.items():
        P(f"| {k} | {v} | {'так' if ok else 'ні'} |")
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")
    return {k: ok for k, (ok, _) in gates.items()}


# ----------------------------------------------------------------------------- full (only after the gates)


def _row(label: str, vals: list[tuple[str, float]], k: float = 100.0) -> str:
    d = day_baskets(vals)
    b = bootstrap_mean([v for _, v, _ in d])
    if not d:
        return f"| {label} | 0 | 0 | — | — |"
    return f"| {label} | {sum(n for _, _, n in d)} | {len(d)} | {b['mean'] * k:+.2f} | [{b['lo'] * k:+.2f}; {b['hi'] * k:+.2f}] |"


def full(cache: Path, out: str) -> None:
    g = stage0(cache, "")
    keys = list(g)
    if not (g[keys[0]] and g[keys[1]] and g[keys[2]]):
        print("\nG1-G3 not passed: no means are computed (pre-registered). Verdict: inconclusive.", flush=True)
        return
    rows = build_rows(cache)
    L: list[str] = [
        "## Повний прогін\n",
        "| Варіант | контрактів | дат | середнє, п.п. | 95 % CI (bootstrap по датах) |",
        "|---|---|---|---|---|",
    ]
    P = L.append
    P(_row("**Калібрування, головний: обидва хвости, 4 активи, давність ≤ 2 год**", calib_values(rows)))
    for side in ("low", "high"):
        P(
            _row(
                f"хвіст {side}",
                [
                    (r["date"], calib_contract(r["q"], r["outcome"])["resid"])
                    for r in rows
                    if r["q"] is not None and r["fresh"] and tail_side(r["q"]) == side
                ],
            )
        )
    for asset in ASSETS:
        P(_row(f"лише {asset}", calib_values(rows, (asset,))))
    ds = sorted({d for d, _ in calib_values(rows)})
    mid = ds[len(ds) // 2]
    P(_row(f"перша половина (< {mid})", [x for x in calib_values(rows) if x[0] < mid]))
    P(_row(f"друга половина (≥ {mid})", [x for x in calib_values(rows) if x[0] >= mid]))
    moves: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["S0"] and r["ST"]:
            moves[r["date"]] = max(moves[r["date"]], abs(math.log(r["ST"] / r["S0"])))
    top5 = set(sorted(moves, key=lambda d: -moves[d])[:5])
    P(_row(f"без 5 дат з найбільшим рухом ({', '.join(sorted(top5))})", [x for x in calib_values(rows) if x[0] not in top5]))
    for s in (1800, 21600):
        rs = build_rows(cache, stale_s=s)
        P(_row(f"давність ≤ {s // 60} хв", calib_values(rs)))
    P(
        _row(
            "центр q ∈ [0.35; 0.65], outcome − q (база)",
            [(r["date"], r["outcome"] - r["q"]) for r in rows if r["q"] is not None and r["fresh"] and tail_side(r["q"]) == "center"],
        )
    )
    # reliability by q bucket (descriptive)
    P("\n**Калібрування за кошиками q (усі свіжі контракти, описово):**\n")
    P("| q | контрактів | середнє q | частота YES | різниця, п.п. |\n|---|---|---|---|---|")
    edges = [0.02, 0.05, 0.10, 0.15, 0.35, 0.65, 0.85, 0.90, 0.95, 0.98]
    for a_, b_ in zip(edges, edges[1:], strict=False):
        rs = [r for r in rows if r["q"] is not None and r["fresh"] and a_ <= r["q"] < b_]
        if rs:
            mq = sum(r["q"] for r in rs) / len(rs)
            fy = sum(r["outcome"] for r in rs) / len(rs)
            P(f"| [{a_:.2f}; {b_:.2f}) | {len(rs)} | {mq:.3f} | {fy:.3f} | {(fy - mq) * 100:+.2f} |")
    # H2 only if its own gate passed
    if g[keys[3]]:
        P("\n**H2 (BTC+ETH, DVOL-фільтр, дохід на капітал, %):**\n")
        P("| Варіант | контрактів | днів | середнє, % | 95 % CI |\n|---|---|---|---|---|")
        P(_row("**H2 головний: півспреду 0.01, комісія за ринком**", h2_values(rows)))
        P(_row("півспреду 0.005", h2_values(rows, half_spread=0.005)))
        P(_row("півспреду 0.02", h2_values(rows, half_spread=0.02)))
        P(_row("комісія 0.07 на всі ринки", h2_values(rows, fee_all=0.07)))
        hv = day_baskets(h2_values(rows))
        worst = min(hv, key=lambda x: x[1])
        P(f"\nНайгірший день кошика H2: {worst[0]}, {worst[1] * 100:+.1f} % капіталу кошика ({worst[2]} контрактів).")
    else:
        P("\nH2: ворота G4 не пройдено — середні H2 не рахувались (inconclusive за попередньою реєстрацією).")
    # H1 descriptive
    P("\n**H1 (описово, не для вердикту): q − p_model у хвостах, п.п.**\n")
    P("| Модель | контрактів | дат | середнє, п.п. | 95 % CI |\n|---|---|---|---|---|")
    spot = load_spot(cache)
    h1d, h1e = [], []
    for r in rows:
        if r["q"] is None or not r["fresh"] or tail_side(r["q"]) not in ("low", "high") or r["S0"] is None:
            continue
        tau = (r["T"] - r["t0"]) / (365 * 86400)
        if r["dvol"] is not None:
            h1d.append((r["date"], r["q"] - digital_above(r["S0"], r["strike"], r["dvol"], tau)))
        rv = realized_vol(spot[r["asset"]], r["t0"])
        if rv:
            h1e.append((r["date"], r["q"] - digital_above(r["S0"], r["strike"], rv, tau)))
    P(_row("DVOL (BTC, ETH)", h1d))
    P(_row("EWMA 30 дн (4 активи)", h1e))
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    ap.add_argument("--basket", action="store_true", help="semantics: sample only basket contracts")
    ap.add_argument("--offset-h", type=int, default=T0_OFFSET_H, help="t0 = T - offset (hours)")
    ap.add_argument("--limit", type=int, default=0, help="fetch at most N markets (timing sample)")
    ap.add_argument("--log", default="", help="write progress lines to this file instead of the terminal")
    a = ap.parse_args()
    c = Path(a.cache_dir).expanduser()
    c.mkdir(parents=True, exist_ok=True)
    if a.log:
        import logging
        import sys

        fh = open(Path(a.log).expanduser(), "a", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = fh
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.stage == "list":
        asyncio.run(stage_list(c))
    elif a.stage == "classify":
        ms, why = load_markets(c)
        for k, v in sorted(why.items()):
            print(k, v)
        print("ok markets", len(ms), "events", len({(m["asset"], m["date"]) for m in ms}))
        print("fees enabled", Counter((m["asset"], m["fees_enabled"], m["fee_rate"], m["fee_exp"]) for m in ms))
        print("no start ts", sum(1 for m in ms if m["start"] is None))
        lead = [(m["T"] - m["start"]) / 3600 for m in ms if m["start"]]
        print("listing lead hours: min", min(lead), "median", statistics.median(lead))
    elif a.stage == "semantics":
        asyncio.run(stage_semantics(c, basket=a.basket))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(c, a.offset_h, a.limit or None))
    elif a.stage == "spot":
        asyncio.run(stage_spot(c))
    elif a.stage == "stage0":
        stage0(c, a.out)
    elif a.stage == "full":
        full(c, a.out)
