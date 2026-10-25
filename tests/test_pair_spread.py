import math

import numpy as np
import pytest

from tradedesk.analytics.pair_spread import (
    Params,
    average_linkage,
    close_trade,
    cluster_bootstrap_ci,
    cluster_se,
    control_cross_sector,
    control_low_corr,
    form_pairs,
    hedge_weights,
    ols_slope,
    pairwise_corr,
    residuals,
    rolling_z,
    simulate_pair,
)

NAN = float("nan")


def test_pairwise_corr_matches_numpy_on_complete_rows():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(50, 3))
    x[:, 1] = 2 * x[:, 0] + 1
    x[:, 2] = -x[:, 0]
    r = pairwise_corr(x)
    assert r[0, 1] == pytest.approx(1.0)
    assert r[0, 2] == pytest.approx(-1.0)
    y = rng.normal(size=(40, 2))
    y[3, 0] = NAN
    y[7, 1] = NAN
    ok = np.isfinite(y).all(axis=1)
    assert pairwise_corr(y)[0, 1] == pytest.approx(np.corrcoef(y[ok, 0], y[ok, 1])[0, 1])
    assert math.isnan(pairwise_corr(np.array([[1.0, 2.0], [NAN, 3.0], [2.0, NAN]]))[0, 1])


def test_ols_slope_and_residuals():
    x = np.arange(10, dtype=float)
    assert ols_slope(3 * x + 1, x) == pytest.approx(3.0)
    m = np.array([1.0, -1.0, 2.0, -2.0, 0.5, -0.5])
    e = np.array([1.0, 1.0, -1.0, -1.0, 0.0, 0.0])  # orthogonal to m and mean zero
    r = residuals(np.column_stack([0.1 + 2 * m + e]), m)
    assert np.allclose(r[:, 0], e)


def test_average_linkage_blocks_and_cut():
    d = np.array([[0, 0.1, 0.9, 0.9], [0.1, 0, 0.9, 0.9], [0.9, 0.9, 0, 0.2], [0.9, 0.9, 0.2, 0]])
    assert average_linkage(d, 0.5) == [0, 0, 1, 1]
    assert average_linkage(d, 0.05) == [0, 1, 2, 3]
    assert average_linkage(d, 1.0) == [0, 0, 0, 0]
    # average linkage: d({0,1}, 2) = (0.3 + 0.7) / 2 = 0.5 > 0.45, single linkage would merge at 0.3
    d2 = np.array([[0, 0.1, 0.3], [0.1, 0, 0.7], [0.3, 0.7, 0]])
    assert average_linkage(d2, 0.45) == [0, 0, 1]


def test_form_pairs_one_pair_per_sector():
    rng = np.random.default_rng(7)
    n = 400
    mkt = rng.normal(0, 0.03, n)
    s1, s2 = rng.normal(0, 0.02, (2, n))
    cols = [mkt + s1 + rng.normal(0, 0.01, n), mkt + s1 + rng.normal(0, 0.01, n), mkt + s2 + rng.normal(0, 0.01, n)]
    cols.append(mkt + s2 + rng.normal(0, 0.01, n))
    cols.append(mkt + s2 + rng.normal(0, 0.02, n))  # third member of sector 2: lower residual correlation
    f = form_pairs(np.column_stack(cols), mkt, rho_min=0.5)
    assert f.labels[0] == f.labels[1] and f.labels[2] == f.labels[3] == f.labels[4] and f.labels[0] != f.labels[2]
    assert sorted((p.a, p.b) for p in f.pairs) == [(0, 1), (2, 3)]
    for p in f.pairs:
        assert 0.8 < p.h < 1.25 and 0.85 < p.rho < 0.98 and p.rho_e > 0.65
    assert control_cross_sector(f.rho, f.rho_e, f.labels, 5, rho_min=0.5, rho_e_max=0.2) != []
    assert all(f.labels[i] != f.labels[j] for i, j in control_cross_sector(f.rho, f.rho_e, f.labels, 5, rho_min=0.5, rho_e_max=0.2))


def test_form_pairs_rho_window_excludes_duplicates():
    rng = np.random.default_rng(3)
    a = rng.normal(0, 0.03, 300)
    x = np.column_stack([a, a * 1.0000001, a + rng.normal(0, 0.03, 300)])
    f = form_pairs(x, rng.normal(0, 0.03, 300), rho_min=0.5, cut=2.0)
    assert (0, 1) not in [(p.a, p.b) for p in f.pairs]  # rho ~ 1 is a rename/duplicate guard


