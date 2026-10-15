import math

import pytest

from tradedesk.analytics.holders_revenue import (
    DAY_S,
    DropRule,
    LegResult,
    beta,
    cluster_bootstrap_mean_ci,
    cluster_se,
    combine,
    drop_events,
    excluded_from_control_pool,
    hedged_return,
    max_adverse_short,
    nearest_controls,
    pair_net,
    qualifies,
    rank_controls,
    short_vs_btc_net,
    to_daily,
    window_state,
)

RULE = DropRule(window=30, drop=0.5, min_prior=100_000, min_history=180, regular_days=90, min_nonzero=12, refractory=60, lag=1)


def flat(level: float, start: int, end: int) -> dict[int, float]:
    return {d: level for d in range(start, end + 1)}


def test_to_daily_and_combine():
    s = to_daily([[0, 5.0], [3600, 1.0], [DAY_S, 2.0], [2 * DAY_S, None]])
    assert s == {0: 6.0, 1: 2.0}
    c = combine([{0: 1.0, 1: 1.0}, {1: 2.0, 2: 3.0}])
    assert c == {0: 1.0, 1: 3.0, 2: 3.0}


def test_event_fires_on_the_first_day_the_drop_reaches_half():
    # 10k/day for 400 days, then 0 per day: current/prior over 30/30 hits 0.5 after 15 zero days
    s = flat(10_000.0, 0, 399) | flat(0.0, 400, 500)
    ev = drop_events(s, RULE, first_day=200, last_day=500)
    assert len(ev) == 1
    e = ev[0]
    assert e.signal_day == 414  # window 385..414: 15 days of 10k + 15 of 0 = 150k vs prior 300k
    assert e.entry_day == 415
    assert e.current == pytest.approx(150_000) and e.prior == pytest.approx(300_000)
    assert e.ratio == pytest.approx(0.5)


def test_refractory_and_repeat():
    # two separate collapses 200 days apart -> two events; continuing zero inside 60 days -> no repeat
    s = flat(10_000.0, 0, 399) | flat(0.0, 400, 449) | flat(10_000.0, 450, 699) | flat(0.0, 700, 800)
    ev = drop_events(s, RULE, first_day=200, last_day=800)
    assert [e.signal_day for e in ev] == [414, 714]


def test_prior_threshold_history_regularity_and_missing_days():
    s = flat(1_000.0, 0, 399) | flat(0.0, 400, 500)  # prior 30k < 100k
    assert drop_events(s, RULE, first_day=200, last_day=500) == []
    young = flat(0.0, 0, 299) | flat(10_000.0, 300, 399) | flat(0.0, 400, 500)  # first non-zero day 300: 114 < 180
    assert drop_events(young, RULE, first_day=200, last_day=500) == []
    monthly = {d: (400_000.0 if d % 30 == 0 else 0.0) for d in range(0, 600)}  # one payment a month: irregular
    assert drop_events(monthly, RULE, first_day=200, last_day=599) == []
    gap = flat(10_000.0, 0, 399) | flat(0.0, 400, 500)
    del gap[405]
    assert window_state(gap, 414, RULE) is None  # a missing row is not a zero
    assert qualifies(gap, 414, RULE, 0) is None


def test_tradable_filter_does_not_consume_refractory():
    s = flat(10_000.0, 0, 399) | flat(0.0, 400, 500)
    ev = drop_events(s, RULE, first_day=200, last_day=500, tradable=lambda entry: entry >= 420)
    assert [e.signal_day for e in ev] == [419]


def test_point_in_time_events_before_t_do_not_depend_on_later_data():
    base = flat(10_000.0, 0, 399) | flat(0.0, 400, 449) | flat(10_000.0, 450, 699) | flat(0.0, 700, 800)
    t = 600
    before = [e for e in drop_events(base, RULE, first_day=200, last_day=800) if e.signal_day <= t]
    changed = dict(base)
    for d in range(t + 1, 801):
        changed[d] = 1e9 if d % 2 else 0.0
    after = [e for e in drop_events(changed, RULE, first_day=200, last_day=800) if e.signal_day <= t]
    assert before == after and len(before) == 1
    truncated = {d: v for d, v in base.items() if d <= t}
    assert drop_events(truncated, RULE, first_day=200, last_day=t) == before


