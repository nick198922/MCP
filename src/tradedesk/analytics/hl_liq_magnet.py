"""Hyperliquid whale-liquidation magnet, step 0: a blind COUNT of episodes (pure: no I/O, time passed in).

Card: docs/ideas/IDEA-20261025-hl-whale-liquidation-magnet.md. An episode is one (account, coin) entering the band
"position worth >= $3M on a top-50-turnover Hyperliquid perp, distance to its public liquidation price d in
[1 %; 4 %] of mark". Step 0 only counts how often that happens; it never looks at what the price did afterwards, so
nothing here reads a price later than the snapshot it scores.

The log keeps liq_px only from the last size change, and a cross-margin liq_px drifts by 10-25 % within days, so the
registered (primary) count trusts only a liq_px written at the very run it scores; every other snapshot of a
qualifying-size position is unknown, and episodes are counted with ``detect_episodes(..., lower_bound=True)``, a lower
bound of what an hourly observer with fresh liq_px would count. Counts on liq_px of any age are descriptive only.

Inputs come from the collector's `trader_positions` change log (rows only when a position's side or size, to 4
significant digits, changes; see ``Store.record_trader_positions``), the daily `trader_snapshot` (the watch list) and
Hyperliquid candles. Units: prices and values in USD, distances as decimals (0.03 = 3 %), times in unix seconds.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HOUR = 3600
DAY = 86400
WEEK = 7 * DAY


# ---------------------------------------------------------------------------------------------------------------------
# collector runs and the watch list


def run_gaps(run_ts: Sequence[int], max_step_s: int = 5400) -> list[tuple[int, int]]:
    """(previous run, next run) pairs further apart than ``max_step_s``: hours the collector did not run."""
    runs = sorted(set(int(t) for t in run_ts))
    return [(a, b) for a, b in zip(runs, runs[1:], strict=False) if b - a > max_step_s]


def exposure_seconds(run_ts: Sequence[int], end: int, max_step_s: int = 5400, step_s: int = HOUR) -> float:
    """Observed time from the first run to ``end``: calendar time minus the unobserved part of every gap (a step longer
    than ``max_step_s`` between runs, or from the last run to ``end``, counts only ``step_s``). Independent of the
    number of runs, so repeated runs minutes apart (collector restarts) add nothing, and an irregular cadence
    (e.g. 55 min, 45 min) does not lose time either."""
    runs = sorted(set(int(t) for t in run_ts if t < end))
    if not runs:
        return 0.0
    total = float(end - runs[0])
    for a, b in zip(runs, [*runs[1:], end], strict=True):
        if b - a > max_step_s:
            total -= (b - a) - step_s
    return total


def history_covers(first_open_s: int | None, listing_day_s: int | None, start: int, interval_s: int = 900) -> bool:
    """Whether a coin's candle history (first candle open ``first_open_s``) reaches the period ``start``: the first
    candle must be complete by ``start``, or the coin was listed later (first 1d candle ``listing_day_s``) and the
    history starts within that listing day. Hyperliquid serves only the last 5000 candles (~52 days of 15m), so an
    old period silently loses its early marks without this check."""
    if first_open_s is None:
        return False
    need = start - interval_s
    if listing_day_s is not None:
        need = max(need, listing_day_s + DAY - interval_s)
    return first_open_s <= need


def watchlists_by_day(
    rows: Iterable[Mapping[str, Any]], top_n: int = 100, min_account_usd: float = 500_000.0
) -> list[tuple[int, str, frozenset[str]]]:
    """The collector's watch list per snapshot day, as ``Store.tracked_traders`` builds it from that day's rows:
    the ``top_n`` accounts by ``pnl_month`` plus the ``top_n`` by ``pnl_all`` (each among rows with that PnL set and
    ``account_value`` >= ``min_account_usd``, a missing value passing). Returns ``(available_ts, day, accounts)``
    sorted by ``available_ts``, the earliest ``ts`` stored for the day (rows rewritten later in the day carry a later
    ts, so availability can only be estimated late, never early)."""
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(str(r["day"]), []).append(r)
    out: list[tuple[int, str, frozenset[str]]] = []
    for day, rs in by_day.items():
        ok = [r for r in rs if (r.get("account_value") if r.get("account_value") is not None else min_account_usd) >= min_account_usd]
        acc: set[str] = set()
        for col in ("pnl_month", "pnl_all"):
            ranked = sorted((r for r in ok if r.get(col) is not None), key=lambda r, c=col: -float(r[c]))
            acc |= {str(r["account"]) for r in ranked[:top_n]}
        out.append((min(int(r["ts"]) for r in rs), day, frozenset(acc)))
    return sorted(out)


def watchlist_at(lists: Sequence[tuple[int, str, frozenset[str]]], ts: int) -> frozenset[str] | None:
    """The watch list in force at ``ts``: that of the latest day available at or before ``ts`` (None before any)."""
    i = bisect_right([a for a, _, _ in lists], ts) - 1
    return lists[i][2] if i >= 0 else None


# ---------------------------------------------------------------------------------------------------------------------
# turnover rank and mark, point in time


def daily_turnover(candles: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """Hyperliquid 1d candles -> {UTC day start (s): turnover USD}, turnover = base volume ``v`` x close ``c`` (an
    approximation of the notional traded; HL candles carry no quote volume)."""
    out: dict[int, float] = {}
    for c in candles:
        try:
            out[int(c["t"]) // 1000 // DAY * DAY] = float(c["v"]) * float(c["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def top_coins_at(turnover: Mapping[str, Mapping[int, float]], ts: int, n: int = 50, window_days: int = 30) -> frozenset[str]:
    """The ``n`` coins with the largest turnover over the last ``window_days`` UTC days completed before ``ts``
    (days whose end is at or before ``ts``; the running day is never used). Ties broken by coin name."""
    last_day = ts // DAY * DAY - DAY  # start of the latest completed UTC day
    first_day = last_day - (window_days - 1) * DAY
    tot = []
    for coin, days in turnover.items():
        s = sum(v for d, v in days.items() if first_day <= d <= last_day)
        if s > 0:
            tot.append((-s, coin))
    return frozenset(c for _, c in sorted(tot)[:n])


def mark_at(open_s: Sequence[int], closes: Sequence[float], ts: int, interval_s: int = 900, max_age_s: int = 2 * HOUR) -> float | None:
    """Close of the latest candle completed at or before ``ts`` (open + interval <= ts), from candles sorted by open
    time; None if there is none or it ended more than ``max_age_s`` before ``ts``. Never reads a later candle."""
    i = bisect_right(open_s, ts - interval_s) - 1
    if i < 0 or ts - (open_s[i] + interval_s) > max_age_s:
        return None
    return float(closes[i])


def signed_distance(side: str, liq_px: float | None, mark: float | None) -> float | None:
    """Distance from mark to the liquidation price as a decimal, positive while the position is alive: a long
    1 - liq/mark (liq below), a short liq/mark - 1 (liq above). |d| = |liq/mark - 1|. None without inputs; <= 0 means
    the recorded liq_px is already crossed (a stale or wrong record: the position would have been liquidated)."""
    if liq_px is None or mark is None or mark <= 0 or liq_px <= 0:
        return None
    if side == "long":
        return 1.0 - liq_px / mark
    if side == "short":
        return liq_px / mark - 1.0
    return None


# ---------------------------------------------------------------------------------------------------------------------
# replaying one account's change log


def replay_account(
    groups: Sequence[tuple[int, Sequence[Mapping[str, Any]]]], delta_start: int | None
) -> Iterator[tuple[int, dict[str, dict[str, Any]], dict[str, int]]]:
    """One account's `trader_positions` groups (ts, rows), sorted by ts -> (ts, {market: row}, {market: ts of the row
    that set it}) after each group. Same rules as ``smart_money.replay_positions``: before ``delta_start`` (schema <
    12) and at the account's first group from it on, a group is the full set ('flat' = none); later groups are
    changes (a row replaces its market, 'closed' removes it)."""
    state: dict[str, dict[str, Any]] = {}
    since: dict[str, int] = {}
    baselined = False
    for ts, rows in groups:
        full = delta_start is None or ts < delta_start or not baselined
        if delta_start is not None and ts >= delta_start:
            baselined = True
        if full:
            state, since = {}, {}
        else:
            state, since = dict(state), dict(since)
        for r in rows:
            side, market = str(r.get("side") or ""), str(r.get("market") or "")
            if side == "flat" or not market:
                continue
            if side == "closed":
                state.pop(market, None)
                since.pop(market, None)
            else:
                state[market] = dict(r)
                since[market] = ts
        yield ts, state, since


# ---------------------------------------------------------------------------------------------------------------------
# episodes


@dataclass(frozen=True)
class Obs:
    """One (account, coin) at one collector run where the account was watched. ``qualifies``: True in the band, False
    out of it (including no position), None unknown (no mark, a liq_px older than the variant allows, or the account
    was not polled at that run). ``first_obs``: the account's first watched run with a baseline; ``data_start``: that
    run is the first run of the data; ``after_gap``: the account was not observed at the previous run (collector gap,
    or off the watch list)."""

    ts: int
    side: str | None
    qualifies: bool | None
    first_obs: bool = False
    data_start: bool = False
    after_gap: bool = False


@dataclass(frozen=True)
class Episode:
    ts: int
    side: str
    kind: str  # 'fresh' | 'start' (in band at the first run of the data) | 'join' (at the account's first later
    #            observation) | 'gap' (first observation after the account was unobserved)
    prev_known_ts: int | None = None  # the last KNOWN observation before the start (lower-bound mode; None: none)


@dataclass(frozen=True)
class Variant:
    """A band [lo; hi] of the signed distance d and the oldest liq_px it trusts: ``max_liq_age_s`` = 0 means only a
    liq_px written at this very run (fresh), None any age (the log keeps liq_px only from the last size change)."""

    lo: float
    hi: float
    max_liq_age_s: int | None = 0


def account_observations(  # noqa: C901, PLR0912, PLR0913 - one linear replay, all inputs explicit
    groups: Sequence[tuple[int, Sequence[Mapping[str, Any]]]],
    runs: Sequence[int],
    *,
    delta_start: int | None,
    watched: Callable[[int], bool],
    top_at: Callable[[int], frozenset[str]],
    mark: Callable[[str, int], float | None],
    variants: Mapping[str, Variant],
    min_value: float,
    gap_s: int = 5400,
) -> tuple[dict[str, dict[str, list[Obs]]], dict[tuple[str, int], dict[str, Any]], Counter[str]]:
    """One account's observations per variant and coin, at every collector run where it was observed.

    ``groups``: the account's `trader_positions` groups (ts, rows) sorted by ts; ``runs``: all collector runs sorted;
    ``watched(r)``: the account is on the reconstructed watch list at r; ``top_at(r)``: the top coins in force at r;
    ``mark(coin, r)``: the mark at r without look-ahead (None unknown). The account is observed at r when it has rows
    at r or is watched, and r is not before its first group. At an observed run, for each coin it ever held:

    - not polled (before ``delta_start``, i.e. full groups, and no group at r): unknown (None) in every variant;
    - no position: False (closes are logged); position off the top coins: False; no mark: None;
    - value |size| x mark below ``min_value``: False (size is current: every size change is logged);
    - otherwise d = signed distance to the liq_px of the row that set the position; its age is r minus that row's ts.
      If the age exceeds the variant's ``max_liq_age_s``: None (unknown, the stored liq_px may be far from the real
      one). Else True iff 0 < d and lo <= d <= hi; a missing liq_px (Hyperliquid gives none when the position cannot
      be liquidated) or d <= 0 (crossed) is False.

    Returns ({variant: {coin: [Obs]}}, {(coin, r): {d, liq_age_h, n_pos}} for runs where some variant qualifies,
    counters). Uses only rows at or before each r and marks from ``mark(coin, r)``."""
    obs: dict[str, dict[str, list[Obs]]] = {v: {} for v in variants}
    meta: dict[tuple[str, int], dict[str, Any]] = {}
    cnt: Counter[str] = Counter()
    if not groups:
        return obs, meta, cnt
    rows_at = {int(ts) for ts, _ in groups}
    first_ts = int(groups[0][0])
    held = sorted({str(r.get("market")) for _, rs in groups for r in rs if r.get("market")})
    data_start = runs[0] if runs else None
    states = iter(replay_account(groups, delta_start))
    nxt = next(states, None)
    state: dict[str, dict[str, Any]] = {}
    since: dict[str, int] = {}
    prev_run: int | None = None
    prev_obs_run: int | None = None
    first_seen = True
    for r in runs:
        r = int(r)
        if r < first_ts:
            prev_run = r
            continue
        while nxt is not None and nxt[0] <= r:
            _, state, since = nxt
            nxt = next(states, None)
        if not (r in rows_at or watched(r)):
            prev_run = r
            continue
        after_gap = (not first_seen) and (prev_obs_run is None or prev_obs_run != prev_run or r - (prev_run or r) > gap_s)
        polled = r in rows_at or not (delta_start is None or r < delta_start)
        if not polled:
            cnt["unpolled_runs"] += 1
        top = top_at(r)
        for c in held:
            p = state.get(c) if polled else None
            side = str(p["side"]) if p else None
            q: dict[str, bool | None]
            d: float | None = None
            if not polled:
                q = dict.fromkeys(variants, None)
            elif p is None or c not in top:
                q = dict.fromkeys(variants, False)
            else:
                m = mark(c, r)
                if m is None:
                    q = dict.fromkeys(variants, None)
                    cnt["no_mark"] += 1
                elif abs(float(p.get("size") or 0)) * m < min_value:
                    q = dict.fromkeys(variants, False)
                else:
                    age = r - since.get(c, r)
                    cnt["in_scope"] += 1
                    cnt["in_scope_fresh"] += age == 0
                    d = signed_distance(side or "", p.get("liq_px"), m)
                    if d is not None and d <= 0:
                        cnt["crossed"] += 1
                        cnt["crossed_fresh"] += age == 0
                    q = {}
                    for v, var in variants.items():
                        if var.max_liq_age_s is not None and age > var.max_liq_age_s:
                            q[v] = None
                        else:
                            q[v] = d is not None and d > 0 and var.lo <= d <= var.hi
            for v in variants:
                obs[v].setdefault(c, []).append(Obs(r, side, q[v], first_seen, first_seen and r == data_start, after_gap))
            if p and any(q.values()):
                meta[(c, r)] = {"d": d, "liq_age_h": (r - since.get(c, r)) / HOUR, "n_pos": len(state)}
        prev_obs_run = r
        prev_run = r
        first_seen = False
    return obs, meta, cnt


def detect_episodes(obs: Sequence[Obs], refractory_s: int = DAY, lower_bound: bool = False) -> list[Episode]:  # noqa: C901
    """Entries of one (account, coin) into the band, from its observations sorted by ts.

    An entry is an observation that qualifies when the last KNOWN state (unknown observations skipped) did not
    qualify, or had the other side, or there is none. Default mode (the registered rule): an entry within
    ``refractory_s`` of the previous episode's start on the same side is merged into it (repeated snapshots and quick
    in-out flicker are one event).

    ``lower_bound=True`` (for observations where unknown snapshots are many, e.g. a stale liq_px): the real entry lies
    somewhere in (last known observation p, this observation s], so an entry opens a new episode only if p is at
    least ``refractory_s`` after the previous counted start, whatever the side; and when the account was observed from
    the first run of the data but that observation is unknown, the data start acts as a possible left-censored episode
    (later entries within ``refractory_s`` of it are merged into it, an entry with no known state before it is
    'start'). Then every counted start maps to a distinct real entry, the mapped entries are ``refractory_s`` apart,
    so an observer who knew every one of these snapshots would count at least as many non-'start' episodes under the
    default rule. Uses only observations at or before each start, so starts before t do not change when data after t
    changes."""
    out: list[Episode] = []
    prev_q: bool | None = None
    prev_side: str | None = None
    prev_ts: int | None = None
    gap_since_known = False
    last: Episode | None = None
    if lower_bound and obs and obs[0].data_start and obs[0].qualifies is None:
        last = Episode(obs[0].ts, "", "start")  # a possible censored episode at the data start; never reported
    for o in obs:
        gap_since_known = gap_since_known or o.after_gap
        if o.qualifies is None:
            continue
        if o.qualifies and o.side and not (prev_q and prev_side == o.side):
            if lower_bound:
                new = last is None or (prev_ts is not None and prev_ts >= last.ts + refractory_s)
            else:
                new = last is None or last.side != o.side or o.ts - last.ts >= refractory_s
            if new:
                if lower_bound and prev_ts is None:
                    kind = "start" if obs[0].data_start else "join"
                elif lower_bound:
                    kind = "gap" if gap_since_known else "fresh"
                elif o.first_obs:
                    kind = "start" if o.data_start else "join"
                elif o.after_gap:
                    kind = "gap"
                else:
                    kind = "fresh"
                last = Episode(o.ts, o.side, kind, prev_ts if lower_bound else None)
                out.append(last)
        prev_q, prev_side, prev_ts = o.qualifies, o.side, o.ts
        gap_since_known = False
    return out


def summarize(episodes: Sequence[Mapping[str, Any]], exposure_s: float, period: tuple[int, int], block_days: int = 10) -> dict[str, Any]:
    """Counts of episodes (dicts with ts, account, coin, side): n, rate per week of exposure, coin-day clusters,
    long / short, the most frequent coin and account with their shares, and counts per ``block_days`` block of the
    calendar ``period`` (start, end)."""
    n = len(episodes)
    weeks = exposure_s / WEEK if exposure_s > 0 else 0.0
    clusters = {(e["coin"], int(e["ts"]) // DAY) for e in episodes}
    coins = Counter(str(e["coin"]) for e in episodes)
    accounts = Counter(str(e["account"]) for e in episodes)
    sides = Counter(str(e["side"]) for e in episodes)
    start, end = period
    nblocks = max(1, -(-(end - start) // (block_days * DAY)))
    blocks = [0] * nblocks
    for e in episodes:
        blocks[min(nblocks - 1, max(0, (int(e["ts"]) - start) // (block_days * DAY)))] += 1
    top_coin = coins.most_common(1)[0] if coins else (None, 0)
    top_acc = accounts.most_common(1)[0] if accounts else (None, 0)
    return {
        "n": n,
        "per_week": n / weeks if weeks else None,
        "coin_days": len(clusters),
        "coin_days_per_week": len(clusters) / weeks if weeks else None,
        "long": sides.get("long", 0),
        "short": sides.get("short", 0),
        "n_coins": len(coins),
        "top_coin": top_coin[0],
        "top_coin_share": top_coin[1] / n if n else None,
        "n_accounts": len(accounts),
        "top_account_share": top_acc[1] / n if n else None,
        "coins": dict(coins.most_common()),
        "blocks": blocks,
    }


def gate(n: int, exposure_s: float, min_per_week: float = 1.0, min_per_10d: float = 5.0, need: int = 90) -> dict[str, Any]:
    """The card's step-0 gate: pass only if the rate is at least ``min_per_week`` AND at least ``min_per_10d`` per
    10 days of exposure (the card kills the forward when either fails). Also the weeks a forward would need to
    collect ``need`` events at that rate (None at rate 0)."""
    weeks = exposure_s / WEEK
    per_week = n / weeks if weeks > 0 else 0.0
    per_10d = per_week * 10 / 7
    return {
        "per_week": per_week,
        "per_10d": per_10d,
        "passed": per_week >= min_per_week and per_10d >= min_per_10d,
        "weeks_to_need": need / per_week if per_week > 0 else None,
    }


def coin_day_clusters(episodes: Sequence[Mapping[str, Any]]) -> int:
    """Distinct (coin, UTC day of the start) among episodes: the unit of the cluster gate."""
    return len({(str(e["coin"]), int(e["ts"]) // DAY) for e in episodes})


def step0_decision(episodes: Sequence[Mapping[str, Any]], exposure_s: float, need: int = 90) -> dict[str, Any]:
    """The registered step-0 decision on the primary (fresh liq_px, lower-bound) episodes: the same ``gate`` must pass
    for the episodes AND for their coin-day clusters. Outcome 'pass', else 'unresolved' (history cannot reject: the
    log keeps liq_px only from size changes, so the count is a lower bound). Weeks to ``need`` events are taken from
    the clusters (an upper estimate of the forward's time, since the rate is a lower bound). Descriptive extras: day
    clusters over all coins and the gate without the account with most episodes."""
    ep = gate(len(episodes), exposure_s, need=need)
    cl = gate(coin_day_clusters(episodes), exposure_s, need=need)
    accounts = Counter(str(e["account"]) for e in episodes)
    top_acc, top_n = accounts.most_common(1)[0] if accounts else (None, 0)
    rest = [e for e in episodes if str(e["account"]) != top_acc]
    return {
        "episodes": ep,
        "coin_days": {**cl, "n": coin_day_clusters(episodes)},
        "passed": bool(ep["passed"] and cl["passed"]),
        "outcome": "pass" if ep["passed"] and cl["passed"] else "unresolved",
        "weeks_to_need_clusters": cl["weeks_to_need"],
        "descriptive": {
            "days": len({int(e["ts"]) // DAY for e in episodes}),
            "top_account_n": top_n,
            "without_top_account": {
                "n": len(rest),
                "coin_days": coin_day_clusters(rest),
                "episodes": gate(len(rest), exposure_s, need=need),
                "clusters": gate(coin_day_clusters(rest), exposure_s, need=need),
            },
        },
    }
