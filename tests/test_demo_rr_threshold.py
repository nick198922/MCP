import math

import pytest

from tradedesk.analytics import demo_rr_threshold as d

DAY = d.DAY_MS


def _row(link, ts, r=None, symbol="ETH/USDT:USDT", side="short", tf="4h", status="resolved"):
    return {"link_id": link, "decided_ts": ts, "r": r, "symbol": symbol, "side": side, "timeframe": tf, "status": status}


def test_rr_from_message_and_bucket():
    assert d.rr_from_message("reward/risk 1.19 after fees < 1.5") == pytest.approx(1.19)
    assert d.rr_from_message("score 1 < 2") is None
    assert d.rr_from_message(None) is None
    assert d.rr_bucket(0.7) == "< 1.0"
    assert d.rr_bucket(1.0) == "1.0–1.2"
    assert d.rr_bucket(1.2) == "1.2–1.5"
    assert d.rr_bucket(1.49) == "1.2–1.5"
    assert d.rr_bucket(1.5) == "≥ 1.5"
    assert d.rr_bucket(None) == "unknown"


def test_first_decisions_keeps_earliest_per_signal_and_ignores_future():
    rows = [_row("a", 300, 1.0), _row("a", 100, -1.0), _row("b", 200, 0.5), _row(None, 50, 0.2), _row("c", 900, 2.0)]
    out = d.first_decisions(rows, now_ms=500)
    assert [(r["link_id"], r["decided_ts"]) for r in out] == [(None, 50), ("a", 100), ("b", 200)]


def test_results_before_t_do_not_change_with_later_rows():
    base = [_row("a", 100, -1.0), _row("b", 200, 1.2)]
    later = [_row("a", 600, 5.0), _row("z", 700, 9.0)]
    assert d.first_decisions(base, now_ms=500) == d.first_decisions(base + later, now_ms=500)


def test_mean_ci_iid_matches_t_interval():
    vals = [1.0, -1.0, 1.0, -1.0]
    ci = d.mean_ci(vals)
    se = statistics_stdev(vals) / 2
    assert ci["mean"] == pytest.approx(0.0)
    assert ci["se"] == pytest.approx(se)
    assert ci["hi"] == pytest.approx(3.182446305 * se, rel=1e-3)


def statistics_stdev(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def test_mean_ci_cluster_duplicates_do_not_shrink_se():
    # each value duplicated within its own cluster: the cluster SE equals the iid SE of the distinct values (CR1)
    vals = [1.0, -1.0, 0.5]
    dup = [v for v in vals for _ in range(3)]
    cl = [i for i in range(3) for _ in range(3)]
    se_c = d.mean_ci(dup, cl)["se"]
    m = sum(vals) / 3
    cr1 = math.sqrt(sum(((v - m) / 3) ** 2 for v in vals) * 3 / 2)
    assert se_c == pytest.approx(cr1)
    assert d.mean_ci(dup, cl)["g"] == 3
    assert d.mean_ci([1.0, 2.0], [0, 0])["se"] is None


def test_diff_ci_independent_clusters_adds_variances():
    a, ca = [1.0, 0.0, 2.0], ["x", "y", "z"]
    b, cb = [0.0, 1.0], ["u", "v"]
    out = d.diff_ci(a, ca, b, cb)
    assert out["diff"] == pytest.approx(1.0 - 0.5)
    sa = d.mean_ci(a, ca)["se"] ** 2 * 2 / 3  # back to CR0 of a
    sb = d.mean_ci(b, cb)["se"] ** 2 * 1 / 2
    assert out["se"] == pytest.approx(math.sqrt((sa + sb) * 5 / 4))


def test_widest_mde_n_needed():
    cis = [{"lo": -1.0, "hi": 1.0}, {"lo": -2.0, "hi": 1.0}, {"lo": None, "hi": None}]
    assert d.widest(cis) == {"lo": -2.0, "hi": 1.0}
    assert d.mde(0.1) == pytest.approx(0.28)
    # se 0.1 at n=100 -> sd 1.0; effect 0.28 -> n = (2.8/0.28)^2 = 100
    assert d.n_needed(0.1, 100, 0.28) == 100
    assert d.n_needed(None, 10, 0.1) is None


def test_open_bounds_and_without_top():
    b = d.open_bounds([1.0, -1.0], [1.3, None])
    assert b["worst"] == pytest.approx(-0.5)
    assert b["best"] == pytest.approx((1.0 - 1.0 + 1.3 + 1.0) / 4)
    assert d.without_top([3.0, 1.0, -1.0], 1) == pytest.approx(0.0)
    assert d.without_top([1.0], 3) is None


def test_between_ss_and_permutation_p():
    vals = [1.0, 1.0, -1.0, -1.0]
    assert d.between_ss(vals, ["a", "a", "b", "b"]) == pytest.approx(4.0)
    assert d.between_ss(vals, ["a", "b", "a", "b"]) == pytest.approx(0.0)
    # perfectly separated labels: only 2 of the 6 distinct label arrangements reach the observed spread
    p = d.label_permutation_p(vals, ["a", "a", "b", "b"], n_perm=3000, seed=1)
    assert p == pytest.approx(1 / 3, abs=0.03)
    assert d.label_permutation_p(vals, ["a"] * 4) is None


def test_overlap_share_same_symbol_side_within_hold():
    rows = [
        _row("a", 0),
        _row("b", 2 * DAY),  # within 7 days of a
        _row("c", 20 * DAY),  # alone
        _row("d", DAY, side="long"),  # other side
    ]
    out = d.overlap_share(rows, {"4h": 7 * DAY})
    assert out["overlapping"] == 2
    assert out["share"] == pytest.approx(0.5)
    assert out["keys"] == 2
