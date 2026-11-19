"""DefiLlama yield pools (yields.llama.fi) normalised, filtered and summarised (pure: no I/O, time passed in).

DefiLlama quotes every APY field in PERCENT (5.2 = 5.2 %); here all rates become decimals (0.052), as everywhere in the
project. `apy` = `apyBase` + `apyReward`: apyBase is the pool's own yield (trading fees, lending interest), apyReward the
incentives paid in reward tokens, valued at the token's current price. `apyPct1D/7D/30D` are changes of `apy` in
percentage points, here decimal points (0.01 = +1 pp). `count` is how many daily points DefiLlama holds for the pool,
used here as a rough age in days (`history_days`; small = a new pool). Names, symbols and `poolMeta` are untrusted
third-party text: control and invisible characters are stripped and the length is capped.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .chain_metrics import resolve_chain

DAY = 86400
MAX_GAP = 2 * DAY  # the reference point of a TVL change may be at most this much older than the target, else None
STALE_AFTER = 2 * DAY  # latest history point older than this (vs `now`) is reported as stale
MAX_TOKENS = 10  # reward / underlying token addresses kept per pool

POOL_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# C0/C1 controls, zero-width, bidi embeddings / overrides, invisible operators, BOM (escaped so a diff shows them)
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]")
SORT_KEYS = ("tvl_usd", "apy", "newest")


def valid_pool_id(pool: Any) -> str | None:
    """A DefiLlama pool id (a UUID, e.g. from yield_pools) lower-cased, else None. It becomes a URL path segment."""
    if not isinstance(pool, str):
        return None
    p = pool.strip()
    return p.lower() if POOL_ID_RE.match(p) else None


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except OverflowError:  # an integer beyond the float range
        return None
    return v if math.isfinite(v) else None


def _pct(x: Any) -> float | None:
    """DefiLlama percent -> decimal (5.2 -> 0.052)."""
    v = _num(x)
    return None if v is None else round(v / 100.0, 8)


def _text(x: Any, limit: int = 80) -> str | None:
    if not isinstance(x, str):
        return None
    return _CTRL.sub("", x).strip()[:limit] or None


def _tokens(x: Any) -> list[str]:
    if not isinstance(x, list):
        return []
    return [t for t in (_text(v, 100) for v in x[:MAX_TOKENS]) if t]


def pool_row(p: Any) -> dict[str, Any] | None:
    """One `/pools` row in our units, or None when it lacks a pool id, chain, project or a finite TVL >= 0."""
    if not isinstance(p, dict):
        return None
    pool, chain, project = _text(p.get("pool"), 100), _text(p.get("chain")), _text(p.get("project"))
    tvl = _num(p.get("tvlUsd"))
    if pool is None or chain is None or project is None or tvl is None or tvl < 0:
        return None
    count = _num(p.get("count"))
    stable, outlier = p.get("stablecoin"), p.get("outlier")
    return {
        "pool": pool,
        "chain": chain,
        "project": project,
        "symbol": _text(p.get("symbol")),
        "pool_meta": _text(p.get("poolMeta")),
        "tvl_usd": round(tvl, 2),
        "apy": _pct(p.get("apy")),
        "apy_base": _pct(p.get("apyBase")),
        "apy_reward": _pct(p.get("apyReward")),
        "apy_mean_30d": _pct(p.get("apyMean30d")),
        "apy_change_1d": _pct(p.get("apyPct1D")),
        "apy_change_7d": _pct(p.get("apyPct7D")),
        "apy_change_30d": _pct(p.get("apyPct30D")),
        "stablecoin": stable if isinstance(stable, bool) else None,
        "il_risk": _text(p.get("ilRisk"), 10),
        "exposure": _text(p.get("exposure"), 10),
        "il_7d": _pct(p.get("il7d")),
        "volume_usd_1d": _num(p.get("volumeUsd1d")),
        "volume_usd_7d": _num(p.get("volumeUsd7d")),
        "reward_tokens": _tokens(p.get("rewardTokens")),
        "underlying_tokens": _tokens(p.get("underlyingTokens")),
        "outlier": outlier if isinstance(outlier, bool) else None,
        "history_days": int(count) if count is not None and count >= 0 else None,
    }


def _sort_key(sort_by: str):
    if sort_by == "apy":
        return lambda r: (r["apy"] is None, -(r["apy"] or 0.0), -r["tvl_usd"])
    if sort_by == "newest":
        return lambda r: (r["history_days"] is None, r["history_days"] or 0, -r["tvl_usd"])
    return lambda r: -r["tvl_usd"]


def normalise_pools(rows: list[Any]) -> dict[str, Any]:
    """Every `/pools` row through `pool_row` once: `{"pools": [rows in our units], "skipped": malformed count}`.

    Done when the list is fetched (the provider caches the result), so `filter_pools` does not re-parse ~15k rows
    on every call."""
    parsed = [pool_row(r) for r in rows]
    good = [r for r in parsed if r is not None]
    return {"pools": good, "skipped": len(parsed) - len(good)}


def filter_pools(
    rows: list[dict[str, Any]],
    *,
    skipped: int = 0,
    chain: str | None = None,
    project: str | None = None,
    min_tvl_usd: float = 0.0,
    stablecoin_only: bool = False,
    sort_by: str = "tvl_usd",
    limit: int = 20,
) -> dict[str, Any]:
    """Pools matching every filter, sorted and cut to `limit`.

    `rows` are `normalise_pools(...)["pools"]` (already in our units, shared with the cache: not mutated; the
    returned pools are shallow copies) and `skipped` its malformed count, reported as `pools_skipped`.
    `chain` takes DefiLlama names or the aliases of `chain_metrics.resolve_chain` (eth, sol, bnb, arb, ...) and
    `project` a DefiLlama project slug (aave-v3, lido, uniswap-v3); both match case-insensitively and exactly.
    `stablecoin_only` keeps pools DefiLlama flags `stablecoin: true`. Sort: tvl_usd and apy highest first, newest by
    the fewest days of history (pools without a count last).
    """
    want_chain = resolve_chain(chain).lower() if chain else None
    want_project = project.strip().lower() if project else None
    matched = [
        r
        for r in rows
        if (want_chain is None or r["chain"].lower() == want_chain)
        and (want_project is None or r["project"].lower() == want_project)
        and r["tvl_usd"] >= min_tvl_usd
        and (not stablecoin_only or r["stablecoin"] is True)
    ]
    key = sort_by if sort_by in SORT_KEYS else "tvl_usd"
    matched.sort(key=_sort_key(key))
    out = [dict(r) for r in matched[:limit]]
    return {
        "pools_total": len(rows),
        "pools_skipped": skipped,
        "pools_matched": len(matched),
        "pools_returned": len(out),
        "filters": {
            "chain": resolve_chain(chain) if chain else None,
            "project": want_project,
            "min_tvl_usd": min_tvl_usd,
            "stablecoin_only": stablecoin_only,
        },
        "sort_by": key,
        "outliers_returned": sum(1 for r in out if r["outlier"] is True),
        "pools": out,
    }


def _ts(x: Any) -> int | None:
    """ISO time of a `/chart/{pool}` point ("2026-11-18T23:01:05.123Z") -> unix seconds, else None.

    None also when the instant has no UTC date: "0001-01-01T00:00:00+14:00" parses but falls before year 1 in UTC,
    and `_day` of it would raise (invariant 1: one bad point is skipped, not fatal)."""
    if not isinstance(x, str):
        return None
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
        ts = int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())
        _day(ts)
    except (ValueError, OverflowError, OSError):
        return None
    return ts


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 8) if xs else None


def pool_history(rows: list[Any], now_ts: float, days: int = 90) -> dict[str, Any] | None:
    """`/chart/{pool}` points `{timestamp, tvlUsd, apy, apyBase, apyReward, il7d, apyBase7d}` -> daily series of the
    last `days` days (decimals, one point per UTC day: the last of the day) and a summary of the whole history.

    tvl_change_7d / 30d compare the latest TVL with the point 7 / 30 days earlier (None without a point within
    MAX_GAP before the target); apy_mean_7d / 30d average the points of the last 7 / 30 days up to the latest one.
    None when no point has a time and a TVL or APY.
    """
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = _ts(r.get("timestamp"))
        tvl, apy = _num(r.get("tvlUsd")), _pct(r.get("apy"))
        if ts is None or (tvl is None and apy is None) or (tvl is not None and tvl < 0):
            continue
        pt = {
            "ts": ts,
            "date": _day(ts),
            "tvl_usd": None if tvl is None else round(tvl, 2),
            "apy": apy,
            "apy_base": _pct(r.get("apyBase")),
            "apy_reward": _pct(r.get("apyReward")),
            "il_7d": _pct(r.get("il7d")),
        }
        prev = by_day.get(pt["date"])
        if prev is None or ts >= prev["ts"]:
            by_day[pt["date"]] = pt
    pts = sorted(by_day.values(), key=lambda p: p["ts"])
    if not pts:
        return None
    last = pts[-1]
    t_last = last["ts"]

    def tvl_at_or_before(t: int) -> float | None:
        best = None
        for p in pts:
            if p["ts"] > t:
                break
            if p["tvl_usd"] is not None:
                best = p
        if best is None or t - best["ts"] > MAX_GAP:
            return None
        return best["tvl_usd"]

    def change(ref: float | None) -> float | None:
        cur = last["tvl_usd"]
        if cur is None or ref is None or ref <= 0:
            return None
        return round(cur / ref - 1.0, 6)

    def apy_mean(n_days: int) -> float | None:
        return _mean([p["apy"] for p in pts if p["apy"] is not None and p["ts"] > t_last - n_days * DAY])

    window = [p for p in pts if p["ts"] >= now_ts - days * DAY]
    apys = [p["apy"] for p in window if p["apy"] is not None]
    age = max(now_ts - t_last, 0.0)
    return {
        "first_date": pts[0]["date"],
        "points_total": len(pts),
        "latest": {k: v for k, v in last.items() if k != "ts"},
        "age_days": round(age / DAY, 2),
        "stale": age > STALE_AFTER,
        "tvl_change_7d": change(tvl_at_or_before(t_last - 7 * DAY)),
        "tvl_change_30d": change(tvl_at_or_before(t_last - 30 * DAY)),
        "apy_mean_7d": apy_mean(7),
        "apy_mean_30d": apy_mean(30),
        "window_days": days,
        "apy_min": min(apys) if apys else None,
        "apy_max": max(apys) if apys else None,
        "series": [{k: v for k, v in p.items() if k not in ("ts", "il_7d")} for p in window],
    }
