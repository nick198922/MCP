"""Pure step-0 logic of the hl-whale-liquidation-magnet card: analytic references, no network."""

from __future__ import annotations

import math
import random

from tradedesk.analytics.hl_liq_magnet import (
    DAY,
    HOUR,
    WEEK,
    Obs,
    Variant,
    account_observations,
    coin_day_clusters,
    coin_day_clusters_strict,
    daily_turnover,
    detect_episodes,
    exposure_seconds,
    gate,
    history_covers,
    listed_after,
    mark_at,
    recount_completeness,
    recount_outcome,
    replay_account,
    run_gaps,
    signed_distance,
    step0_decision,
    summarize,
    top_coins_at,
    watchlist_at,
    watchlists_by_day,
)
from tradedesk.analytics.smart_money import replay_positions


def test_run_gaps():
    assert run_gaps([0, 3600, 7200, 7200 + 5 * HOUR, 7200 + 6 * HOUR]) == [(7200, 7200 + 5 * HOUR)]
    assert run_gaps([10]) == []


def test_watchlist_mirrors_tracked_traders_and_is_point_in_time():
    rows = [
        {"day": "d1", "ts": 100, "account": "a", "account_value": 1e6, "pnl_month": 5.0, "pnl_all": 1.0},
        {"day": "d1", "ts": 101, "account": "b", "account_value": 1e5, "pnl_month": 9.0, "pnl_all": 9.0},  # too small
        {"day": "d1", "ts": 102, "account": "c", "account_value": None, "pnl_month": 1.0, "pnl_all": 7.0},
        {"day": "d2", "ts": 500, "account": "d", "account_value": 2e6, "pnl_month": 3.0, "pnl_all": None},
    ]
    lists = watchlists_by_day(rows, top_n=1, min_account_usd=5e5)
    assert [(t, d) for t, d, _ in lists] == [(100, "d1"), (500, "d2")]
    assert lists[0][2] == frozenset({"a", "c"})  # top-1 by month (a) + top-1 by all-time (c)
    assert watchlist_at(lists, 99) is None
    assert watchlist_at(lists, 499) == frozenset({"a", "c"})
    assert watchlist_at(lists, 500) == frozenset({"d"})


