import random

import pytest

from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import (
    Plan,
    Trade,
    cluster_bootstrap_ci,
    cochran_q,
    holm,
    label_permutation_p,
    mde,
    per_call_r,
    permutation_p,
    placed_plan,
    plan_entry,
    power,
    relative_plan,
    sample_starts,
    simulate,
    snowflake_ms,
    verdict,
    week_of,
)

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def bars(*ohlc, t0=T0):
    return [Bar(t0 + i * H, *x) for i, x in enumerate(ohlc)]


def test_snowflake_matches_a_known_tweet_time():
    # 1841082810225848793: KillaXBT 2024-10-01 11:48 UTC (note in docs/calls/x/KillaXBT.csv)
    ms = snowflake_ms("1841082810225848793")
    assert ms // 60_000 * 60_000 == 1727783280000  # 2024-10-01T11:48:00Z


def test_week_of_starts_on_monday():
    monday = 1_727_049_600_000  # 2024-09-23 00:00 UTC, a Monday
    assert week_of(monday) == week_of(monday + 7 * 86_400_000 - 1) == week_of(monday - 1) + 1


@pytest.mark.parametrize(
    ("side", "ref", "kind", "entry"),
    [
        ("long", 105.0, "limit", 100.0),  # price above the zone: limit at the top
        ("long", 90.0, "stop", 98.0),  # price below: breakout stop order at the bottom
        ("long", 99.0, "market", None),  # inside the zone
        ("short", 90.0, "limit", 98.0),  # price below the zone: limit at the bottom
        ("short", 105.0, "stop", 100.0),  # price above: stop order at the top
    ],
)
def test_plan_entry_nearest_edge(side, ref, kind, entry):
    p = plan_entry(side, 98.0, 100.0, ref, disclosure=False, stop=None, target=None)
    assert (p.kind, p.entry) == (kind, entry)


def test_plan_entry_single_price_near_market_and_disclosure_and_limit_style():
    assert plan_entry("long", 100.0, 100.0, 100.4, disclosure=False, stop=None, target=None).kind == "market"
    assert plan_entry("long", 100.0, 100.0, 101.0, disclosure=False, stop=None, target=None).kind == "limit"
    assert plan_entry("short", 98.0, 100.0, 50.0, disclosure=True, stop=None, target=None).kind == "market"
    p = plan_entry("long", 98.0, 100.0, 90.0, disclosure=False, stop=None, target=None, style="limit")
    assert (p.kind, p.entry) == ("limit", 100.0)


def test_limit_long_target_in_r_with_costs():
    b = bars((105, 106, 99, 101), (101, 112, 100, 111))
    t = simulate(b, Plan("long", "limit", 100.0, 95.0, 110.0), start_ms=T0, fill_ms=48 * H, hold_ms=48 * H, cost_frac=0.0015)
    assert t.status == "target" and t.fill == 100.0
    assert t.r_gross == pytest.approx(2.0)
    assert t.r_net == pytest.approx(2.0 - 0.0015 * 100 / 5)
    assert t.ret_net == pytest.approx(0.10 - 0.0015)


def test_stop_order_long_fills_on_break_and_same_bar_stop_and_target_is_stop():
    b = bars((90, 91, 89, 90), (90, 101, 90, 100), (100, 120, 94, 110))
    t = simulate(b, Plan("long", "stop", 98.0, 95.0, 115.0), start_ms=T0, fill_ms=48 * H, hold_ms=48 * H, cost_frac=0.0)
    assert t.fill == 98.0 and t.fill_t == T0 + H
    assert t.status == "stop" and t.r_gross == pytest.approx(-1.0)


def test_short_limit_gap_and_time_exit():
    b = bars((90, 91, 89, 90), (103, 104, 102, 103), (103, 103, 101, 102), (102, 102, 100, 101))
    t = simulate(b, Plan("short", "limit", 100.0, 110.0, None), start_ms=T0, fill_ms=48 * H, hold_ms=2 * H, cost_frac=0.0)
    assert t.fill == 103.0  # gap through the limit fills at the open
    assert t.status == "time" and t.exit == 102.0  # close of the last bar opening within 2 h of the fill bar
    assert t.r_gross == pytest.approx((1 - 102 / 103) * 103 / 10)


def test_no_fill_pending_and_cancel():
    b = bars((105, 106, 104, 105), (105, 106, 101, 105), (105, 106, 102, 103))
    p = Plan("long", "limit", 100.0, 95.0, None)
    assert simulate(b, p, start_ms=T0, fill_ms=2 * H, hold_ms=H, cost_frac=0).status == "no_fill"
    assert simulate(b, p, start_ms=T0, fill_ms=48 * H, hold_ms=H, cost_frac=0).status == "pending"
    assert simulate(b, p, start_ms=T0, fill_ms=48 * H, hold_ms=H, cost_frac=0, cancel_ms=T0 + 2 * H).status == "no_fill"
    assert simulate(b, p, start_ms=T0, fill_ms=48 * H, hold_ms=H, cost_frac=0).fill is None


def test_wrong_side_stop_gives_no_r_and_wrong_side_target_is_ignored():
    b = bars((100, 101, 99, 100), (100, 101, 99, 100))
    t = simulate(b, Plan("long", "market", None, 101.0, 99.0), start_ms=T0, fill_ms=H, hold_ms=2 * H, cost_frac=0)
    assert t.r_net is None and t.status in ("time", "open")


