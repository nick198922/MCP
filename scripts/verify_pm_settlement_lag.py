"""Step 0 of IDEA-20261029-pm-post-observation-settlement-lag from public archives (standing item 473).

Daily Polymarket crypto markets (BTC/ETH/SOL/XRP "above X on <date>" ladders and daily "Up or Down") that resolve on
the Binance 1m candle opening at 12:00 ET. The winning side comes from Binance spot klines (``rt.binance_vision``),
never from Polymarket; prices are CLOB ``/prices-history`` (fidelity=1) of the winning token only. The registration
(gates K1-K3, definitions) is in the idea card, section "Попередня реєстрація". Network only through runtime
providers; per-file JSON cache under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm_settlement_lag``).

Usage (each stage resumes from the cache):
    uv run python scripts/verify_pm_settlement_lag.py --stage list      # Gamma series walk -> markets_<kind>_<asset>.json
    uv run python scripts/verify_pm_settlement_lag.py --stage spot      # Binance 1m closes at 12:00 ET -> spot_<asset>.json
    uv run python scripts/verify_pm_settlement_lag.py --stage events    # primary/sensitivity events (no prices) -> events.json
    uv run python scripts/verify_pm_settlement_lag.py --stage history [--limit N]   # /prices-history windows per market
    uv run python scripts/verify_pm_settlement_lag.py --stage coverage  # blind counts (rows per window, no prices)
    uv run python scripts/verify_pm_settlement_lag.py --stage full --out FILE       # gates K1-K3 + robustness
    uv run python scripts/verify_pm_settlement_lag.py --stage all --out FILE        # list, spot, events, history, full
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common_helpers import rss_mb  # noqa: E402

from tradedesk.analytics.pm_calibration import cluster_bootstrap_ci  # noqa: E402
from tradedesk.analytics.pm_favorites import cochran_q  # noqa: E402
from tradedesk.analytics.pm_settlement_lag import (  # noqa: E402
    ENTRY_MAX_ASK,
    FAR_FROM_STRIKE,
    HIST_MISSING_MAX,
    K2_RETURN_KILL,
    K3_NONPAY_KILL,
    NO_PRICE_MAX,
    candle_variant,
    entry_return,
    frozen_share,
    gamma_ts,
    gate_decision,
    is_exact_binary,
    median_cluster_ci,
    nearest_strikes,
    offset_audit,
    price_in_window,
    settlement,
    slug_date,
    slug_date_ok,
    t_obs_of,
    winner_above,
    winner_updown,
)
from tradedesk.analytics.pm_threshold import ET, parse_strike, settle_ts  # noqa: E402

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_settlement_lag").expanduser()
ASSETS = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT", "xrp": "XRPUSDT"}
SEED_DATE = "november-20"  # a 2025 date on which every daily series below exists (probe 2026-11-26)
KINDS = {"above": "{a}-above-on-{d}", "updown": "{a}-up-or-down-on-{d}"}
SLUG_RE = {
    "above": re.compile(r"^(bitcoin|ethereum|solana|xrp)-above-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$"),
    "updown": re.compile(r"^(bitcoin|ethereum|solana|xrp)-up-or-down-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$"),
}
PERIOD_END = date(2026, 11, 18)  # last T date: the 7-day payout window must be fully observable on 2026-11-26
HIST_BEFORE_S = 45 * 60  # /prices-history window [T - 45 min, T + 75 min] covers t_obs-30 and t_obs+60 min
HIST_AFTER_S = 75 * 60
LADDER_K = 3  # sensitivity: the 3 strikes nearest the Binance close (primary: the nearest one)
# candle opens cached relative to T: both registered offsets (0, -60) and their -1 / +1 minute neighbours (review #1)
SPOT_OFFSETS = (-120, -60, 0, 60)
SLIM_KEYS = (
    "id conditionId question outcomes clobTokenIds outcomePrices endDate closedTime umaEndDate umaResolutionStatus "
    "umaResolutionStatuses automaticallyResolved resolvedBy feesEnabled feeSchedule feeType volumeNum"
).split()


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg} | rss {rss_mb():.0f} MB", flush=True)


def _write(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def _jl(x: Any) -> list[Any]:
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x) if isinstance(x, str) else None
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def check_market(kind: str, asset: str, ev: dict[str, Any], m: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Rule / shape checks BEFORE any price or outcome is read. Returns (status, slim row or None)."""
    outs = [str(x).lower() for x in _jl(m.get("outcomes"))]
    toks = [str(x) for x in _jl(m.get("clobTokenIds"))]
    want = ["yes", "no"] if kind == "above" else ["up", "down"]
    if outs != want or len(toks) != 2:
        return "tokens", None
    cv = candle_variant(str(m.get("description") or ev.get("description") or ""), asset, kind)
    if cv is None:
        return "rules", None
    end = gamma_ts(m.get("endDate") or ev.get("endDate"))
    if end is None:
        return "end", None
    d = datetime.fromtimestamp(end, ET).date()
    if end != settle_ts(d):
        return "end", None
    if not slug_date_ok(str(ev.get("slug") or ""), d):  # review 2026-11-26 #2: market 584188 in the wrong event
        return "slug_date", None
    strike = None
    if kind == "above":
        strike = parse_strike(str(m.get("question") or ""))
        if not strike or strike <= 0:
            return "strike", None
    row = {k: m.get(k) for k in SLIM_KEYS}
    row.update(
        {
            "kind": kind,
            "asset": asset,
            "event_slug": ev.get("slug"),
            "event_closed": ev.get("closed"),
            "T": end,
            "date": d.isoformat(),
            "strike": strike,
            "candle_off": cv[0],
            "candle_var": cv[1],
        }
    )
    return "ok", row