def test_controls_disjoint():
    rho = np.full((6, 6), 0.1)
    np.fill_diagonal(rho, 1.0)
    out = control_low_corr(rho, 10, np.random.default_rng(0))
    used = [s for p in out for s in p]
    assert len(out) == 3 and len(set(used)) == 6
    assert control_low_corr(rho, 2, np.random.default_rng(0)).__len__() == 2


def test_hedge_weights():
    assert hedge_weights(1.0) == (1.0, 1.0)
    wa, wb = hedge_weights(2.0)
    assert wa == pytest.approx(2 / 3) and wb == pytest.approx(4 / 3)


def test_rolling_z_analytic():
    s = np.array([0.0, 1.0, 2.0, 3.0, 10.0])
    z = rolling_z(s, window=3, min_obs=3)
    assert np.isnan(z[:2]).all()
    w = np.array([2.0, 3.0, 10.0])
    assert z[4] == pytest.approx((10 - w.mean()) / w.std(ddof=1))
    assert z[2] == pytest.approx(1.0)  # (2 - 1) / 1


def flat(n, v=100.0):
    return np.full(n, v)


def test_simulate_target_exit_and_funding():
    n = 20
    oa, ca, ob, cb = flat(n), flat(n), flat(n), flat(n)
    oa[8] = 95.0
    fa, fb = np.zeros(n), np.zeros(n)
    fa[6] = fa[7] = 0.001
    fa[8] = 0.5  # settles after the exit open: not counted
    z = np.zeros(n)
    z[:5] = 0.0
    z[5], z[6], z[7] = 2.5, 1.0, 0.3
    tr, nxt = simulate_pair(oa, ca, ob, cb, fa, fb, z, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=12)
    assert len(tr) == 1
    t = tr[0]
    assert (t.signal, t.entry, t.exit, t.side, t.reason) == (5, 6, 8, -1, "target")
    assert t.price == pytest.approx(0.05)  # short A from 100 to 95
    assert t.funding == pytest.approx(0.002)  # the short leg receives two settlements of 0.1 %
    assert t.net(0.0062) == pytest.approx(0.05 + 0.002 - 0.0062)
    assert nxt == 8


def test_simulate_time_exit_reentry_and_hold_past_month():
    n = 40
    z = np.full(n, -2.2)  # never reverts, never stops
    o = flat(n)
    f = np.zeros(n)
    tr, nxt = simulate_pair(o, o, o, o, f, f, z, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=12, p=Params(max_hold=10))
    assert [(t.signal, t.entry, t.exit, t.side, t.reason) for t in tr] == [(0, 1, 11, 1, "time"), (11, 12, 22, 1, "time")]
    assert nxt == 22  # the second trade runs past the signal window
    tr2, _ = simulate_pair(o, o, o, o, f, f, z, w_a=1.0, w_b=1.0, sig_lo=12, sig_hi=30, free_from=nxt)
    assert tr2[0].signal == 22


def test_simulate_stops_and_truncation():
    n = 30
    oa, ca, ob, cb = flat(n), flat(n), flat(n), flat(n)
    ca[3] = 120.0  # long A? no: z > 0 -> short A, A rallies 20 % -> pnl -0.2 <= -0.15
    oa[4] = 121.0
    z = np.zeros(n)
    z[1] = 2.1
    z[2:] = 2.5
    tr, _ = simulate_pair(oa, ca, ob, cb, np.zeros(n), np.zeros(n), z, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=2)
    assert (tr[0].reason, tr[0].exit) == ("stop", 4)
    assert tr[0].price == pytest.approx(-0.21)
    tr, _ = simulate_pair(oa, ca, ob, cb, np.zeros(n), np.zeros(n), z, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=2, p=Params(stops=False))
    assert tr[0].reason == "time"
    # z-stop
    z2 = np.zeros(n)
    z2[1], z2[2], z2[3] = 2.1, 2.5, 4.2
    tr, _ = simulate_pair(flat(n), flat(n), flat(n), flat(n), np.zeros(n), np.zeros(n), z2, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=2)
    assert (tr[0].reason, tr[0].exit) == ("stop", 4)
    # truncation: B delisted after day 5 -> closed at its last close
    ob2, cb2 = flat(n), flat(n)
    cb2[5] = 90.0
    ob2[6:] = NAN
    cb2[6:] = NAN
    t = close_trade(
        flat(n), flat(n), ob2, cb2, np.zeros(n), np.zeros(n), w_a=1.0, w_b=1.0, signal=1, entry=2, exit=12, side=1, reason="time"
    )
    assert t.truncated and t.price == pytest.approx(0.10)  # short B from 100 to 90


