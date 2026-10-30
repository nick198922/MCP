import math
import statistics

import pytest

from tradedesk.analytics.premarket_fade import (
    DAY_MS,
    HOUR_MS,
    beta_ols,
    bootstrap_diff_ci,
    bootstrap_mean_ci,
    entry_point,
    event_outcome,
    funding_sum,
    mark_h1,
    mark_h2,
    mark_h3,
    registered_t_conv,
    round_trip_cost,
    spearman,
    stop_trade,
    summarize,
    t_quantile,
    tercile_groups,
)

T0 = 1_760_000_400_000 - (1_760_000_400_000 % HOUR_MS)  # an hour boundary


def flat(start: int, n: int, price: float) -> dict[int, tuple[float, float, float, float]]:
    return {start + i * HOUR_MS: (price, price, price, price) for i in range(n)}


def test_registered_t_conv_priority():
    assert registered_t_conv([T0 + 5], T0, T0 + HOUR_MS) == (T0 + 5, "announced")
    # several announced stamps: nearest to the index start
    assert registered_t_conv([T0 - 2 * DAY_MS, T0 + 2 * 60_000], T0, None) == (T0 + 2 * 60_000, "announced")
    assert registered_t_conv([], T0, T0 + 3 * HOUR_MS) == (T0 + 3 * HOUR_MS, "spot")
    assert registered_t_conv([], T0, None) == (T0, "index")
    assert registered_t_conv([], None, None) == (None, "none")


def test_entry_point_on_and_off_the_hour():
    bars = flat(T0 - 10 * HOUR_MS, 20, 1.0)
    bars[T0 - HOUR_MS] = (1, 1, 1, 2.0)  # last bar fully before T0
    bars[T0] = (1, 1, 1, 1.5)  # first bar at/after T0
    e = entry_point(bars, T0)
    assert e is not None
    assert (e.p_pre, e.p0, e.t_e) == (2.0, 1.5, T0 + HOUR_MS)
    assert e.gap == pytest.approx(math.log(1.5 / 2.0))
    # conversion at 13:33: the 13:00 bar straddles -> pre = 12:00 bar, entry = 14:00 bar
    e2 = entry_point(bars, T0 + 33 * 60_000)
    assert e2 is not None and e2.pre_open == T0 - HOUR_MS and e2.entry_open == T0 + HOUR_MS


def test_event_outcome_analytic():
    bars = flat(T0, 200, 10.0)
    btc = flat(T0, 200, 100.0)
    h = 7 * DAY_MS
    t_e = T0 + HOUR_MS
    bars[t_e + h - HOUR_MS] = (10, 10, 10, 8.0)
    btc[t_e + h - HOUR_MS] = (100, 100, 100, 110.0)
    fund_tok = [(t_e, 0.5), (t_e + 4 * HOUR_MS, 0.001), (t_e + h, 0.002), (t_e + h + 1, 0.9)]
    fund_btc = [(t_e + 8 * HOUR_MS, 0.0001)]
    o = event_outcome(bars, btc, fund_tok, fund_btc, t_e, 10.0, h, 1.0)
    assert o is not None
    assert o.gross == pytest.approx(math.log(0.8) - math.log(1.1))
    assert o.f_tok == pytest.approx(0.003)  # (t_e, t_e + h]
    assert o.cost == pytest.approx(0.002)
    assert o.net == pytest.approx(o.gross - 0.003 + 0.0001 + 0.002)
    assert event_outcome(bars, btc, [], [], t_e, 10.0, 300 * HOUR_MS, 1.0) is None  # missing bar


def test_no_lookahead():
    """Outcomes up to horizon k and the entry do not change when bars after t_e + k change."""
    bars = {T0 + i * HOUR_MS: (1 + i / 100, 1 + i / 90, 1 + i / 110, 1 + i / 100) for i in range(400)}
    btc = {T0 + i * HOUR_MS: (100, 100, 100, 100 + math.sin(i)) for i in range(400)}
    t_conv = T0 + 50 * HOUR_MS
    e = entry_point(bars, t_conv)
    assert e is not None
    h = DAY_MS
    o1 = event_outcome(bars, btc, [], [], e.t_e, e.p0, h, 1.0)
    s1 = stop_trade(bars, btc, [], [], e.t_e, e.p0, h, 1.0)
    b1 = beta_ols(bars, btc, t_conv, min_obs=10)
    later = dict(bars)
    for t in later:
        if t >= e.t_e + h:
            later[t] = (9, 9, 9, 9.0)
    assert entry_point(later, t_conv) == e
    assert event_outcome(later, btc, [], [], e.t_e, e.p0, h, 1.0) == o1
    assert stop_trade(later, btc, [], [], e.t_e, e.p0, h, 1.0) == s1
    # beta uses only bars ending by t_conv
    post = {t: (v if t + HOUR_MS <= t_conv else (5, 5, 5, 5.0)) for t, v in bars.items()}
    assert beta_ols(post, btc, t_conv, min_obs=10) == b1