def test_daily_turnover_and_top_coins_use_only_completed_days():
    t = 10 * DAY
    candles = [{"t": (t - DAY) * 1000, "v": "2", "c": "50"}, {"t": t * 1000, "v": "1000", "c": "1"}]
    assert daily_turnover(candles) == {t - DAY: 100.0, t: 1000.0}
    turnover = {"A": {t - DAY: 100.0, t: 1e9}, "B": {t - DAY: 200.0}, "C": {t - 40 * DAY: 1e9}}
    # at noon of day t, day t is still running: only day t-1 counts, C is outside the 30-day window
    assert top_coins_at(turnover, t + DAY // 2, n=1) == frozenset({"B"})
    assert top_coins_at(turnover, t + DAY, n=1) == frozenset({"A"})  # day t completed
    assert top_coins_at(turnover, t + DAY // 2, n=5) == frozenset({"A", "B"})


def test_mark_at_takes_the_last_completed_candle_only():
    opens = [0, 900, 1800]
    closes = [1.0, 2.0, 3.0]
    assert mark_at(opens, closes, 899) is None
    assert mark_at(opens, closes, 900) == 1.0
    assert mark_at(opens, closes, 2699) == 2.0  # the candle opened at 1800 is not complete yet
    assert mark_at(opens, closes, 2700) == 3.0
    assert mark_at(opens, closes, 2700 + 3 * HOUR) is None  # too stale


def test_signed_distance():
    assert math.isclose(signed_distance("long", 97.0, 100.0), 0.03)
    assert math.isclose(signed_distance("short", 104.0, 100.0), 0.04)
    assert signed_distance("long", 101.0, 100.0) < 0  # crossed
    assert signed_distance("long", None, 100.0) is None
    assert signed_distance("flat", 90.0, 100.0) is None


def test_replay_account_matches_smart_money_replay():
    rows = [
        {"ts": 1, "account": "x", "market": "BTC", "side": "long", "size": 1.0},
        {"ts": 1, "account": "x", "market": "ETH", "side": "short", "size": 2.0},
        {"ts": 5, "account": "x", "market": "ETH", "side": "closed", "size": 0.0},
        {"ts": 7, "account": "x", "market": "SOL", "side": "long", "size": 3.0},
        {"ts": 9, "account": "x", "market": "BTC", "side": "long", "size": 1.5},
    ]
    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(r["ts"], []).append(r)
    for until in (1, 5, 7, 9):
        ref = replay_positions(rows, delta_start=1, until=until).get("x", {})
        mine = [s for ts, s, _ in replay_account(sorted(groups.items()), 1) if ts <= until][-1]
        assert {m: p["size"] for m, p in mine.items()} == {m: p["size"] for m, p in ref.items()}
    *_, (ts, _, since) = replay_account(sorted(groups.items()), 1)
    assert since == {"BTC": 9, "SOL": 7}


def _obs(seq, side="long", t0=0, step=HOUR):
    return [Obs(t0 + i * step, side if q is not False else None, q) for i, q in enumerate(seq)]


def test_episodes_entry_dedup_and_refractory():
    # in, in, out, in (2 h later, merged by 24 h refractory), out ... in again 30 h after the first start
    seq = [True, True, False, True, False] + [False] * 25 + [True]
    eps = detect_episodes(_obs(seq), refractory_s=DAY)
    assert [e.ts for e in eps] == [0, 30 * HOUR]
    assert [e.ts for e in detect_episodes(_obs(seq), refractory_s=0)] == [0, 3 * HOUR, 30 * HOUR]
    # unknown observations are skipped: in, ?, in is one episode even without a refractory
    assert len(detect_episodes(_obs([True, None, True]), refractory_s=0)) == 1


def test_episode_kinds():
    obs = [Obs(0, "long", True, first_obs=True, data_start=True), Obs(HOUR, None, False)]
    assert [e.kind for e in detect_episodes(obs)] == ["start"]
    assert detect_episodes([Obs(5 * HOUR, "short", True, first_obs=True)])[0].kind == "join"
    obs = [Obs(0, None, False), Obs(5 * HOUR, "short", True, after_gap=True)]
    assert detect_episodes(obs)[0].kind == "gap"
    obs = [Obs(0, None, False), Obs(HOUR, "short", True)]
    assert detect_episodes(obs)[0].kind == "fresh"
    # a side flip while in band is a new entry, even inside the refractory
    obs = [Obs(0, None, False), Obs(HOUR, "long", True), Obs(2 * HOUR, "short", True)]
    assert [(e.side, e.kind) for e in detect_episodes(obs)] == [("long", "fresh"), ("short", "fresh")]


def test_episodes_before_t_do_not_depend_on_later_data():
    base = [False, True, True, False, False, True] + [False] * 30
    t = 10 * HOUR
    full = detect_episodes(_obs(base + [True, False, True]))
    changed = detect_episodes(_obs(base + [False, True, True, True]))
    assert [e for e in full if e.ts <= t] == [e for e in changed if e.ts <= t]
    truncated = detect_episodes([o for o in _obs(base) if o.ts <= t])
    assert truncated == [e for e in full if e.ts <= t]


def test_summarize_and_gate():
    eps = [
        {"ts": 0, "account": "a", "coin": "BTC", "side": "long"},
        {"ts": HOUR, "account": "a", "coin": "BTC", "side": "short"},
        {"ts": 12 * DAY, "account": "b", "coin": "ETH", "side": "short"},
    ]
    s = summarize(eps, exposure_s=2 * WEEK, period=(0, 20 * DAY))
    assert s["n"] == 3 and math.isclose(s["per_week"], 1.5)
    assert s["coin_days"] == 2 and (s["long"], s["short"]) == (1, 2)
    assert s["top_coin"] == "BTC" and math.isclose(s["top_coin_share"], 2 / 3)
    assert math.isclose(s["top_account_share"], 2 / 3) and s["blocks"] == [2, 1]
    g = gate(3, 2 * WEEK)  # 1.5 per week passes the weekly bar but not 5 per 10 days (2.14)
    assert not g["passed"] and math.isclose(g["per_10d"], 1.5 * 10 / 7) and math.isclose(g["weeks_to_need"], 60.0)
    assert gate(8, WEEK)["passed"]  # 8 / wk = 11.4 per 10 d
    assert gate(0, WEEK)["weeks_to_need"] is None


def test_exposure_is_calendar_time_minus_gaps_not_number_of_runs():
    hourly = [i * HOUR for i in range(10)]
    assert exposure_seconds(hourly, 10 * HOUR) == 10 * HOUR
    # repeated runs 20 s and 30 min after a run add nothing; an irregular 55 / 45 min cadence loses nothing
    assert exposure_seconds([*hourly, 20, 3 * HOUR + 1800], 10 * HOUR) == 10 * HOUR
    assert exposure_seconds([0, 55 * 60, 100 * 60, 3 * HOUR], 4 * HOUR) == 4 * HOUR
    # a 5 h gap between runs counts 1 h; a 3 h tail without runs before the end counts 1 h
    assert exposure_seconds([0, HOUR, 6 * HOUR, 7 * HOUR], 10 * HOUR) == 10 * HOUR - 4 * HOUR - 2 * HOUR
    assert exposure_seconds([], HOUR) == 0.0


def test_history_covers_period_start_or_listing_day():
    start = 100 * DAY
    assert history_covers(start - DAY, start - 300 * DAY, start)  # old coin, full history
    assert history_covers(start - 900, None, start)  # first candle closes exactly at start
    assert not history_covers(start + 3 * DAY, start - 300 * DAY, start)  # old coin, history cut (5000-candle cap)
    assert history_covers(start + 10 * DAY + HOUR, start + 10 * DAY, start)  # listed during the period
    assert not history_covers(start + 12 * DAY, start + 10 * DAY, start)  # listed, but its first days are missing
    assert not history_covers(None, None, start)


def test_listed_after_explains_only_a_later_listing():
    end = 100 * DAY
    assert listed_after(end, end)  # first daily candle opens when the window closes
    assert not listed_after(end - DAY, end)  # the coin traded inside the window: the empty answer was wrong
    assert not listed_after(None, end)  # no candle at all for a live coin


def _pos(ts, market, side, size, liq):
    return {"ts": ts, "market": market, "side": side, "size": size, "value_usd": None, "liq_px": liq}


VARS = {"fresh": Variant(0.01, 0.04, 0), "any": Variant(0.01, 0.04, None)}


def _observe(groups, runs, delta_start, marks=None, watched=lambda r: True, **kw):
    marks = marks or {}
    return account_observations(
        groups,
        runs,
        delta_start=delta_start,
        watched=watched,
        top_at=lambda r: frozenset({"BTC", "ETH"}),
        mark=lambda c, r: marks.get((c, r), 100.0),
        variants=VARS,
        min_value=50.0,
        **kw,
    )


def test_observations_stale_liq_px_is_unknown_fresh_is_known():
    h = HOUR
    runs = [0, h, 2 * h, 3 * h, 4 * h, 5 * h]
    groups = [
        (0, [_pos(0, "BTC", "long", 1.0, 97.0)]),  # pre-v12 full group: fresh, d = 3 %
        # 1 h: no group before v12 -> the account was not polled: unknown everywhere
        (2 * h, [_pos(2 * h, "BTC", "long", 1.0, 97.5)]),  # v12 baseline: fresh, d = 2.5 %
        # 3 h: no rows -> BTC liq_px is 1 h old
        (4 * h, [_pos(4 * h, "ETH", "short", 10.0, 103.0)]),  # new ETH row: fresh, d = 3 %
        (5 * h, [_pos(5 * h, "ETH", "closed", 0.0, None)]),
    ]
    obs, meta, cnt = _observe(groups, runs, delta_start=2 * h)
    assert [o.qualifies for o in obs["fresh"]["BTC"]] == [True, None, True, None, None, None]
    assert [o.qualifies for o in obs["any"]["BTC"]] == [True, None, True, True, True, True]
    assert [o.qualifies for o in obs["fresh"]["ETH"]] == [False, None, False, False, True, False]
    first = obs["fresh"]["BTC"][0]
    assert first.first_obs and first.data_start and not obs["fresh"]["BTC"][2].first_obs
    assert math.isclose(meta[("BTC", 3 * h)]["liq_age_h"], 1.0) and math.isclose(meta[("ETH", 4 * h)]["d"], 0.03)
    assert cnt["unpolled_runs"] == 1 and cnt["in_scope"] == 6 and cnt["in_scope_fresh"] == 3
    # no mark -> unknown; a position worth less than min_value -> known False even with a stale liq_px
    obs, _, _ = _observe(groups, runs, 2 * h, marks={("BTC", 2 * h): None, ("BTC", 3 * h): 40.0})
    assert [o.qualifies for o in obs["fresh"]["BTC"]][2:4] == [None, False]


def test_observations_state_frozen_at_the_v12_boundary_is_unknown():
    h = HOUR
    # positions on the last pre-v12 run, nothing written at the v12 baseline (flat or failed request)
    groups = [(0, [_pos(0, "BTC", "long", 1.0, 97.0)])]
    obs, _, _ = _observe(groups, [0, h, 2 * h, 3 * h], delta_start=2 * h)
    assert [o.qualifies for o in obs["fresh"]["BTC"]] == [True, None, None, None]
    assert [o.qualifies for o in obs["any"]["BTC"]] == [True, None, True, True]


def test_observations_skip_unwatched_runs_and_mark_gaps():
    h = HOUR
    groups = [(0, [_pos(0, "BTC", "short", 1.0, 103.0)]), (3 * h, [_pos(3 * h, "BTC", "short", 2.0, 102.0)])]
    obs, _, _ = _observe(groups, [0, h, 2 * h, 3 * h], delta_start=0, watched=lambda r: r != h)
    o = obs["fresh"]["BTC"]
    assert [x.ts for x in o] == [0, 2 * h, 3 * h]  # 1 h off the list and without rows: not observed
    assert [x.after_gap for x in o] == [False, True, False]


def test_lower_bound_merges_entries_that_could_fall_in_the_refractory():
    # truth: in at 0, out at 1 h, back in at 23 h (merged by the 24 h refractory: one episode).
    # seen: 23 h unknown (stale liq_px), first fresh in-band snapshot at 24 h.
    seen = [Obs(0, "short", True, first_obs=True), Obs(HOUR, None, False)]
    seen += [Obs(t * HOUR, "short", None) for t in range(2, 24)] + [Obs(24 * HOUR, "short", True)]
    assert len(detect_episodes(seen, DAY)) == 2  # the default rule would count a phantom second episode
    eps = detect_episodes(seen, DAY, lower_bound=True)
    assert [(e.ts, e.kind, e.prev_known_ts) for e in eps] == [(0, "join", None)]
    # an unknown first run of the data is a possible censored episode: an entry 5 h later is merged into it
    seen = [Obs(0, None, None, first_obs=True, data_start=True), Obs(HOUR, None, False), Obs(5 * HOUR, "long", True)]
    assert detect_episodes(seen, DAY, lower_bound=True) == []
    assert [e.kind for e in detect_episodes(seen, DAY)] == ["fresh"]
    # far enough from the last known state: counted
    seen = [Obs(0, "long", True, first_obs=True), Obs(30 * HOUR, None, False), Obs(31 * HOUR, "long", True)]
    assert [(e.ts, e.prev_known_ts) for e in detect_episodes(seen, DAY, lower_bound=True)][1] == (31 * HOUR, 30 * HOUR)


def test_lower_bound_never_exceeds_the_count_on_all_snapshots():
    rng = random.Random(7)
    states = [(False, None), (True, "long"), (True, "short")]
    for _ in range(3000):
        n = rng.randint(1, 80)
        data_start = rng.random() < 0.5
        p_in = rng.choice([0.1, 0.3, 0.6])
        full = []
        for i in range(n):
            q, side = states[0] if rng.random() > p_in else rng.choice(states[1:])
            if rng.random() < 0.05:
                q = None  # unknown in both (no mark)
            full.append(Obs(i * HOUR, side, q, first_obs=i == 0, data_start=data_start and i == 0, after_gap=rng.random() < 0.05))
        p_hide = rng.choice([0.2, 0.5, 0.9])
        seen = [Obs(o.ts, o.side, None if rng.random() < p_hide else o.qualifies, o.first_obs, o.data_start, o.after_gap) for o in full]
        for rf in (0, 6 * HOUR, DAY, 3 * DAY):
            truth = sum(e.kind != "start" for e in detect_episodes(full, rf))
            lb = sum(e.kind != "start" for e in detect_episodes(seen, rf, lower_bound=True))
            assert lb <= truth, (rf, full, seen)
            # with every snapshot known the lower-bound rule is never above the default one either
            assert sum(e.kind != "start" for e in detect_episodes(full, rf, lower_bound=True)) <= truth


def test_lower_bound_episodes_before_t_do_not_depend_on_later_data():
    base = [False, True, None, False, None, True] + [False] * 30 + [None, True]
    t = (len(base) - 1) * HOUR  # only data after the base differs
    a = detect_episodes(_obs(base + [False, True, None, True]), DAY, lower_bound=True)
    b = detect_episodes(_obs(base + [None, None, True]), DAY, lower_bound=True)
    assert [e for e in a if e.ts <= t] == [e for e in b if e.ts <= t]


def test_step0_decision_needs_episodes_and_coin_day_clusters():
    same_day = [{"ts": 10 * DAY + i * HOUR, "account": f"a{i}", "coin": "BTC", "side": "short"} for i in range(8)]
    d = step0_decision(same_day, WEEK)
    assert d["episodes"]["passed"] and not d["coin_days"]["passed"] and d["outcome"] == "unresolved"
    assert d["coin_days"]["n"] == 1 and math.isclose(d["weeks_to_need_clusters"], 90.0)
    spread = [{"ts": i * DAY, "account": "a" if i < 5 else "b", "coin": "BTC" if i % 2 else "ETH", "side": "short"} for i in range(8)]
    d = step0_decision(spread, WEEK)
    assert d["passed"] and d["outcome"] == "pass" and math.isclose(d["weeks_to_need_clusters"], 90 / 8)
    assert d["descriptive"]["days"] == 8 and d["descriptive"]["top_account_n"] == 5
    assert d["descriptive"]["without_top_account"]["n"] == 3 and not d["descriptive"]["without_top_account"]["episodes"]["passed"]
    assert coin_day_clusters(spread) == 8 and step0_decision([], WEEK)["outcome"] == "unresolved"
    assert d["descriptive"]["coin_days_strict"] == 2  # no prev_known_ts: windows open to the left, one day per coin


def test_observations_poll_markers_decide_polling_in_the_recount_era():
    """Recount rules rev. 4: a marker (ok or not) for every account the collector meant to poll; the reconstructed
    watch list is not used from ``polls_from`` on."""
    h = HOUR
    runs = [0, h, 2 * h, 3 * h, 4 * h, 5 * h, 6 * h, 7 * h]
    groups = [
        (0, [_pos(0, "BTC", "short", 1.0, 103.0)]),
        (2 * h, [_pos(2 * h, "BTC", "short", 1.0, 103.0)]),  # refresh row at an ok marker
        (6 * h, [_pos(6 * h, "ETH", "short", 1.0, 103.0)]),  # rows at a not-ok marker (stopped before the flip)
        (7 * h, [_pos(7 * h, "ETH", "short", 2.0, 102.0)]),  # rows without a marker
    ]
    # 3 h: on the reconstructed list, no marker, no rows -> off the intended list, not observed (rev. 3: unpolled)
    # 4 h: not-ok marker, no rows (failed request or never reached) -> observed, not polled
    # 5 h: ok marker, no rows -> polled, BTC unchanged with a 3 h old liq_px
    polls = {2 * h: True, 4 * h: False, 5 * h: True, 6 * h: False}
    obs, _, cnt = _observe(groups, runs, delta_start=0, watched=lambda r: True, polls=polls, polls_from=2 * h)
    btc = obs["fresh"]["BTC"]
    assert [o.ts for o in btc] == [0, h, 2 * h, 4 * h, 5 * h, 6 * h, 7 * h]
    assert [o.after_gap for o in btc] == [False, False, False, True, False, False, False]
    assert [o.qualifies for o in btc] == [True, None, True, None, None, None, None]
    assert [o.qualifies for o in obs["any"]["BTC"]] == [True, True, True, None, True, True, True]
    assert [o.qualifies for o in obs["fresh"]["ETH"]] == [False, False, False, None, False, True, True]
    assert cnt["observed_runs"] == 7 and cnt["unpolled_runs"] == 1 and cnt["rows_unmarked"] == 1
    # the history path (no markers) is unchanged: the reconstructed list decides, post-v12 runs count as polled
    obs, _, cnt = _observe(groups, runs, delta_start=0, watched=lambda r: r != 3 * h)
    assert [o.ts for o in obs["any"]["BTC"]] == [0, h, 2 * h, 4 * h, 5 * h, 6 * h, 7 * h]
    assert cnt["unpolled_runs"] == 0 and "rows_unmarked" not in cnt
    # an account the collector meant to poll and never logged rows for still enters the completeness denominator
    _, _, cnt = _observe([], runs, delta_start=0, polls={2 * h: True, 3 * h: False}, polls_from=2 * h)
    assert cnt["observed_runs"] == 2 and cnt["unpolled_runs"] == 1
    assert _observe([], runs, delta_start=0)[2] == {}
    # an ok marker before the account's first group (no prior state) is a known flat book
    late = [(4 * h, [_pos(4 * h, "BTC", "short", 1.0, 103.0)])]
    obs, _, _ = _observe(late, runs, delta_start=0, polls={2 * h: True, 4 * h: True}, polls_from=2 * h)
    assert [(o.ts, o.qualifies) for o in obs["fresh"]["BTC"]] == [(2 * h, False), (4 * h, True)]
    # a top-coin position without a mark: counted, and as "big" when its logged value reaches min_value
    big = [(0, [{"ts": 0, "market": "BTC", "side": "short", "size": 1.0, "value_usd": 100.0, "liq_px": 103.0}])]
    _, _, cnt = _observe(big, [0], delta_start=0, marks={("BTC", 0): None})
    assert cnt["no_mark"] == 1 and cnt["no_mark_big"] == 1


def test_coin_day_clusters_strict_stabs_every_window():
    eps = [
        {"coin": "BTC", "ts": DAY + 10 * HOUR, "prev_known_ts": DAY + 8 * HOUR},  # window [day 0; day 1]
        {"coin": "BTC", "ts": 2 * DAY + 5 * HOUR, "prev_known_ts": DAY + 20 * HOUR},  # [day 0; day 2]: day 1 stabs it
        {"coin": "BTC", "ts": 5 * DAY + 1 * HOUR, "prev_known_ts": 4 * DAY + 23 * HOUR},  # [day 3; day 5]
        {"coin": "ETH", "ts": 5 * DAY, "prev_known_ts": None},  # open to the left
    ]
    assert coin_day_clusters(eps) == 4 and coin_day_clusters_strict(eps) == 3
    assert coin_day_clusters_strict([]) == 0


def test_strict_clusters_never_exceed_the_observer_with_all_snapshots():
    rng = random.Random(11)
    states = [(False, None), (True, "long"), (True, "short")]
    for _ in range(1500):
        full_eps, lb_eps = [], []
        p_in, p_hide, step = rng.choice([0.1, 0.3, 0.6]), rng.choice([0.2, 0.5, 0.9]), rng.choice([HOUR, 3 * HOUR])
        for acc in range(rng.randint(1, 4)):
            coin = rng.choice(["BTC", "ETH"])
            off = 0 if acc == 0 else rng.randrange(0, DAY, HOUR)
            data_start = off == 0 and rng.random() < 0.5
            full = []
            for i in range(rng.randint(1, 40)):
                q, side = states[0] if rng.random() > p_in else rng.choice(states[1:])
                q = None if rng.random() < 0.05 else q
                full.append(Obs(off + i * step, side, q, first_obs=i == 0, data_start=data_start and i == 0))
            seen = [Obs(o.ts, o.side, None if rng.random() < p_hide else o.qualifies, o.first_obs, o.data_start) for o in full]
            full_eps += [{"coin": coin, "ts": e.ts} for e in detect_episodes(full, DAY) if e.kind != "start"]
            lb_eps += [
                {"coin": coin, "ts": e.ts, "prev_known_ts": e.prev_known_ts}
                for e in detect_episodes(seen, DAY, lower_bound=True)
                if e.kind != "start"
            ]
        assert coin_day_clusters_strict(lb_eps) <= coin_day_clusters(full_eps), (full_eps, lb_eps)


def test_recount_completeness_and_outcome():
    ok = {"observed_runs": 100, "unpolled_runs": 4, "in_scope": 50, "in_scope_fresh": 48, "no_mark_big": 0}
    c = recount_completeness(ok, exposure_s=0.95 * 42 * DAY, calendar_s=42 * DAY)
    assert c["complete"] and math.isclose(c["polled"], 0.96) and math.isclose(c["known"], 0.96)
    passed, failed = {"passed": True}, {"passed": False}
    assert recount_outcome(passed, c) == "pass" and recount_outcome(failed, c) == "kill"
    # missing data never turns into a kill: poll failures, unknown liq_px / marks, collector gaps
    for bad, exp, which in (
        ({**ok, "unpolled_runs": 6}, 0.95, "polled"),
        ({**ok, "no_mark_big": 3}, 0.95, "known"),
        ({**ok, "in_scope_fresh": 40}, 0.95, "known"),
        (ok, 0.85, "exposure"),
        ({**ok, "rows_unmarked": 1}, 0.95, "unmarked"),  # markers do not cover the intended list (rev. 4)
    ):
        c = recount_completeness(bad, exposure_s=exp * 42 * DAY, calendar_s=42 * DAY)
        assert c["failed"] == [which] and recount_outcome(failed, c) == "incomplete"
    # no whale at all with complete polling is a real absence, not missing data
    none = {"observed_runs": 100, "unpolled_runs": 0}
    assert recount_outcome(failed, recount_completeness(none, 42 * DAY, 42 * DAY)) == "kill"
    assert recount_completeness({}, 0.0, 42 * DAY)["failed"] == ["polled", "exposure"]