def test_author_stop_and_close():
    b = bars((105, 106, 99, 101), (101, 102, 96, 97), (97, 98, 96, 97))
    p = Plan("long", "limit", 100.0, 95.0, 110.0)
    s = simulate(b, p, start_ms=T0, fill_ms=48 * H, hold_ms=48 * H, cost_frac=0, author=("stop", T0 + H + 5))
    assert s.status == "author_stop" and s.r_gross == pytest.approx(-1.0)
    c = simulate(b, p, start_ms=T0, fill_ms=48 * H, hold_ms=48 * H, cost_frac=0, author=("close", T0 + H + 60_000))
    assert c.status == "author_close" and c.exit == 97 and c.r_gross == pytest.approx(-0.6)
    # no fill in our bars before the author's exit: his word counts, fill at the planned entry
    far = Plan("long", "limit", 90.0, 85.0, None)
    f = simulate(b, far, start_ms=T0, fill_ms=48 * H, hold_ms=48 * H, cost_frac=0, author=("close", T0 + 2 * H))
    assert f.fill == 90.0 and f.exit == 97


def test_relative_plan_round_trip_and_mirror():
    p = Plan("long", "limit", 98.0, 95.0, 110.0)
    rel = relative_plan(p, 100.0)
    same = placed_plan(rel, "long", 200.0)
    assert (same.entry, same.stop, same.target) == pytest.approx((196.0, 190.0, 220.0))
    flip = placed_plan(rel, "short", 100.0)
    assert (flip.entry, flip.stop, flip.target) == pytest.approx((102.0, 105.0, 90.0))
    assert flip.kind == "limit"


def test_results_before_t_do_not_change_when_later_data_change():
    b = bars((105, 106, 99, 101), (101, 112, 100, 111), (111, 111, 111, 111))
    later = b[:2] + [Bar(b[2].t, 50, 200, 1, 60)] + bars((1, 2, 0.5, 1), t0=b[2].t + H)
    p = Plan("long", "limit", 100.0, 95.0, 110.0)
    kw = {"start_ms": T0, "fill_ms": 48 * H, "hold_ms": 48 * H, "cost_frac": 0.001}
    assert simulate(b, p, **kw) == simulate(later, p, **kw)  # exit happens on bar 1; later bars are irrelevant


def test_per_call_r_counts_unfilled_as_zero():
    ts = [Trade("no_fill"), Trade("pending"), Trade("stop", fill=1.0, r_net=-1.1), Trade("time", fill=1.0, r_net=None)]
    assert per_call_r(ts) == [0.0, -1.1]


def test_cluster_bootstrap_ci_brackets_mean_and_degenerates_for_constant():
    lo, hi = cluster_bootstrap_ci([1.0] * 6, [1, 1, 2, 2, 3, 3], n_boot=500)
    assert lo == hi == 1.0
    rnd = random.Random(1)
    v = [rnd.gauss(0.5, 1.0) for _ in range(200)]
    lo, hi = cluster_bootstrap_ci(v, [i // 4 for i in range(200)], n_boot=2000)
    m = sum(v) / len(v)
    assert lo < m < hi and hi - lo < 1.0


def test_permutation_p_and_holm():
    assert permutation_p(1.0, [0.0] * 99) == pytest.approx(0.01)
    assert permutation_p(0.0, [0.0] * 99) == 1.0
    assert holm([0.01, 0.04, 0.03, 0.5]) == [True, False, False, False]
    assert holm([0.001, 0.012, 0.02, 0.04]) == [True, True, True, True]


def test_mde_and_power_are_consistent():
    m = mde(1.5, 50)
    assert m == pytest.approx((1.6448536 + 0.8416212) * 1.5 / 50**0.5, rel=1e-6)
    assert power(m, 1.5, 50) == pytest.approx(0.8, abs=1e-6)


def test_cochran_q_and_label_permutation():
    q, df, i2 = cochran_q([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
    assert (q, df, i2) == (0.0, 2, 0.0)
    q, df, i2 = cochran_q([0.0, 2.0], [0.5, 0.5])
    assert q == pytest.approx(8.0) and i2 == pytest.approx(7 / 8)
    vals = [0.0] * 20 + [5.0] * 20
    assert label_permutation_p(vals, ["a"] * 20 + ["b"] * 20, n=500) < 0.01
    assert label_permutation_p(vals, ["a", "b"] * 20, n=500) > 0.5


def test_verdict_rules():
    base = dict(n_calls=60, n_filled=40, mean_r=0.4, ci_low=0.1, p_placebo=0.005, p_placebo_mech=0.01)
    assert verdict(**base, halves_positive=True, without_recovered_positive=True).mark == "✓"
    assert verdict(**base, halves_positive=False, without_recovered_positive=True).mark == "≈"
    assert verdict(**{**base, "p_placebo": 0.2}, halves_positive=True, without_recovered_positive=True).mark == "✗"
    assert verdict(**{**base, "n_calls": 20}, halves_positive=True, without_recovered_positive=True).mark == "◯"


def test_sample_starts_within_bounds():
    times = list(range(0, 100))
    got = sample_starts(times, 10, 20, 50, random.Random(3))
    assert len(got) == 50 and all(10 <= x <= 20 for x in got)
    assert sample_starts(times, 200, 300, 5, random.Random(3)) == []