def test_stop_trade_hits_stop():
    bars = flat(T0, 200, 10.0)
    btc = flat(T0, 200, 100.0)
    t_e = T0 + HOUR_MS
    bars[t_e + 5 * HOUR_MS] = (10, 14.0, 10, 12.0)
    s = stop_trade(bars, btc, [], [], t_e, 10.0, 7 * DAY_MS, 1.0)
    assert s is not None and s.stopped and s.t_exit == t_e + 6 * HOUR_MS
    assert s.pnl == pytest.approx(-0.30 - round_trip_cost(1.0))
    s2 = stop_trade(flat(T0, 200, 10.0), btc, [(t_e + 1, 0.01)], [], t_e, 10.0, DAY_MS, 0.0)
    assert s2 is not None and not s2.stopped and s2.pnl == pytest.approx(0.01 - 0.001)


def test_beta_ols_recovers_slope_and_defaults():
    btc = {T0 + i * HOUR_MS: (1, 1, 1, 100 * math.exp(0.01 * math.sin(i))) for i in range(100)}
    tok = {t: (1, 1, 1, 5 * math.exp(2 * 0.01 * math.sin((t - T0) // HOUR_MS))) for t in btc}
    b, n = beta_ols(tok, btc, T0 + 100 * HOUR_MS)
    assert b == pytest.approx(2.0, abs=1e-9) and n == 99
    assert beta_ols(tok, btc, T0 + 10 * HOUR_MS) == (1.0, 9)  # too few -> default
    neg = {t: (1, 1, 1, 5 * math.exp(-5 * 0.01 * math.sin((t - T0) // HOUR_MS))) for t in btc}
    assert beta_ols(neg, btc, T0 + 100 * HOUR_MS)[0] == 0.0  # clipped


def test_funding_sum_half_open():
    f = [(10, 0.1), (20, 0.2), (30, 0.3)]
    assert funding_sum(f, 10, 30) == pytest.approx(0.5)


def test_t_quantile_matches_tables():
    assert t_quantile(0.975, 24) == pytest.approx(2.0639, abs=2e-3)
    assert t_quantile(0.975, 9) == pytest.approx(2.2622, abs=5e-3)


def test_bootstrap_and_summary():
    xs = [-0.1, 0.0, 0.1, 0.2, -0.2] * 6
    lo, hi = bootstrap_mean_ci(xs, n_boot=4000)
    assert lo < 0 < hi
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    assert hi - lo == pytest.approx(2 * 1.96 * se, rel=0.2)
    s = summarize(xs, n_boot=2000)
    assert s.n == 30 and s.mean == pytest.approx(0.0, abs=1e-12) and s.share_neg == pytest.approx(0.4)
    assert s.t_lo < 0 < s.t_hi
    dlo, dhi = bootstrap_diff_ci([1.0, 1.1, 0.9] * 4, [0.0, 0.1, -0.1] * 4, n_boot=2000)
    assert 0.8 < dlo < 1.0 < dhi < 1.2


def test_terciles_and_spearman():
    bot, top = tercile_groups([5.0, 1.0, 3.0, 4.0, 2.0, 6.0, 0.0])
    assert bot == [6, 1] and top == [0, 5]
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2, 3], [1, 1, 2, 3]) == pytest.approx(1.0)


def test_marks():
    s = summarize([-0.08] * 10 + [-0.06] * 10, n_boot=500)
    assert mark_h1(-0.08, s) == "✓"
    assert mark_h1(-0.03, s) == "≈"
    assert mark_h1(-0.1, summarize([0.01, 0.02, 0.0], n_boot=500)) == "✗"
    assert mark_h2(summarize([-0.05] * 8 + [0.01] * 2, n_boot=500)) == "✓"
    assert mark_h2(summarize([-0.01, 0.0, -0.02], n_boot=500)) == "≈"
    assert mark_h3(-0.06, -0.01) == "✓" and mark_h3(-0.06, 0.02) == "≈" and mark_h3(0.01, 0.1) == "✗"
