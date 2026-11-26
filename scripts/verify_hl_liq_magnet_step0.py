"""Step 0 of docs/ideas/IDEA-20261025-hl-whale-liquidation-magnet.md: a BLIND count of whale positions near their
public liquidation price on Hyperliquid (no outcome: no price after an episode start is read).

Reads the collector store READ-ONLY (``mode=ro``): `trader_positions` (change log), `trader_snapshot` (watch list),
and for the recount `trader_polls` (poll markers). Hyperliquid meta and candles through the runtime provider
(``build_server()`` -> ``rt.hyperliquid``), cached per file under ``--cache-dir``. Definitions: the card's «Попередня
реєстрація»; pure logic in ``tradedesk.analytics.hl_liq_magnet``.

    uv run python scripts/verify_hl_liq_magnet_step0.py --smoke          # data checks + pipeline on 8 accounts, no count
    uv run python scripts/verify_hl_liq_magnet_step0.py --prefetch       # candles of every top-50 coin into the cache, no count
    uv run python scripts/verify_hl_liq_magnet_step0.py --out ~/.cache/tradedesk-agents/hl_liq_magnet/result.json

Recount (registration: «Повторна лічба (ред. 3)» with «ред. 4 правил»; 42 calendar days from the first run with a
poll marker, end excluded; the reconstructed watch list is not used, the markers decide who is observed and polled):

    uv run python scripts/verify_hl_liq_magnet_step0.py --recount --smoke      # structural checks only, any time
    uv run python scripts/verify_hl_liq_magnet_step0.py --recount --prefetch   # from the period end + 1 h, within 2 days
    uv run python scripts/verify_hl_liq_magnet_step0.py --recount --out ~/.cache/tradedesk-agents/hl_liq_magnet_recount/result.json

Exit codes (each has a registered consequence in the card, rev. 3): 0 done (full run: the line ``STEP 0 OUTCOME``;
recount: ``RECOUNT OUTCOME``); 3 daily candles of a coin still failing after retries, or a live coin with none in the
window that was not listed after its end (the top-50 ranking would admit the 51st coin): no count, rerun;
4 refused, nothing was counted: history mode with ``--end`` past the registered ``END`` (any mode: the window would
reach into the recount period); recount: no poll markers, the period has not ended, the count is past its 7-day
deadline without ``--allow-late``, or a late count (``--allow-late``) with a coin whose 15m history is short or failed. Any other non-zero: a crash; what it means before and after the variant table
is printed is in the card. A coin whose 15m candles fail or do not reach the period start is not an exit: its marks
are unknown (None), which only lowers the lower bound; it is listed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common_helpers import connect_ro, read_json, rss_mb, write_json  # noqa: E402

from tradedesk.analytics.hl_liq_magnet import (  # noqa: E402
    DAY,
    HOUR,
    RECOUNT_DAYS,
    Variant,
    account_observations,
    close_reopen_pairs,
    daily_turnover,
    detect_episodes,
    exposure_seconds,
    history_covers,
    listed_after,
    mark_at,
    recount_completeness,
    recount_outcome,
    recount_period,
    recount_runs,
    recount_stage,
    run_gaps,
    snapshot_age_days,
    step0_decision,
    summarize,
    top_coins_at,
    watchlist_at,
    watchlists_by_day,
)
from tradedesk.analytics.smart_money import detect_delta_start  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "hl_liq_magnet"
RECOUNT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "hl_liq_magnet_recount"  # fresh meta for the recount
END = "2026-11-25T00:00:00Z"  # registered period end (exclusive)
M15 = 900
TOP_N_COINS = 50
TURNOVER_DAYS = 30
MIN_VALUE = 3_000_000.0
BAND = (0.01, 0.04)  # gate band (forward trigger)
BAND5 = (0.0, 0.05)  # descriptive: 0 < d <= 5 %
REFRACTORY = (0, DAY, 3 * DAY)  # 24 h is primary
GAP_S = 5400
MIN_STEP_S = 3000  # repeated runs closer than this are only reported (exposure does not count runs)
# primary = fresh liq_px (row written at this run), counted in lower-bound mode; the rest is descriptive only
VARIANTS = {
    "fresh_1_4": Variant(*BAND, 0),
    "le24h_1_4": Variant(*BAND, DAY),
    "any_1_4": Variant(*BAND, None),
    "fresh_0_5": Variant(*BAND5, 0),
    "any_0_5": Variant(*BAND5, None),
}
PRIMARY = "fresh_1_4"
RETRY_WAITS = (5, 20, 60)  # seconds before each retry of a candle fetch that raised ProviderError (after Http's own)
SMOKE_COINS = ["BTC", "ETH", "HYPE", "ZEC", "kPEPE"]
SMOKE_ACCOUNTS = 8


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: UP017


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# --- network (through the provider, cached per file) -------------------------------------------------------------


async def fetch_meta(rt, cache: Path) -> list[dict[str, Any]]:
    p = cache / "meta.json"
    if p.exists():
        return read_json(p)
    rows = (await rt.hyperliquid.asset_contexts(include_delisted=True)).data
    write_json(p, rows)
    return rows


async def fetch_candles(rt, coin: str, interval: str, start: int, end: int, cache: Path, span_s: int) -> list[dict]:
    """Candles [start, end) in windows of ``span_s`` (each well under HL's 5000-candle cap), one cache file per window
    once the window is complete."""
    out: list[dict] = []
    t = start
    while t < end:
        w_end = min(end, t + span_s)
        p = cache / interval / f"{coin}_{t}_{w_end}.json"
        if p.exists():
            got = read_json(p)
        else:
            got = (await rt.hyperliquid.candles(coin, interval, t * 1000, w_end * 1000 - 1)).data or []
            if got and w_end < time.time() - DAY:  # an empty answer is never cached, so a rerun asks again
                write_json(p, got)
        out += [c for c in got if t * 1000 <= int(c["t"]) < w_end * 1000]
        t = w_end
    seen: dict[int, dict] = {int(c["t"]): c for c in out}
    return [seen[k] for k in sorted(seen)]


async def fetch_retry(rt, coin: str, interval: str, start: int, end: int, cache: Path, span_s: int) -> list[dict]:
    """``fetch_candles`` retried on ProviderError (a network failure must not look like a missing history); the
    last failure is raised to the caller, which decides (1d: fatal; 15m: the coin's marks are unknown)."""
    for i, wait in enumerate((*RETRY_WAITS, None)):
        try:
            return await fetch_candles(rt, coin, interval, start, end, cache, span_s)
        except ProviderError as e:
            if wait is None:
                raise
            print(f"  {interval} {coin}: {e}; retry {i + 1}/{len(RETRY_WAITS)} in {wait}s", flush=True)
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


async def fetch_universe(smoke: bool, cache: Path, start: int, end: int, runs: list[int], held: set[str]) -> dict[str, Any] | int:
    """Meta, daily turnover (top-50 per day of ``runs``), 15m marks of the held top-50 union for [start - 1 day, end)
    and the 15m depth check. Returns the pieces, or the exit code 3 (daily candles missing: no count)."""
    from tradedesk.server import build_server  # noqa: PLC0415

    _, rt = build_server()
    failed15: list[str] = []
    try:
        meta = await fetch_meta(rt, cache)
        coins = sorted(r["coin"] for r in meta)
        vol24 = {r["coin"]: r.get("day_volume_usd") for r in meta}
        print(
            f"meta coins={len(coins)} (delisted incl. {sum(1 for r in meta if r.get('delisted'))}); "
            f"held markets not in meta: {sorted(held - set(coins))[:10]}",
            flush=True,
        )
        if smoke:
            coins = list(SMOKE_COINS)
        d_start = start // DAY * DAY - (TURNOVER_DAYS + 1) * DAY
        turnover: dict[str, dict[int, float]] = {}
        tf = time.time()
        for i, c in enumerate(coins):
            try:
                turnover[c] = daily_turnover(await fetch_retry(rt, c, "1d", d_start, end, cache, 200 * DAY))
            except ProviderError as e:
                print(f"FATAL: 1d candles of {c} still fail after {len(RETRY_WAITS)} retries: {e}", flush=True)
                print("without them the top-50 ranking would admit the 51st coin; no count. Rerun later (cache kept).")
                return 3
            if i % 40 == 0:
                print(f"  1d {i + 1}/{len(coins)} {time.time() - tf:.0f}s rss={rss_mb():.0f}MB", flush=True)
        delisted = {r["coin"] for r in meta if r.get("delisted")}
        no1d = [c for c in coins if not turnover.get(c)]
        print(
            f"coins without any 1d candle in the window: {len(no1d)} ({sum(c in delisted for c in no1d)} delisted): {no1d[:15]}",
            flush=True,
        )
        unexplained: list[str] = []  # live coins with an empty 1d answer that a later listing does not explain
        for c in (x for x in no1d if x not in delisted):
            try:
                later = (await rt.hyperliquid.candles(c, "1d", d_start * 1000, int(time.time()) * 1000)).data or []
            except ProviderError:
                later = []
            if not listed_after(min((int(x["t"]) // 1000 for x in later), default=None), end):
                unexplained.append(c)
        if unexplained:
            print(f"FATAL: live coins without 1d candles in the window: {unexplained[:15]}")
            print("without them the top-50 ranking would admit the 51st coin; no count. Rerun later (cache kept).")
            return 3
        days = sorted({r // DAY * DAY for r in runs})
        top_by_day = {d: top_coins_at(turnover, d + 1, TOP_N_COINS, TURNOVER_DAYS) for d in days}
        union = sorted(set().union(*top_by_day.values()) & held) if not smoke else coins
        print(
            f"top-{TOP_N_COINS} union over the period: {len(set().union(*top_by_day.values()))} coins, "
            f"{len(union)} of them held by watched accounts",
            flush=True,
        )
        # turnover unit check: the latest completed day vs the live 24 h notional volume
        for c in ("BTC", "ETH"):
            last = max(turnover.get(c, {0: 0.0}).items())
            print(
                f"  turnover check {c}: last day {_iso(last[0])[:10]} v*close=${last[1] / 1e6:,.0f}M  "
                f"live dayNtlVlm=${(vol24.get(c) or 0) / 1e6:,.0f}M"
            )
        marks: dict[str, tuple[list[int], list[float]]] = {}
        tf = time.time()
        for i, c in enumerate(union):
            try:
                cs = await fetch_retry(rt, c, "15m", start - DAY, end, cache, 30 * DAY)
            except ProviderError as e:  # registered: marks unknown (None), which only lowers the lower bound
                print(f"  15m {c}: still failing after retries, its marks are unknown: {e}", flush=True)
                failed15.append(c)
                continue
            marks[c] = ([int(x["t"]) // 1000 for x in cs], [float(x["c"]) for x in cs])
            if i % 10 == 0 or smoke:
                first = _iso(marks[c][0][0]) if cs else "-"
                print(f"  15m {i + 1}/{len(union)} {c} n={len(cs)} from {first} {time.time() - tf:.0f}s rss={rss_mb():.0f}MB", flush=True)
    finally:
        await rt.aclose()

    # --- 15m history must reach the period start (HL keeps only the last 5000 candles, ~52 days of 15m) ----------
    short = []
    for c in union:
        first15 = marks[c][0][0] if c in marks and marks[c][0] else None
        listing = min(turnover[c]) if turnover.get(c) else None
        if not history_covers(first15, listing, start, M15):
            short.append((c, _iso(first15) if first15 else "-"))
    print(f"15m history check: {len(union) - len(short)}/{len(union)} coins cover {_iso(start)} (or their listing day)")
    if short:  # rev. 3: no stop; missing marks are unknown (None) and only lower the bound; reported
        print(f"  incomplete 15m history (early marks unknown): {len(short)} coins {short[:20]}; fetch failed: {failed15}")
    return {"top_by_day": top_by_day, "union": union, "marks": marks, "short": short, "failed15": failed15}


# --- store -------------------------------------------------------------------------------------------------------


def load_store(db: str, end: int) -> dict[str, Any]:
    con = connect_ro(db, timeout=30)
    try:
        runs = [
            r[0] for r in con.execute("SELECT DISTINCT ts FROM trader_positions WHERE venue='hyperliquid' AND ts < ? ORDER BY ts", (end,))
        ]
        closed = [
            r[0] for r in con.execute("SELECT ts FROM trader_positions WHERE venue='hyperliquid' AND side='closed' AND ts < ?", (end,))
        ]
        snap = [
            dict(zip(("day", "ts", "account", "account_value", "pnl_month", "pnl_all"), r, strict=True))
            for r in con.execute(
                "SELECT day, ts, account, account_value, pnl_month, pnl_all FROM trader_snapshot WHERE venue='hyperliquid' AND ts < ?",
                (end,),
            )
        ]
        accounts = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT account FROM trader_positions WHERE venue='hyperliquid' AND ts < ? ORDER BY account", (end,)
            )
        ]
        markets = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT market FROM trader_positions WHERE venue='hyperliquid' AND ts < ? AND market != ''", (end,)
            )
        ]
        n_rows, n_liq_null = con.execute(
            "SELECT COUNT(*), SUM(liq_px IS NULL) FROM trader_positions WHERE venue='hyperliquid' AND ts < ? AND side IN ('long','short')",
            (end,),
        ).fetchone()
        null_by_side = con.execute(
            "SELECT side, value_usd >= ?, COUNT(*), SUM(liq_px IS NULL) FROM trader_positions "
            "WHERE venue='hyperliquid' AND ts < ? AND side IN ('long','short') GROUP BY 1, 2",
            (MIN_VALUE, end),
        ).fetchall()
    finally:
        con.close()
    return {
        "runs": runs,
        "closed": closed,
        "snap": snap,
        "accounts": accounts,
        "markets": markets,
        "n_rows": n_rows,
        "n_liq_null": n_liq_null,
        "null_by_side": null_by_side,
    }


def account_groups(db: str, account: str, end: int) -> list[tuple[int, list[dict]]]:
    con = connect_ro(db, timeout=30)
    try:
        cols = ("ts", "account", "market", "side", "size", "value_usd", "liq_px")
        rows = [
            dict(zip(cols, r, strict=True))
            for r in con.execute(
                f"SELECT {', '.join(cols)} FROM trader_positions WHERE venue='hyperliquid' AND account=? AND ts < ? ORDER BY ts",
                (account, end),
            )
        ]
    finally:
        con.close()
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(int(r["ts"]), []).append(r)
    return sorted(groups.items())


def load_polls_from(db: str) -> int | None:
    """The first collector run with a Hyperliquid poll marker (registered start of the recount period), or None."""
    con = connect_ro(db, timeout=30)
    try:
        v = con.execute("SELECT MIN(ts) FROM trader_polls WHERE venue='hyperliquid'").fetchone()[0]
    finally:
        con.close()
    return None if v is None else int(v)


def load_recount_store(db: str, start: int, end: int) -> dict[str, Any]:
    """What the recount needs from the store. The whole log before ``end`` is replayed (the state at ``start`` is the
    baseline replayed from the log, and ``delta_start`` is the v12 cutover of the whole log, as on the history);
    observations, runs and markers are limited to the period [start, end)."""
    con = connect_ro(db, timeout=30)
    try:
        log_runs = [
            r[0] for r in con.execute("SELECT DISTINCT ts FROM trader_positions WHERE venue='hyperliquid' AND ts < ? ORDER BY ts", (end,))
        ]
        closed = [
            r[0] for r in con.execute("SELECT ts FROM trader_positions WHERE venue='hyperliquid' AND side='closed' AND ts < ?", (end,))
        ]
        polls: dict[str, dict[int, bool]] = {}
        n_markers = 0
        for acc, ts, ok in con.execute(
            "SELECT account, ts, ok FROM trader_polls WHERE venue='hyperliquid' AND ts >= ? AND ts < ? ORDER BY ts", (start, end)
        ):
            polls.setdefault(str(acc), {})[int(ts)] = bool(ok)
            n_markers += 1
        logged = [
            str(r[0])
            for r in con.execute(
                "SELECT DISTINCT account FROM trader_positions WHERE venue='hyperliquid' AND ts < ? ORDER BY account", (end,)
            )
        ]
        markets = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT market FROM trader_positions WHERE venue='hyperliquid' AND ts < ? AND market != ''", (end,)
            )
        ]
        n_rows = con.execute(
            "SELECT COUNT(*) FROM trader_positions WHERE venue='hyperliquid' AND ts >= ? AND ts < ?", (start, end)
        ).fetchone()[0]
        in_period = {
            str(r[0])
            for r in con.execute(
                "SELECT DISTINCT account FROM trader_positions WHERE venue='hyperliquid' AND ts >= ? AND ts < ?", (start, end)
            )
        }
        snap_days = con.execute(
            "SELECT day, MIN(ts) FROM trader_snapshot WHERE venue='hyperliquid' AND ts < ? GROUP BY day", (end,)
        ).fetchall()
    finally:
        con.close()
    poll_ts = sorted({t for p in polls.values() for t in p})
    return {
        "log_runs": log_runs,
        "closed": closed,
        "runs": recount_runs(log_runs, poll_ts, start, end),
        "poll_ts": poll_ts,
        "polls": polls,
        "n_markers": n_markers,
        # accounts with rows in the log (state) or markers in the period: also those with markers and no rows at all
        "accounts": sorted(set(logged) | set(polls)),
        "accounts_marked_no_rows": len(set(polls) - set(logged)),
        "accounts_rows_in_period": len(in_period),
        "markets": markets,
        "n_rows_period": n_rows,
        "snap_days": [(_ts(f"{d}T00:00:00Z"), int(t)) for d, t in snap_days],
    }


# --- episodes and the report (shared by the history count and the recount) ---------------------------------------


def new_tallies() -> tuple[dict[tuple[str, int], list[dict]], Counter[str]]:
    return {(v, rf): [] for v in VARIANTS for rf in REFRACTORY}, Counter()


def add_account(
    episodes: dict[tuple[str, int], list[dict]],
    counters: Counter[str],
    acc: str,
    groups: list[tuple[int, list[dict]]],
    runs: list[int],
    **obs_kw: Any,
) -> None:
    """One account's observations (``account_observations``) -> its episodes in every variant and refractory."""
    obs, meta_ep, cnt = account_observations(groups, runs, variants=VARIANTS, min_value=MIN_VALUE, gap_s=GAP_S, **obs_kw)
    counters.update(cnt)
    for v, var in VARIANTS.items():
        lb = var.max_liq_age_s == 0  # fresh liq_px only: lower-bound rule; 'any age' and '<= 24 h': rule of rev. 1
        for c, ob in obs[v].items():
            for rf in REFRACTORY:
                for e in detect_episodes(ob, rf, lower_bound=lb):
                    episodes[(v, rf)].append(
                        {
                            "ts": e.ts,
                            "account": acc,
                            "coin": c,
                            "side": e.side,
                            "kind": e.kind,
                            "prev_known_ts": e.prev_known_ts,
                            **meta_ep.get((c, e.ts), {}),
                        }
                    )


def print_counters(counters: Counter[str], unpolled_label: str) -> None:
    print(
        f"position-runs >= $3M on top-{TOP_N_COINS}: {counters['in_scope']} (liq_px fresh: {counters['in_scope_fresh']}); "
        f"liq_px already crossed: {counters['crossed']} (fresh: {counters['crossed_fresh']}); no mark: {counters['no_mark']} "
        f"(logged value >= $3M: {counters['no_mark_big']}, "
        f"{counters['no_mark_big'] / max(1, counters['in_scope'] + counters['no_mark_big']):.1%} of in-scope + them); "
        f"account-runs not polled ({unpolled_label}): {counters['unpolled_runs']}/{counters['observed_runs']}  rss={rss_mb():.0f}MB",
        flush=True,
    )


def report(
    episodes: dict[tuple[str, int], list[dict]],
    result: dict[str, Any],
    exposure: float,
    start: int,
    end: int,
    outcome_line: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """The variant table and the registered gates on the primary counter; fills ``result`` and returns the decision."""
    print(
        f"\n{'variant':10} {'refr':>5} {'kinds':14} {'n':>4} {'/wk':>5} {'coin-d':>6} {'L/S':>7} {'top coin':>14} {'top acc':>7}  blocks10d"
    )
    for (v, rf), eps in sorted(episodes.items()):
        for label, kinds in (
            ("gate", {"fresh", "join", "gap"}),
            ("fresh", {"fresh"}),
            ("start", {"start"}),
            ("join", {"join"}),
            ("gap", {"gap"}),
        ):
            sel = [e for e in eps if e["kind"] in kinds]
            s = summarize(sel, exposure, (start, end))
            result["variants"][f"{v}|{rf // HOUR}h|{label}"] = {**s, "episodes": sel if label == "gate" else None}
            if label in ("gate", "fresh", "start") or (v, rf) == (PRIMARY, DAY):
                print(
                    f"{v:10} {rf // HOUR:>4}h {label:14} {s['n']:>4} {s['per_week'] or 0:5.2f} {s['coin_days']:>6} "
                    f"{s['long']:>3}/{s['short']:<3} {str(s['top_coin']):>8} {s['top_coin_share'] or 0:5.0%} "
                    f"{s['top_account_share'] or 0:7.0%}  {s['blocks']}"
                )
    prim = [e for e in episodes[(PRIMARY, DAY)] if e["kind"] != "start"]
    dec = step0_decision(prim, exposure)
    g, g_cl = dec["episodes"], dec["coin_days"]
    ages = sorted(e["liq_age_h"] for e in prim if "liq_age_h" in e)
    multi = sum(1 for e in prim if e.get("n_pos", 0) > 1)
    span_day = sum(1 for e in prim if e["prev_known_ts"] is not None and e["prev_known_ts"] // DAY != e["ts"] // DAY)
    print(
        f"\nGATE (fresh liq_px, lower bound; band [1%;4%], >= $3M, top-{TOP_N_COINS}, refractory 24h, excl. 'start'):\n"
        f"  episodes n={len(prim)} per_week={g['per_week']:.2f} per_10d={g['per_10d']:.2f} -> {'pass' if g['passed'] else 'no'}\n"
        f"  coin-day clusters n={g_cl['n']} per_week={g_cl['per_week']:.2f} per_10d={g_cl['per_10d']:.2f} "
        f"-> {'pass' if g_cl['passed'] else 'no'}\n"
        f"  {outcome_line(dec)}; weeks to 90 events from clusters (rough estimate, not a bound): "
        f"{dec['weeks_to_need_clusters']}"
    )
    de = dec["descriptive"]
    wo = de["without_top_account"]
    print(
        f"  descriptive: strict coin-day lower bound={de['coin_days_strict']}; "
        f"UTC days with an episode={de['days']}; top account has {de['top_account_n']} episodes; "
        f"without it n={wo['n']} ({wo['episodes']['per_week']:.2f}/wk), coin-days={wo['coin_days']} "
        f"({wo['clusters']['per_week']:.2f}/wk); entry interval (last known, start] crosses a UTC day: {span_day}/{len(prim)}"
    )
    if ages:
        print(
            f"liq_px age at start (h since the row that set it): median={statistics.median(ages):.0f} "
            f"p90={ages[int(0.9 * len(ages))]:.0f}; accounts with >1 open position: {multi}/{len(prim)}"
        )
    print("coins:", summarize(prim, exposure, (start, end))["coins"])
    result["decision"] = dec
    return dec


# --- main: history (step 0) --------------------------------------------------------------------------------------


async def main() -> int:  # noqa: C901, PLR0912, PLR0915 - one linear report
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--cache-dir", type=Path, default=None, help=f"default {CACHE} ({RECOUNT_CACHE} with --recount)")
    ap.add_argument("--out", type=Path, default=None, help="full result JSON")
    ap.add_argument("--end", default=None, help=f"history period end (default {END}; later is refused); not allowed with --recount")
    ap.add_argument("--top-n", type=int, default=100, help="watch list per ranking (collector setting)")
    ap.add_argument("--min-account", type=float, default=500_000.0)
    ap.add_argument("--smoke", action="store_true", help="data checks on a few coins only; no episode metric")
    ap.add_argument("--prefetch", action="store_true", help="fetch and cache the candles only, then stop; no episode metric")
    ap.add_argument("--recount", action="store_true", help="the registered recount on the poll-marker period")
    ap.add_argument("--allow-late", action="store_true", help="recount count past its 7-day deadline (a reported deviation)")
    a = ap.parse_args()
    if a.recount:
        return await recount_main(a)
    cache: Path = a.cache_dir or CACHE
    end = _ts(a.end or END)
    if end > _ts(END):
        # The end is the only history option that moves the window (the start is the first row of the log; --db,
        # --top-n, --min-account and --cache-dir do not change the time range). Past the registered END the window
        # holds runs of the recount period, which the history path would count (and every mode, smoke and prefetch
        # included, prints their runs, exposure and gaps) before the recount: refused in every mode.
        print(f"REFUSED: --end {_iso(end)} is past the registered history end {_iso(_ts(END))} UTC; later runs belong to")
        print("the recount (--recount), which is counted only after its period ends; nothing read, nothing counted")
        return 4
    t0 = time.time()

    st = load_store(a.db, end)
    runs: list[int] = st["runs"]
    start = runs[0]
    gaps = run_gaps(runs, GAP_S)
    exposure = exposure_seconds(runs, end, GAP_S)  # registered: calendar time minus gaps, not the number of runs
    missing_h = sum((b - a_) / HOUR - 1 for a_, b in gaps)
    short_steps = sum(1 for x, y in zip(runs, runs[1:], strict=False) if y - x < MIN_STEP_S)
    print(
        f"period {_iso(start)} .. {_iso(end)} UTC  runs={len(runs)} (steps < {MIN_STEP_S // 60} min: {short_steps})  "
        f"calendar_h={(end - start) / HOUR:.0f}  exposure_h={exposure / HOUR:.1f}  "
        f"exposure_weeks={exposure / (7 * DAY):.2f}  accounts={len(st['accounts'])}  rss={rss_mb():.0f}MB",
        flush=True,
    )
    print(
        f"gaps (> {GAP_S // 60} min between runs): {len(gaps)}, ~{missing_h:.0f} h without a run; longest: "
        + ", ".join(f"{_iso(x)}->{_iso(y)}" for x, y in sorted(gaps, key=lambda g: g[0] - g[1])[:5])
    )
    print(f"position rows={st['n_rows']}  liq_px null={st['n_liq_null'] / max(1, st['n_rows']):.1%}")
    for side, big, n, nn in st["null_by_side"]:
        print(f"  liq_px null {side:5} value_usd {'>= $3M' if big else '<  $3M'}: {nn}/{n} = {nn / max(1, n):.1%}")
    delta_start = detect_delta_start(runs, st["closed"])
    lists = watchlists_by_day(st["snap"], a.top_n, a.min_account)
    print(
        f"delta_start={_iso(delta_start) if delta_start else None}  watch-list days={len(lists)}  "
        f"size median={statistics.median(len(x[2]) for x in lists) if lists else 0}"
    )

    uni = await fetch_universe(a.smoke, cache, start, end, runs, set(st["markets"]))
    if isinstance(uni, int):
        return uni
    top_by_day, marks = uni["top_by_day"], uni["marks"]
    if a.prefetch:
        print(f"prefetch done in {time.time() - t0:.0f}s rss={rss_mb():.0f}MB (candles cached; no replay, no episode metric)")
        return 0

    def top_at(ts: int) -> frozenset[str]:
        return top_by_day.get(ts // DAY * DAY, frozenset())

    # --- sanity: mark from candles vs value_usd / size on rows written at the same run; watch-list consistency ---
    rel: list[float] = []
    off_list = on_list = 0
    accounts = st["accounts"][:SMOKE_ACCOUNTS] if a.smoke else st["accounts"]
    per_account: list[tuple[str, list[tuple[int, list[dict]]]]] = []
    for acc in accounts:
        groups = account_groups(a.db, acc, end)
        for ts, rows in groups:
            wl = watchlist_at(lists, ts)
            if wl is not None:
                on_list += acc in wl
                off_list += acc not in wl
            rel += _mark_errors(rows, marks, ts)
        per_account.append((acc, groups))
    _print_mark_check(rel)
    print(
        f"watch-list check: account-runs with rows on the reconstructed list {on_list}, off it {off_list} "
        f"({off_list / max(1, on_list + off_list):.2%})  rss={rss_mb():.0f}MB",
        flush=True,
    )

    # --- episodes ---------------------------------------------------------------------------------------------
    episodes, counters = new_tallies()

    def mark(c: str, r: int) -> float | None:
        return mark_at(*marks[c], r, M15) if c in marks else None

    for acc, groups in per_account:

        def watched(r: int, acc: str = acc) -> bool:
            wl = watchlist_at(lists, r)
            return wl is not None and acc in wl

        add_account(episodes, counters, acc, groups, runs, delta_start=delta_start, watched=watched, top_at=top_at, mark=mark)
    if a.smoke:  # the pipeline ran end to end on a few accounts and coins; every count is withheld by design
        step0_decision([], HOUR)
        print(f"episode pipeline ran on {len(per_account)} accounts x {len(VARIANTS)} variants x {len(REFRACTORY)} refractories")
        print(f"smoke done in {time.time() - t0:.0f}s (no episode metric by design)")
        return 0
    print_counters(counters, "pre-v12, no group")

    result: dict[str, Any] = {
        "period": [start, end],
        "runs": len(runs),
        "exposure_s": exposure,
        "gaps": gaps,
        "counters": dict(counters),
        "variants": {},
    }
    report(episodes, result, exposure, start, end, lambda d: f"STEP 0 OUTCOME: {d['outcome'].upper()}")
    if a.out:
        write_json(a.out.expanduser(), result)
        print(f"wrote {a.out}")
    print(f"done in {time.time() - t0:.0f}s rss={rss_mb():.0f}MB")
    return 0


def _mark_errors(rows: list[dict], marks: dict[str, tuple[list[int], list[float]]], ts: int) -> list[float]:
    """|value_usd / size / mark - 1| for the position rows written at run ``ts`` (units and symbols check)."""
    out: list[float] = []
    for r in rows:
        c = r["market"]
        if r["side"] in ("long", "short") and c in marks and r["size"] and r["value_usd"]:
            m = mark_at(*marks[c], ts, M15)
            if m:
                out.append(abs(abs(r["value_usd"]) / abs(r["size"]) / m - 1))
    return out


def _print_mark_check(rel: list[float]) -> None:
    rel.sort()
    if rel:
        q = lambda p: rel[min(len(rel) - 1, int(p * len(rel)))]  # noqa: E731
        print(
            f"mark check (value_usd/size at the run vs last completed 15m close): n={len(rel)} median={q(0.5):.4%} "
            f"p95={q(0.95):.3%} p99={q(0.99):.3%} share>5%={sum(1 for x in rel if x > 0.05) / len(rel):.3%}"
        )


# --- main: recount (registration «Повторна лічба (ред. 3)» + «ред. 4 правил») -------------------------------------


async def recount_main(a: argparse.Namespace) -> int:  # noqa: C901, PLR0912, PLR0915 - one linear report
    """The registered recount: period [polls_from, polls_from + 42 days), runs = marker ts ∪ row ts in it, observed /
    polled by the markers (``polls`` per account, ``polls_from``; the reconstructed watch list is not used), the state
    at ``polls_from`` replayed from the whole log, then ``recount_completeness`` and ``recount_outcome``. Stages:
    --smoke (any time, structural checks only), --prefetch and the count (both only after the period end)."""
    if a.end is not None:
        print("--end is fixed by the registration in --recount (42 days from the first marker); refused")
        return 4
    # a smoke runs before the period end: its meta must not become the count's cached meta, so a smoke always writes
    # to the 'smoke' subfolder of the cache dir (also of a custom --cache-dir that the count might reuse)
    base: Path = a.cache_dir or RECOUNT_CACHE
    cache: Path = base / "smoke" if a.smoke else base
    t0 = time.time()
    polls_from = load_polls_from(a.db)
    if polls_from is None:
        print("REFUSED: no Hyperliquid poll markers in trader_polls yet; nothing counted")
        return 4
    start, end = recount_period(polls_from)
    stage = "smoke" if a.smoke else "prefetch" if a.prefetch else "count"
    chk = recount_stage(stage, time.time(), end)
    print(
        f"recount period {_iso(start)} .. {_iso(end)} UTC ({RECOUNT_DAYS} days from the first marker, end excluded); stage={stage}",
        flush=True,
    )
    if not chk["allowed"]:
        print(f"REFUSED: the period has not ended; {stage} opens at {_iso(chk['opens_at'])} UTC; nothing counted")
        return 4
    if chk["late"]:
        print(f"WARNING: {stage} past its registered deadline {_iso(chk['deadline'])} UTC (a deviation for the report)")
        if stage == "count" and not a.allow_late:
            print("REFUSED: the count is past its 7-day deadline; rerun with --allow-late to count anyway (reported)")
            return 4

    st = load_recount_store(a.db, start, end)
    runs: list[int] = st["runs"]
    print(  # structural only: what was read, no share, no completeness, no count
        f"tables read: trader_polls markers={st['n_markers']} at {len(st['poll_ts'])} ts "
        f"(first {_iso(st['poll_ts'][0]) if st['poll_ts'] else '-'}); trader_positions rows in period={st['n_rows_period']}; "
        f"runs (marker ts ∪ row ts)={len(runs)}; accounts with markers={len(st['polls'])}, with rows in period="
        f"{st['accounts_rows_in_period']}, with markers and no rows ever={st['accounts_marked_no_rows']}; "
        f"accounts replayed={len(st['accounts'])}; snapshot days={len(st['snap_days'])}  rss={rss_mb():.0f}MB",
        flush=True,
    )
    if not runs:
        print("REFUSED: no run in the period; nothing counted")
        return 4
    delta_start = detect_delta_start(st["log_runs"], st["closed"])  # v12 cutover of the whole log, as on the history
    print(f"delta_start={_iso(delta_start) if delta_start else None} (whole log; the state at {_iso(start)} is replayed from it)")

    # coins whose first 15m window (the one HL drops first) was already cached, e.g. by the prefetch, before this run
    w0 = (start - DAY, min(end, start - DAY + 30 * DAY))
    cached15 = {p.name.rsplit("_", 2)[0] for p in (cache / "15m").glob(f"*_{w0[0]}_{w0[1]}.json")}
    uni = await fetch_universe(a.smoke, cache, start, end, runs, set(st["markets"]))
    if isinstance(uni, int):
        return uni
    top_by_day, marks = uni["top_by_day"], uni["marks"]
    first15_cached = sorted(set(uni["union"]) & cached15)
    print(f"first 15m window {_iso(w0[0])} .. {_iso(w0[1])} from the cache: {len(first15_cached)}/{len(uni['union'])} coins")
    if a.prefetch:
        print(f"prefetch done in {time.time() - t0:.0f}s rss={rss_mb():.0f}MB (candles cached; no replay, no metric)")
        return 0
    if chk["late"] and (uni["short"] or uni["failed15"]):  # only a count gets here late, and only with --allow-late
        print(
            f"REFUSED: a late count (--allow-late) with incomplete 15m history: short={uni['short'][:20]} "
            f"failed={uni['failed15']}; their early marks are unknown, which a count within 7 days would not lose; "
            "nothing counted"
        )
        return 4

    def top_at(ts: int) -> frozenset[str]:
        return top_by_day.get(ts // DAY * DAY, frozenset())

    def mark(c: str, r: int) -> float | None:
        return mark_at(*marks[c], r, M15) if c in marks else None

    def unused_watch_list(r: int) -> bool:  # the recount does not use the reconstructed list (rules rev. 4)
        return False

    accounts = [x for x in st["accounts"] if x in st["polls"]][:SMOKE_ACCOUNTS] if a.smoke else st["accounts"]
    episodes, counters = new_tallies()
    rel: list[float] = []
    reopen = 0
    for i, acc in enumerate(accounts):  # one account at a time: the whole log does not stay in memory
        groups = account_groups(a.db, acc, end)
        for ts, rows in groups:
            if ts >= start:
                rel += _mark_errors(rows, marks, ts)
        add_account(
            episodes, counters, acc, groups, runs, delta_start=delta_start, watched=unused_watch_list, top_at=top_at,
            mark=mark, polls=st["polls"].get(acc, {}), polls_from=start,
        )  # fmt: skip
        reopen += close_reopen_pairs(groups, runs, delta_start, start, end)
        if i % 100 == 0:
            print(f"  accounts {i + 1}/{len(accounts)} {time.time() - t0:.0f}s rss={rss_mb():.0f}MB", flush=True)
    if a.smoke:  # the pipeline ran end to end on a few accounts and coins; every count is withheld by design
        step0_decision([], HOUR)
        recount_outcome({}, recount_completeness({}, 0.0, 1.0))
        print(f"recount pipeline ran on {len(accounts)} accounts x {len(VARIANTS)} variants x {len(REFRACTORY)} refractories")
        print(f"smoke done in {time.time() - t0:.0f}s rss={rss_mb():.0f}MB (no completeness, no episode metric by design)")
        return 0

    # --- data checks and descriptive lines (they decide nothing) ---------------------------------------------------
    gaps = run_gaps(runs, GAP_S)
    exposure = exposure_seconds(runs, end, GAP_S)
    calendar = float(end - start)
    per_run = Counter(t for p in st["polls"].values() for t in p)
    sizes = sorted(per_run.get(r, 0) for r in runs)
    ages = [x for x in snapshot_age_days(st["snap_days"], runs) if x is not None]
    print(
        f"runs={len(runs)} gaps (> {GAP_S // 60} min)={len(gaps)} exposure_h={exposure / HOUR:.1f} of {calendar / HOUR:.0f}; "
        f"intended list per run (markers): min={sizes[0]} median={statistics.median(sizes)}; "
        f"newest trader_snapshot day age at a run: max={max(ages) if ages else None} d, runs >= 2 d: "
        f"{sum(1 for x in ages if x >= 2)}; closed and reopened (same side, size) at the next run: {reopen}"
    )
    _print_mark_check(rel)
    print_counters(counters, "marker not ok, no rows")

    # --- registered completeness first, then the gates, then the outcome --------------------------------------------
    comp = recount_completeness(counters, exposure, calendar)
    print(
        f"COMPLETENESS: polled={comp['polled']:.2%} (>= 95%)  known={comp['known']:.2%} (>= 95%)  "
        f"exposure={comp['exposure']:.2%} (>= 90%)  rows without a marker={comp['rows_unmarked']} (= 0)  "
        f"-> {'complete' if comp['complete'] else 'INCOMPLETE ' + str(comp['failed'])}",
        flush=True,
    )
    result: dict[str, Any] = {
        "recount": True,
        "started_at": int(t0),
        "period": [start, end],
        "polls_from": polls_from,
        "late": chk["late"],
        "allow_late": bool(a.allow_late),
        "delta_start": delta_start,
        "short": uni["short"],
        "failed15": uni["failed15"],
        "first15_window": list(w0),
        "first15_cached": first15_cached,
        "union": len(uni["union"]),
        "runs": len(runs),
        "exposure_s": exposure,
        "gaps": gaps,
        "counters": dict(counters),
        "completeness": comp,
        "descriptive": {
            "intended_list_min": sizes[0],
            "intended_list_median": statistics.median(sizes),
            "snapshot_age_max_d": max(ages) if ages else None,
            "close_reopen_pairs": reopen,
        },
        "variants": {},
    }
    dec: dict[str, Any] = {}
    if comp["complete"]:
        dec = report(episodes, result, exposure, start, end, lambda d: f"gates (episodes and clusters): {'PASS' if d['passed'] else 'NO'}")
    else:  # stricter reading (flagged in the card): no verdict, so no rate is shown for an incomplete period
        print("data incomplete: no verdict; the episode table and gates are withheld (a missing measurement is not a low rate)")
    outcome = recount_outcome(dec, comp)
    result["outcome"] = outcome
    print(f"RECOUNT OUTCOME: {outcome.upper()}", flush=True)
    if a.out:
        write_json(a.out.expanduser(), result)
        print(f"wrote {a.out}")
    print(f"done in {time.time() - t0:.0f}s rss={rss_mb():.0f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