def event_date(ev: dict[str, Any]) -> str | None:
    """ET date named by the event slug (year from the slug, else from the event's endDate); None if unparseable."""
    s = slug_date(str(ev.get("slug") or ""))
    if s is None:
        return None
    year = s[2]
    if year is None:
        end = gamma_ts(ev.get("endDate"))
        if end is None:
            return None
        year = datetime.fromtimestamp(end, ET).year
    try:
        return date(year, s[0], s[1]).isoformat()
    except ValueError:
        return None


def scan_event(kind: str, asset: str, ev: dict[str, Any], rows: list[dict[str, Any]], why: Counter, status: dict[str, str]) -> None:
    """Check every market of one event; record the event's date status ("ok" or the commonest rejection)."""
    st_ev: Counter = Counter()
    for m in ev.get("markets") or []:
        st, r = check_market(kind, asset, ev, m)
        why[st] += 1
        st_ev[st] += 1
        if r is not None:
            rows.append(r)
    d = event_date(ev)
    if d is not None:
        s = "ok" if st_ev["ok"] else (st_ev.most_common(1)[0][0] if st_ev else "no_markets")
        if not ev.get("closed"):
            s += "+open"
        if status.get(d, "").startswith("ok"):
            return
        status[d] = s


# ----------------------------------------------------------------------------- stage: list


async def stage_list(cache: Path, refresh: bool = False) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    pm = rt.polymarket
    try:
        for kind, pat in KINDS.items():
            for asset in ASSETS:
                out = cache / f"markets_{kind}_{asset}.json"
                if out.exists() and not refresh:
                    log(f"{kind}/{asset}: cached")
                    continue
                seed = pat.format(a=asset, d=SEED_DATE)
                f = await pm.gamma.get_json("/events", params={"slug": seed}, ttl=0, tool="events", args={"slug": seed})
                sids = sorted({str(s.get("id")) for ev in (f.data or []) for s in ev.get("series") or []})
                rows: list[dict[str, Any]] = []
                why: Counter = Counter()
                status: dict[str, str] = {}
                seen: set[str] = set()
                n_open = 0
                # closed AND open events (review 2026-11-26 #3): an event held open by an unresolved market must stay in
                # the universe (K3 counts it as non-payment). iter_events raises if a listing does not finish.
                for sid in sids:
                    for closed in ("true", "false"):
                        async for evs, _last in pm.iter_events({"series_id": sid, "closed": closed}):
                            for ev in evs:
                                if str(ev.get("id")) in seen:
                                    continue
                                seen.add(str(ev.get("id")))
                                if not SLUG_RE[kind].match(str(ev.get("slug") or "")):
                                    why["slug"] += 1
                                    continue
                                n_open += not ev.get("closed")
                                scan_event(kind, asset, ev, rows, why, status)
                # dates with no event in the series listings: look the slug up directly (an event outside the series)
                ok_dates = sorted(d for d, s in status.items() if s.startswith("ok"))
                missing: dict[str, str] = {}
                if ok_dates:
                    d0, d = date.fromisoformat(ok_dates[0]), date.fromisoformat(ok_dates[0])
                    while d <= PERIOD_END:
                        k = d.isoformat()
                        if k not in status:
                            found = False
                            base = pat.format(a=asset, d=f"{d:%B}".lower() + f"-{d.day}")
                            for slug in (f"{base}-{d.year}", base):
                                f = await pm.gamma.get_json("/events", params={"slug": slug}, ttl=0, tool="events", args={"slug": slug})
                                for ev in f.data or []:
                                    if str(ev.get("id")) in seen or event_date(ev) != k:
                                        continue
                                    seen.add(str(ev.get("id")))
                                    found = True
                                    n_open += not ev.get("closed")
                                    scan_event(kind, asset, ev, rows, why, status)
                                    why["found_by_slug"] += 1
                                if found:
                                    break
                            if not found:
                                missing[k] = "not_in_gamma"
                        if not status.get(k, "ok").startswith("ok"):
                            missing[k] = status[k]
                        d += timedelta(days=1)
                    log(f"{kind}/{asset}: date range {d0}..{PERIOD_END}, missing {len(missing)}: {dict(Counter(missing.values()))}")
                _write(
                    out, {"series": sids, "events": len(seen), "open_events": n_open, "why": dict(why), "missing": missing, "rows": rows}
                )
                dates = sorted({r["date"] for r in rows})
                log(
                    f"{kind}/{asset}: series={sids} events={len(seen)} open={n_open} markets ok={len(rows)} checks={dict(why)} "
                    f"candle={dict(Counter(r['candle_var'] for r in rows))} dates {dates[:1]}..{dates[-1:]}"
                )
    finally:
        await rt.aclose()