def test_beta_and_hedged_return_analytic():
    btc = {d: 100.0 * math.exp(0.01 * math.sin(d)) for d in range(0, 200)}
    tok = {d: 5.0 * (btc[d] / 100.0) ** 2 for d in range(0, 200)}  # log returns exactly 2x BTC's
    assert beta(tok, btc, 150) == pytest.approx(2.0)
    btc2 = {0: 100.0, 10: 110.0}
    tok2 = {0: 1.0, 10: 1.3}
    assert hedged_return(tok2, btc2, 0, 10, 1.5) == pytest.approx(0.3 - 1.5 * 0.1)
    assert hedged_return(tok2, btc2, 0, 11, 1.0) is None
    assert beta({0: 1.0}, btc, 150) is None


def test_beta_ignores_data_after_end_day():
    btc = {d: 100.0 + d for d in range(0, 120)}
    tok = {d: 10.0 + 0.5 * d for d in range(0, 120)}
    b1 = beta(tok, btc, 80)
    tok2 = dict(tok) | {d: 1e6 for d in range(81, 120)}
    assert beta(tok2, btc, 80) == b1


def test_max_adverse_short():
    highs = {1: 11.0, 2: 13.0, 3: 9.0, 4: 50.0}
    assert max_adverse_short(highs, 10.0, 0, 3) == pytest.approx(0.3)


def test_pair_net_and_short_vs_btc():
    ev = LegResult(beta=1.2, ar=-0.10, funding=0.01, side_cost=0.0015)
    c1 = LegResult(beta=1.0, ar=0.02, funding=0.02, side_cost=0.0015)
    c2 = LegResult(beta=0.8, ar=0.00, funding=0.00, side_cost=0.00155)
    gross, net = pair_net(ev, [c1, c2], btc_funding=0.03, btc_side_cost=0.0007)
    assert gross == pytest.approx(0.01 - (-0.10))
    b_net = 1.2 - 0.9
    costs = 2 * 0.0015 + 2 * 0.001525 + b_net * 2 * 0.0007
    assert net == pytest.approx(0.11 - costs + 0.01 - 0.01 - b_net * 0.03)
    g2, n2 = short_vs_btc_net(ev, btc_funding=0.03, btc_side_cost=0.0007)
    assert g2 == pytest.approx(0.10)
    assert n2 == pytest.approx(0.10 - (0.003 + 1.2 * 0.0014) + 0.01 - 1.2 * 0.03)
    with pytest.raises(ValueError):
        pair_net(ev, [], btc_funding=0.0, btc_side_cost=0.0)


def test_matching():
    assert nearest_controls(0.1, {"a": 0.5, "b": 0.12, "c": 0.05, "d": 0.09}, 2) == ["d", "b"]
    cands = {"a": (0.0, 0.0), "b": (1.0, 1.0), "c": (0.9, -1.0)}
    assert rank_controls((0.95, 0.9), cands, 1) == ["b"]
    assert rank_controls((0.0, 0.0), {}, 3) == []


def test_control_pool_excludes_only_past_events():
    entry = 1000
    # own event within the past 60 days (inclusive bounds) -> excluded
    assert excluded_from_control_pool([940], entry)
    assert excluded_from_control_pool([1000], entry)
    assert excluded_from_control_pool([970], entry)
    # older than 60 days -> stays in the pool
    assert not excluded_from_control_pool([939], entry)
    # a future event of the control token must not exclude it (no look-ahead)
    assert not excluded_from_control_pool([1001], entry)
    assert not excluded_from_control_pool([1030, 1059], entry)
    assert not excluded_from_control_pool([], entry)
    assert excluded_from_control_pool([1030, 950], entry)
    assert not excluded_from_control_pool([985], entry, lookback=10)


def test_cluster_se_matches_iid_formula_for_singletons_and_inflates_for_clusters():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    iid = math.sqrt(sum((x - 3.5) ** 2 for x in xs) / len(xs) ** 2 * len(xs) / (len(xs) - 1))
    assert cluster_se(xs, [list(range(6))]) == pytest.approx(iid)
    # duplicated observations in the same cluster: SE should not shrink like sqrt(2n)
    dup = xs + xs
    ids = list(range(6)) * 2
    assert cluster_se(dup, [ids]) > cluster_se(dup, [list(range(12))])
    two = cluster_se(dup, [ids, list(range(12))])
    assert two == pytest.approx(cluster_se(dup, [ids]))  # second dimension adds nothing with singleton clusters
    ci = cluster_bootstrap_mean_ci(xs, list(range(6)), reps=2000)
    assert ci is not None and ci[0] < 3.5 < ci[1]