def test_simulate_end_of_data():
    n = 8
    z = np.full(n, 3.0)
    z[4:] = 3.5
    o = flat(n)
    tr, _ = simulate_pair(o, o, o, o, np.zeros(n), np.zeros(n), z, w_a=1.0, w_b=1.0, sig_lo=0, sig_hi=3)
    assert tr[0].reason == "end" and tr[0].exit == n


def test_causality_trades_before_t_unchanged():
    rng = np.random.default_rng(11)
    n, t_cut = 400, 250
    pa = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    pb = pa * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    fa, fb = rng.normal(0, 1e-4, n), rng.normal(0, 1e-4, n)

    def run(pa, pb, fa, fb):
        z = rolling_z(np.log(pa) - np.log(pb))
        return z, simulate_pair(pa, pa, pb, pb, fa, fb, z, w_a=0.8, w_b=1.2, sig_lo=60, sig_hi=n)[0]

    z1, t1 = run(pa, pb, fa, fb)
    pa2, pb2, fa2 = pa.copy(), pb.copy(), fa.copy()
    pa2[t_cut:] *= 1.7
    pb2[t_cut:] *= 0.4
    fa2[t_cut:] = 0.01
    z2, t2 = run(pa2, pb2, fa2, fb)
    assert np.array_equal(np.nan_to_num(z1[:t_cut], nan=9), np.nan_to_num(z2[:t_cut], nan=9))
    before = [t for t in t1 if t.exit < t_cut]
    assert before and before == [t for t in t2 if t.exit < t_cut]


def test_formation_ignores_rows_outside_window():
    rng = np.random.default_rng(5)
    x = rng.normal(0, 0.03, (300, 4))
    x[:, 1] += x[:, 0]
    m = rng.normal(0, 0.03, 300)
    f1 = form_pairs(x[:180], m[:180], rho_min=0.3)
    y = x.copy()
    y[180:] = rng.normal(0, 0.5, (120, 4))
    f2 = form_pairs(y[:180], m[:180], rho_min=0.3)
    assert f1.pairs == f2.pairs


def test_runner_formation_window_ignores_rows_from_f():
    """The runner's admission rule and formation window read rows before F only (closes, volume, returns)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import verify_sector_pair_spread as r

    rng = np.random.default_rng(11)
    n, k, F = 300, 6, 250
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, (n, k)), axis=0))
    qv = np.full((n, k), 50e6)
    qv[:, 4] = 5e6  # below the 10 M USD median volume floor
    c[F - 1, 5] = np.nan  # no bar on F-1
    R = r.log_returns(c)
    cols = r.admitted(c, qv, R, F, btc=0)
    assert list(cols) == [1, 2, 3]  # BTC (0), thin (4) and no-bar (5) excluded

    c2, qv2 = c.copy(), qv.copy()
    c2[F:] = rng.uniform(1, 1e4, (n - F, k))
    qv2[F:] = 0.0
    qv2[F:, 4] = 1e12
    R2 = r.log_returns(c2)
    assert list(r.admitted(c2, qv2, R2, F, btc=0)) == list(cols)
    win = slice(F - r.FORM_DAYS, F)
    assert np.array_equal(R[win], R2[win], equal_nan=True)
    f1 = form_pairs(R[win][:, cols], R[win, 0], rho_min=0.0)
    f2 = form_pairs(R2[win][:, cols], R2[win, 0], rho_min=0.0)
    assert f1.pairs == f2.pairs


def test_cluster_se_and_bootstrap():
    v = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert cluster_se(v, list(range(5))) == pytest.approx(v.std(ddof=1) / math.sqrt(5))
    # one cluster holding identical copies doubles nothing: two clusters of duplicates
    se_dup = cluster_se([1.0, 1.0, 3.0, 3.0], ["a", "a", "b", "b"])
    assert se_dup == pytest.approx(math.sqrt(2 * ((2 * -1.0) ** 2 + (2 * 1.0) ** 2)) / 4)
    assert cluster_bootstrap_ci([0.5] * 6, [1, 1, 2, 2, 3, 3]) == pytest.approx((0.5, 0.5))
    lo, hi = cluster_bootstrap_ci(np.random.default_rng(0).normal(0.01, 0.05, 300), list(range(300)))
    assert lo < 0.01 < hi