def load_markets(cache: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind in KINDS:
        for asset in ASSETS:
            p = cache / f"markets_{kind}_{asset}.json"
            if p.exists():
                rows += json.loads(p.read_text())["rows"]
    return [r for r in rows if date.fromisoformat(r["date"]) <= PERIOD_END]


# ----------------------------------------------------------------------------- stage: spot


async def stage_spot(cache: Path) -> None:
    """Binance spot 1m closes of the candles opening at 12:00 ET (DST-aware), per asset: {open_ts: close}."""
    from tradedesk.server import build_server

    mk = load_markets(cache)
    dates = sorted({date.fromisoformat(r["date"]) for r in mk})
    if not dates:
        raise SystemExit("run --stage list first")
    need = {d - timedelta(days=1) for d in dates} | set(dates)  # up/down needs the previous day's close
    months = sorted({(d.year, d.month) for d in need})
    today = datetime.now(UTC).date()
    _, rt = build_server()
    try:
        for asset, sym in ASSETS.items():
            p = cache / f"spot_{asset}.json"
            have: dict[str, float] = json.loads(p.read_text()) if p.exists() else {}
            for y, mth in months:
                want = [settle_ts(d) + off for d in need if (d.year, d.month) == (y, mth) for off in SPOT_OFFSETS]
                if all(str(t) in have for t in want):
                    continue
                if (y, mth) < (today.year, today.month):
                    f = await rt.binance_vision.spot_klines_monthly(sym, "1m", y, mth)
                    rows = f.data
                else:  # current month: daily files
                    rows = []
                    for day in sorted({datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d") for t in want}):
                        rows += (await rt.binance_vision.spot_klines_daily(sym, "1m", day)).data
                wanted = {t * 1000 for t in want}
                for r in rows:
                    if int(r[0]) in wanted:
                        have[str(int(r[0]) // 1000)] = float(r[4])
                del rows
                _write(p, have)
                log(f"spot {sym} {y}-{mth:02d}: closes cached {len(have)}")
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- stage: events


def load_offset_fix(cache: Path) -> dict[str, int]:
    """Registered offset correction written by the first `full` pass (``"kind|variant" -> delta seconds``)."""
    p = cache / "offset_fix.json"
    return json.loads(p.read_text()) if p.exists() else {}


def build_events(cache: Path, fix: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], Counter, list[list[str]]]:
    """Primary and ladder-sensitivity events; winner from Binance only. No Polymarket price or outcome is used here.

    Returns (events, drop counts, dropped [kind, asset, date, reason]). ``fix`` shifts the candle of a rule variant
    (``"kind|variant" -> delta``) by the registered offset rule."""
    fix = fix or {}
    spot = {a: json.loads((cache / f"spot_{a}.json").read_text()) for a in ASSETS if (cache / f"spot_{a}.json").exists()}
    by_day: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in load_markets(cache):
        by_day[(r["kind"], r["asset"], r["date"])].append(r)
    out: list[dict[str, Any]] = []
    why: Counter = Counter()
    dropped: list[list[str]] = []

    def drop(reason: str) -> None:
        why[reason] += 1
        dropped.append([kind, asset, d, reason])

    for (kind, asset, d), ms in sorted(by_day.items()):
        T = ms[0]["T"]
        offs = {m["candle_off"] + fix.get(f"{kind}|{m['candle_var']}", 0) for m in ms}
        if len(offs) != 1:
            drop("mixed_rules")
            continue
        off = offs.pop()
        var = "+".join(sorted({m["candle_var"] for m in ms}))
        close = spot.get(asset, {}).get(str(T + off))
        prev = None
        if close is None:
            drop("no_spot")
            continue
        if kind == "updown":
            prev = spot.get(asset, {}).get(str(settle_ts(date.fromisoformat(d) - timedelta(days=1)) + off))
            w = winner_updown(close, prev)
            if len(ms) != 1:
                drop("updown_not_single")
                continue
            if w is None:
                drop("tie_or_no_prev")
                continue
            picks = [(ms[0], 0)]
        else:
            idx = nearest_strikes([m["strike"] for m in ms], close, LADDER_K)
            picks = [(ms[i], rank) for rank, i in enumerate(idx)]
        for m, rank in picks:
            w = winner_above(close, m["strike"]) if kind == "above" else winner_updown(close, prev)
            toks = _jl(m["clobTokenIds"])
            out.append(
                {
                    "market_id": str(m["id"]),
                    "kind": kind,
                    "asset": asset,
                    "date": d,
                    "T": T,
                    "candle_off": off,
                    "candle_var": var,
                    "t_obs": t_obs_of(T + off),
                    "utc_day": datetime.fromtimestamp(t_obs_of(T + off), UTC).strftime("%Y-%m-%d"),
                    "rank": rank,
                    "primary": rank == 0,
                    "winner": w,
                    "win_token": str(toks[w]),
                    "strike": m.get("strike"),
                    "close": close,
                    "prev": prev,
                }
            )
        why[f"{kind}_events"] += 1
    return out, why, dropped


def stage_events(cache: Path) -> None:
    fix = load_offset_fix(cache)
    ev, why, dropped = build_events(cache, fix)
    _write(cache / "events.json", ev)
    _write(cache / "events_meta.json", {"why": dict(why), "dropped": dropped, "offset_fix": fix})
    prim = [e for e in ev if e["primary"]]
    days = sorted({e["utc_day"] for e in prim})
    log(f"events: {len(ev)} rows, primary {len(prim)}, UTC days {len(days)} ({days[:1]}..{days[-1:]}); {dict(why)}; offset fix {fix}")
    per = Counter((e["kind"], e["asset"]) for e in prim)
    log(f"primary per series: {sorted(per.items())}")
    for asset in ASSETS:  # unit / ticker sanity: the nearest strike must sit within a few % of the Binance close
        d = sorted(abs(e["close"] / e["strike"] - 1) for e in prim if e["kind"] == "above" and e["asset"] == asset)
        if d:
            log(f"sanity {asset}: |close/nearest strike - 1| median {statistics.median(d):.4f}, max {d[-1]:.4f}, n {len(d)}")
    cv = Counter((e["kind"], e["candle_var"], e["candle_off"]) for e in prim)
    log(f"candle rule variants (primary, kind/variant/offset): {sorted(cv.items())}")
    for k, v, o in sorted(cv):
        ds = sorted(e["date"] for e in prim if (e["kind"], e["candle_var"], e["candle_off"]) == (k, v, o))
        log(f"  {k}/{v}/{o}: {ds[0]}..{ds[-1]}")
    # neighbour candles needed by the mismatch audit must be in the spot cache
    spot = {a: json.loads((cache / f"spot_{a}.json").read_text()) for a in ASSETS}
    miss = sum(str(e["T"] + e["candle_off"] + s) not in spot[e["asset"]] for e in prim for s in (-60, 60))
    log(f"neighbour candles missing in spot cache: {miss}")
    big = sorted(abs(e["close"] / e["prev"] - 1) for e in prim if e["kind"] == "updown")
    if big:
        log(f"sanity updown |close/prev - 1| median {statistics.median(big):.4f}, n {len(big)}")


# ----------------------------------------------------------------------------- stage: history


def hist_path(hdir: Path, e: dict[str, Any]) -> Path:
    """History file of the event's winning token (per market AND side: an offset fix can flip the winner)."""
    return hdir / f"{e['market_id']}_{e['winner']}.json"


def migrate_legacy_history(hdir: Path, ev: list[dict[str, Any]]) -> int:
    """Rename pre-2026-11-26 files ``<market_id>.json`` to ``<market_id>_<winner>.json`` when the token matches."""
    n = 0
    for e in ev:
        old = hdir / f"{e['market_id']}.json"
        if old.exists() and not hist_path(hdir, e).exists() and json.loads(old.read_text()).get("token") == e["win_token"]:
            old.replace(hist_path(hdir, e))
            n += 1
    return n


async def stage_history(cache: Path, limit: int | None, conc: int) -> None:
    from tradedesk.server import build_server

    ev = json.loads((cache / "events.json").read_text())
    hdir = cache / "history"
    hdir.mkdir(exist_ok=True)
    log(f"history: legacy files renamed {migrate_legacy_history(hdir, ev)}")
    todo = [e for e in ev if not hist_path(hdir, e).exists()]
    todo.sort(key=lambda e: (not e["primary"], e["date"], e["market_id"]))
    if limit:
        todo = todo[:limit]
    log(f"history: {len(todo)} to fetch of {len(ev)}")
    _, rt = build_server()
    sem = asyncio.Semaphore(conc)
    done = 0
    failed: list[str] = []
    t0 = time.perf_counter()

    async def one(e: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            for k in range(5):
                try:
                    f = await rt.polymarket.price_history_range(e["win_token"], e["T"] - HIST_BEFORE_S, e["T"] + HIST_AFTER_S, 1)
                    break
                except Exception as ex:  # noqa: BLE001
                    log(f"retry {e['market_id']} {type(ex).__name__}: {str(ex)[:100]}")
                    await asyncio.sleep(5 * (k + 1))
            else:
                failed.append(e["market_id"])  # never silent (review #4): `full` refuses the gates if > 1 % missing
                return
            _write(hist_path(hdir, e), {"token": e["win_token"], "hist": f.data})
            done += 1
            if done % 200 == 0 or done == len(todo):
                el = time.perf_counter() - t0
                log(f"history {done}/{len(todo)} {el:.0f}s, eta {el / done * (len(todo) - done):.0f}s")

    try:
        await asyncio.gather(*(one(e) for e in todo))
    finally:
        await rt.aclose()
        _write(cache / "history_failed.json", failed)
        log(f"history: fetched {done}, FAILED {len(failed)} (rerun --stage history to retry)")


def stage_coverage(cache: Path) -> None:
    """Blind availability check: number of history points per window, never their values."""
    ev = json.loads((cache / "events.json").read_text())
    hdir = cache / "history"
    c: Counter = Counter()
    for e in ev:
        p = hist_path(hdir, e)
        if not p.exists():
            continue
        c["files"] += 1
        hist = json.loads(p.read_text())["hist"]
        to = e["t_obs"]
        ts = [int(x["t"]) for x in hist if isinstance(x, dict) and "t" in x]
        c["pts_total"] += len(ts)
        c["has_pre_t_obs-30"] += any(to - 2100 <= t <= to - 1800 for t in ts)
        c["has_t_obs+1..+5"] += any(to + 60 <= t <= to + 300 for t in ts)
        c["has_t_obs+1..+15"] += any(to + 60 <= t <= to + 900 for t in ts)
        c["has_any_after_t_obs+60m"] += any(t > to + 3600 for t in ts)
    log(f"coverage (row counts only): {dict(c)}")


# ----------------------------------------------------------------------------- stage: full (registered analysis)


def _markets_index(cache: Path) -> dict[str, dict[str, Any]]:
    return {str(r["id"]): r for r in load_markets(cache)}


def neighbour_winners(e: dict[str, Any], spot: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Binance winner from the candle one minute earlier / later (both days shifted for up/down), and the distance
    |close / reference - 1| (reference = strike, or the previous day's close for up/down). Review 2026-11-26 #1."""
    s = spot.get(e["asset"], {})
    out: dict[str, Any] = {}
    for lab, sh in (("minus", -60), ("plus", 60)):
        c = s.get(str(e["T"] + e["candle_off"] + sh))
        if e["kind"] == "above":
            out[f"w_{lab}"] = winner_above(c, e["strike"])
        else:
            prev_open = settle_ts(date.fromisoformat(e["date"]) - timedelta(days=1)) + e["candle_off"] + sh
            out[f"w_{lab}"] = winner_updown(c, s.get(str(prev_open)))
    ref = e["strike"] if e["kind"] == "above" else e["prev"]
    out["dist"] = abs(e["close"] / ref - 1) if ref else None
    return out


ALL_GATES = ("K1", "K2", "K3")


def validity(prim: list[dict[str, Any]], cache: Path, meta: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Registered validity rules (review 2026-11-26 #1, #4, #10) -> (gate -> reason forcing "undecided", report lines).

    May only turn a gate into "undecided"; never into "pass" or "kill"."""
    force: dict[str, str] = {}
    L: list[str] = []
    n = len(prim)

    def put(gs: tuple[str, ...], why: str) -> None:
        for k in gs:
            force[k] = (force[k] + "; " if k in force else "") + why

    # (a) data gaps
    no_file = [r for r in prim if not r["has_hist"]]
    if n and len(no_file) > HIST_MISSING_MAX * n:
        put(ALL_GATES, f"REFUSED: {len(no_file)} primary events without a history file (> {HIST_MISSING_MAX:.0%}); rerun --stage history")
    no_p5 = [r for r in prim if r.get("p5") is None]
    if n and len(no_p5) > NO_PRICE_MAX * n:
        put(("K1", "K2"), f"{len(no_p5)} primary events without a t_obs+5 price (> {NO_PRICE_MAX:.0%})")
    # (b) frozen quotes
    fz = frozen_share([(r["pre"], r["p5"]) for r in prim if r.get("pre") is not None and r.get("p5") is not None])
    L.append(f"frozen-quote check (pre-T price in [0.1, 0.9]): n {fz['n']}, p5 == pre-T {fz['same']}, share {fz['share']}")
    if fz["blocks"]:
        put(("K1", "K2"), f"frozen quotes: {fz['same']}/{fz['n']} uncertain-before-T events have p5 == pre-T price")
    # (c) candle-offset audit (applies once: the first pass writes offset_fix.json, the corrected pass is primary)
    mism = [
        {"kind": r["kind"], "variant": r["candle_var"], "by_minus": r["by_minus"], "by_plus": r["by_plus"]} for r in prim if r["mismatch"]
    ]
    audit = offset_audit(mism)
    fix = {f"{k}|{v}": g["fix"] for (k, v), g in audit.items() if g["fix"]}
    L.append(f"offset audit per (kind, variant): { {f'{k}|{v}': g for (k, v), g in sorted(audit.items())} }")
    fpath = cache / "offset_fix.json"
    if not fpath.exists():
        _write(fpath, fix)
        if fix:
            put(ALL_GATES, f"offset fix {fix} written: rerun --stage events, history, full (the corrected pass is primary)")
    elif json.loads(fpath.read_text()) != meta.get("offset_fix", {}):
        put(ALL_GATES, "events.json was built without the current offset_fix.json: rerun --stage events, history, full")
    else:
        L.append(f"offset fix in force (applied once, not re-derived): {meta.get('offset_fix')}; would-be fix now {fix}")
        if fix:
            put(ALL_GATES, f"offset audit still proposes {fix} on the corrected events: candle offset undetermined")
    # (d) mismatches far from the strike cannot be a one-minute shift: token / data error or a real misresolution
    far = [r for r in prim if r["mismatch"] and r["dist"] is not None and r["dist"] > FAR_FROM_STRIKE]
    rp = cache / "far_reviewed.json"  # {market_id: "nonpay: <reason>"}, written only after the manual review
    reviewed = json.loads(rp.read_text()) if rp.exists() else {}
    unreviewed = [r for r in far if not str(reviewed.get(str(r.get("market_id")), "")).startswith("nonpay")]
    L.append(f"far mismatches {len(far)}, reviewed as non-payment {len(far) - len(unreviewed)}: {reviewed}")
    if unreviewed:
        put(ALL_GATES, f"BLOCKED: {len(unreviewed)} unreviewed mismatches > {FAR_FROM_STRIKE:.1%} from the strike / previous close")
    return force, L


def nonexact_guard(
    prim: list[dict[str, Any]], g: dict[str, Any], gates: Callable[..., dict[str, Any]], force: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Re-review D: a K2 / K3 "pass" that holds only through resolutions within tolerance but not exactly (1,0)/(0,1)
    becomes "undecided" (such a quote may be closed-but-unresolved). Never turns anything into "kill"."""
    L: list[str] = []
    P = L.append
    tol = {r["market_id"] for r in prim if r["resolved"] is not None and not r["exact_binary"]}
    if tol:  # re-review D: a non-exact "resolution" may be a closed-but-unresolved quote
        strict = [dict(r, paid=False, paid_7d=False, nonpay=True) if r["market_id"] in tol else r for r in prim]
        gs = gates(strict, force=force)
        for k in ("K2", "K3"):
            if g[k]["verdict"] == "pass" and gs[k]["verdict"] == "kill":
                force[k] = f"pass only through {len(tol)} non-exact resolutions (listed)"
        g = gates(prim, force=force)
        P(f"non-exact resolutions (primary): {sorted(tol)[:50]}")
    return g, L


def analyse(cache: Path) -> list[str]:  # noqa: PLR0912, PLR0915
    ev = json.loads((cache / "events.json").read_text())
    meta = json.loads((cache / "events_meta.json").read_text())
    mi = _markets_index(cache)
    spot = {a: json.loads((cache / f"spot_{a}.json").read_text()) for a in ASSETS}
    hdir = cache / "history"
    fpath = cache / "history_failed.json"
    failed = json.loads(fpath.read_text()) if fpath.exists() else []
    rows = []
    for e in ev:
        m = mi[e["market_id"]]
        to = e["t_obs"]
        st = settlement(m, to, e["winner"])
        su = settlement(m, to, e["winner"], "umaEndDate")
        nb = neighbour_winners(e, spot)
        p = hist_path(hdir, e)
        hist = json.loads(p.read_text())["hist"] if p.exists() else None
        px: dict[str, float | None] = {}
        if hist is not None:
            open_ts = e["T"] + e["candle_off"]
            for lab, a, b in (
                ("p5", to + 60, to + 300),
                ("p2", to + 60, to + 120),
                ("p15", to + 60, to + 900),
                ("pm30", to - 2100, to - 1800),
                ("pre", open_ts - 900, open_ts - 60),
            ):
                q = price_in_window(hist, a, b)
                px[lab] = q[0] if q else None
        sched = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        fr = float(sched.get("rate") or 0.0) if m.get("feesEnabled") else 0.0
        fx = float(sched.get("exponent") or 1.0) if m.get("feesEnabled") else 1.0
        rows.append(
            {
                **e,
                **st,
                **px,
                **nb,
                "by_minus": st["resolved"] is not None and nb["w_minus"] == st["resolved"],
                "by_plus": st["resolved"] is not None and nb["w_plus"] == st["resolved"],
                "nonpay_uma": su["nonpay"],
                "paid_7d_uma": su["paid_7d"],
                "uma_lag_s": su["lag_s"],
                "exact_binary": is_exact_binary(m.get("outcomePrices")),
                "event_closed": m.get("event_closed"),
                "has_hist": hist is not None,
                "hist_empty": hist is not None and not hist,
                "fee_rate": fr,
                "fee_exp": fx,
            }
        )

    def ret(r: dict[str, Any], key: str = "p5", hs: float = 0.01, max_ask: float = ENTRY_MAX_ASK, hold_key: str = "lag_s") -> float | None:
        if r.get(key) is None:
            return None
        hold = r[hold_key] if r[hold_key] is not None else 0
        return entry_return(
            r[key], paid=r["paid"], hold_s=hold, half_spread=hs, fee_rate=r["fee_rate"], fee_exp=r["fee_exp"], max_ask=max_ask
        )

    L: list[str] = []
    P = L.append
    prim = [r for r in rows if r["primary"]]
    days = sorted({r["utc_day"] for r in prim})
    half = days[len(days) // 2] if days else ""
    P(f"primary events {len(prim)}, UTC days {len(days)}, with history file {sum(r['has_hist'] for r in prim)}")
    P(f"series: {sorted(Counter((r['kind'], r['asset']) for r in prim).items())}")

    P("\n== Universe and data gaps (review #3, #4) ==")
    for kind in KINDS:
        for asset in ASSETS:
            mf = json.loads((cache / f"markets_{kind}_{asset}.json").read_text())
            miss = mf.get("missing", {})
            P(f"list {kind}/{asset}: events {mf['events']} open {mf.get('open_events')} checks {mf['why']}")
            P(f"  missing dates {len(miss)} by reason {dict(Counter(miss.values()))}: {sorted(miss)[:60]}")
    P(f"events stage drops: {meta['why']}")
    P(f"  dropped: {meta['dropped'][:60]}")
    P(f"history fetch failures (last run): {len(failed)}")
    for lab, sel in (
        ("no history file", lambda r: not r["has_hist"]),
        ("empty history", lambda r: r["hist_empty"]),
        ("no t_obs+5 price", lambda r: r.get("p5") is None),
    ):
        bad = [r for r in prim if sel(r)]
        br = Counter((r["kind"], "H1" if r["utc_day"] < half else "H2") for r in bad)
        P(f"{lab}: {len(bad)} of {len(prim)} by kind/half {sorted(br.items())}")
    open_prim = [r for r in prim if r.get("event_closed") is False]
    P(f"primary events in events still open on Gamma: {len(open_prim)} (counted as non-payment unless paid within 7 d)")

    def gates(
        rs: list[dict[str, Any]],
        key: str = "p5",
        hs: float = 0.01,
        nonpay_key: str = "nonpay",
        hold_key: str = "lag_s",
        force: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        prices = [r[key] for r in rs if r.get(key) is not None]
        rets = [x for x in (ret(r, key, hs, hold_key=hold_key) for r in rs) if x is not None]
        return gate_decision(prices, rets, [r[nonpay_key] for r in rs], force)

    def fmt(g: dict[str, Any]) -> str:
        def v(x: Any, k: float = 1.0) -> str:
            return "—" if x is None else f"{x * k:.4f}"

        why = "; ".join(f"{k}: {g[k]['why']}" for k in ALL_GATES if g[k]["why"])
        return (
            f"K1 median p={v(g['K1']['value'])} n={g['K1']['n']} {g['K1']['verdict']} | "
            f"K2 mean r={v(g['K2']['value'], 100)}% n={g['K2']['n']} {g['K2']['verdict']} | "
            f"K3 nonpay={v(g['K3']['value'], 100)}% n={g['K3']['n']} {g['K3']['verdict']} -> {g['card']}" + (f" [{why}]" if why else "")
        )

    P("\n== Mismatch audit (review #1; registered rule) ==")
    for k, var, off in sorted({(r["kind"], r["candle_var"], r["candle_off"]) for r in prim}):
        rs = [r for r in prim if (r["kind"], r["candle_var"], r["candle_off"]) == (k, var, off)]
        mm = [r for r in rs if r["mismatch"]]
        P(
            f"{k:6s} {var:14s} off {off:4d}: n {len(rs):4d} mismatch {len(mm):3d} explained by -1 min {sum(r['by_minus'] for r in mm):3d} "
            f"by +1 min {sum(r['by_plus'] for r in mm):3d} far (>{FAR_FROM_STRIKE:.1%}) {sum((r['dist'] or 0) > FAR_FROM_STRIKE for r in mm):3d}"
        )
    for r in [r for r in prim if r["mismatch"]][:40]:
        P(
            f"  mismatch {r['kind']} {r['asset']} {r['date']} mkt {r['market_id']} close {r['close']} ref {r['strike'] or r['prev']} dist {r['dist']:.5f} -1:{r['by_minus']} +1:{r['by_plus']}"
        )
    force, vl = validity(prim, cache, meta)
    L += vl

    P("\n== Registered gates (primary: nearest strike / daily up-down, price at t_obs+5, half-spread 1 c) ==")
    g = gates(prim, force=force)
    g, nl = nonexact_guard(prim, g, gates, force)
    L += nl
    if g["K3"]["verdict"] == "kill":  # registered: a K3 kill that exists only through closedTime lateness is undecided
        n_alt = sum(r["nonpay"] and not r["paid_7d_uma"] for r in prim)  # a late event paid within 7 d by umaEndDate
        if n_alt / len(prim) <= K3_NONPAY_KILL:
            force["K3"] = f"kill only through closedTime > 7 d; by umaEndDate nonpay {n_alt}/{len(prim)}: check on-chain resolution"
            g = gates(prim, force=force)
    P(fmt(g))
    P(f"REGISTERED CARD OUTCOME: {g['card']}")
    p5 = [(r["utc_day"], r["p5"]) for r in prim if r.get("p5") is not None]
    P(f"K1 median with day-cluster 95% CI: {median_cluster_ci(p5)}")
    rr = [(r["utc_day"], r["market_id"], x) for r in prim if (x := ret(r)) is not None]
    P(f"K2 mean with day-cluster 95% CI: {cluster_bootstrap_ci(rr)}")
    n_pr = len(p5)
    P(f"entered (ask proxy <= {ENTRY_MAX_ASK}): {len(rr)} of {n_pr} priced ({(len(rr) / n_pr if n_pr else 0):.1%})")
    losers = sorted((x, d, mid) for d, mid, x in rr if x < 0)
    P(f"entered events with a loss: {len(losers)}: {losers[:50]}")
    P(
        f"settlement: mismatch {sum(r['mismatch'] for r in prim)}, unresolved/50-50 {sum(r['resolved'] is None for r in prim)}, "
        f"late >7d {sum(r['paid'] and not r['paid_7d'] for r in prim)}, disputed flag {sum(r['disputed'] for r in prim)}, "
        f"resolved within tolerance but not exactly (1,0)/(0,1) {sum(r['resolved'] is not None and not r['exact_binary'] for r in prim)}"
    )
    lags = sorted(r["lag_s"] / 3600 for r in prim if r["lag_s"] is not None)
    ulags = sorted(r["uma_lag_s"] / 3600 for r in prim if r["uma_lag_s"] is not None)
    if lags:
        P(
            f"lag t_obs->closedTime h: median {statistics.median(lags):.2f}, p10 {lags[len(lags) // 10]:.2f}, p90 {lags[9 * len(lags) // 10]:.2f}"
        )
    if ulags:
        P(f"lag t_obs->umaEndDate h: median {statistics.median(ulags):.2f}")
    P(f"fees enabled on {sum(r['fee_rate'] > 0 for r in prim)} primary markets")

    P("\n== Sensitivity (exploratory, no decision power) ==")
    P(
        f"K1 read as 'median ask proxy p+0.01 >= 0.993' (i.e. median p5 >= 0.983): median p5+0.01 = {statistics.median([x for _, x in p5]) + 0.01 if p5 else None}"
    )
    if p5:
        P(
            f"descriptive upper bound for the forward criterion (not a gate): share p5 <= 0.985 {sum(x <= 0.985 for _, x in p5) / n_pr:.3f}, "
            f"share p5+0.01 <= 0.985 {sum(x + 0.01 <= 0.985 for _, x in p5) / n_pr:.3f}"
        )
    for key in ("p2", "p15"):
        P(f"price at {key}: {fmt(gates(prim, key))}")
    for hs in (0.005, 0.02):
        P(f"half-spread {hs}: {fmt(gates(prim, 'p5', hs))}")
    P(f"settlement time = umaEndDate (K2 hold, K3 7-day window): {fmt(gates(prim, nonpay_key='nonpay_uma', hold_key='uma_lag_s'))}")
    P(f"ladder 3 nearest strikes + up/down: {fmt(gates([r for r in rows if r['kind'] == 'updown' or r['rank'] < LADDER_K]))}")
    uncond = [x for r in prim if (x := ret(r, max_ask=0.999)) is not None]
    P(
        f"unconditional (buy every priced event at p+1c, ask <= 0.999): mean {statistics.fmean(uncond) * 100 if uncond else float('nan'):.3f}% n={len(uncond)}"
    )
    base = [r["pm30"] for r in prim if r.get("pm30") is not None]
    P(f"base t_obs-30min winning-side median {statistics.median(base) if base else None} n={len(base)}")
    for kind in KINDS:
        P(f"kind {kind}: {fmt(gates([r for r in prim if r['kind'] == kind]))}")
    for k, var, off in sorted({(r["kind"], r["candle_var"], r["candle_off"]) for r in prim}):
        P(f"candle {k}/{var}/{off}: {fmt(gates([r for r in prim if (r['kind'], r['candle_var'], r['candle_off']) == (k, var, off)]))}")
    P(f"time split first half (< {half}): {fmt(gates([r for r in prim if r['utc_day'] < half]))}")
    P(f"time split second half: {fmt(gates([r for r in prim if r['utc_day'] >= half]))}")
    rets_sorted = sorted(x for *_, x in rr)
    if len(rets_sorted) >= 20:
        cut = rets_sorted[: int(len(rets_sorted) * 0.95)]
        P(f"K2 without top 5% returns: mean {statistics.fmean(cut) * 100:.3f}% n={len(cut)}")
    for lab, sel in (  # re-review note F: descriptive, no decision power
        ("without entries with p5 == pre-T price", lambda r: r.get("pre") is None or r["p5"] != r["pre"]),
        ("only entries with p5 >= 0.9", lambda r: r["p5"] >= 0.9),
    ):
        sub = [x for r in prim if r.get("p5") is not None and sel(r) and (x := ret(r)) is not None]
        m = statistics.fmean(sub) if sub else None
        flag = g["K2"]["verdict"] == "pass" and (m is None or m < K2_RETURN_KILL)
        P(
            f"K2 {lab} (descriptive): mean {'—' if m is None else f'{m * 100:.3f}%'} n={len(sub)}"
            + (" FLAG: registered K2 'pass' does not hold without the excluded entries" if flag else "")
        )

    P("\n== Per series (5a) ==")
    means, ses, first, second = [], [], {}, {}
    for kind in KINDS:
        for asset in ASSETS:
            rs = [r for r in prim if r["kind"] == kind and r["asset"] == asset]
            xs = [(r["utc_day"], r["market_id"], x) for r in rs if (x := ret(r)) is not None]
            ci = cluster_bootstrap_ci(xs) if xs else {"mean": None, "lo": None, "hi": None}
            ps = [r["p5"] for r in rs if r.get("p5") is not None]
            P(
                f"{kind:6s} {asset:8s} events {len(rs):4d} priced {len(ps):4d} median p5 {statistics.median(ps) if ps else float('nan'):.4f} "
                f"entered {len(xs):4d} mean r {ci['mean']} CI [{ci['lo']}, {ci['hi']}] nonpay {sum(r['nonpay'] for r in rs)}"
            )
            if len(xs) >= 2:
                vals = [x for *_, x in xs]
                means.append(statistics.fmean(vals))
                ses.append(statistics.stdev(vals) / len(vals) ** 0.5)
            key = f"{kind}/{asset}"
            first[key] = [x for d, _, x in xs if d < half]
            second[key] = [x for d, _, x in xs if d >= half]
    P(f"Cochran Q on per-series mean r: {cochran_q(means, ses)}")
    ranked = sorted((k for k in first if first[k]), key=lambda k: -statistics.fmean(first[k]))
    top = ranked[: max(1, len(ranked) // 4)]
    t2 = [x for k in top for x in second[k]]
    o2 = [x for k in ranked[len(top) :] for x in second[k]]
    P(
        f"persistence: top quarter by first half {top}: second-half mean {statistics.fmean(t2) * 100 if t2 else float('nan'):.3f}% n={len(t2)}; "
        f"rest {statistics.fmean(o2) * 100 if o2 else float('nan'):.3f}% n={len(o2)}"
    )
    out_rows = cache / "full_rows.json"
    _write(out_rows, rows)
    P(f"\nrows written to {out_rows}")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True, choices=["list", "spot", "events", "history", "coverage", "full", "all"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--refresh", action="store_true", help="list: re-walk Gamma even if the market files exist")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    st = a.stage
    if st in ("list", "all"):
        asyncio.run(stage_list(cache, a.refresh))
    if st in ("spot", "all"):
        asyncio.run(stage_spot(cache))
    if st in ("events", "all"):
        stage_events(cache)
    if st in ("history", "all"):
        asyncio.run(stage_history(cache, a.limit, a.conc))
    if st == "coverage":
        stage_coverage(cache)
    if st in ("full", "all"):
        lines = analyse(cache)
        text = "\n".join(lines)
        if a.out:
            Path(a.out).expanduser().write_text(text + "\n")
        buf = io.StringIO(text)
        for line in buf.readlines()[:120]:
            print(line.rstrip())


if __name__ == "__main__":
    main()
