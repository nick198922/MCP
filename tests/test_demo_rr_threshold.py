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


def _ms(*args):
    from datetime import UTC, datetime

    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


def test_week_key_monday_start():
    assert d.week_key(_ms(1970, 1, 5)) == 1  # first Monday
    assert d.week_key(_ms(1970, 1, 4, 23, 59)) == 0
    mon = _ms(2026, 11, 16)  # Monday
    assert d.week_key(mon) == d.week_key(_ms(2026, 11, 22, 23, 59))  # same ISO week through Sunday
    assert d.week_key(mon - 1) == d.week_key(mon) - 1


def test_webb_weights_mean_zero_variance_one():
    assert math.fsum(d.WEBB) == pytest.approx(0.0, abs=1e-12)
    assert math.fsum(w * w for w in d.WEBB) / 6 == pytest.approx(1.0)


def test_wild_cluster_se_matches_cr1_and_ci_contains_estimate():
    from tradedesk.analytics.oi_flush import cluster_se_diff, cluster_se_mean

    vals = [1.0, -1.0, 0.5, 2.0, -0.3, 0.8, 1.2, -0.7, 0.1, 0.9]
    cl = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    out = d.wild_cluster(vals, cl, n_boot=2000, seed=3)
    assert out["est"] == pytest.approx(sum(vals) / len(vals))
    assert out["se"] == pytest.approx(cluster_se_mean(vals, cl))
    assert out["lo"] < out["est"] < out["hi"]
    b, cb = [0.2, -0.4, 0.0, 0.6], [0, 1, 2, 5]
    o2 = d.wild_cluster(vals, cl, b, cb, n_boot=2000, seed=3)
    assert o2["est"] == pytest.approx(sum(vals) / 10 - sum(b) / 4)
    assert o2["se"] == pytest.approx(cluster_se_diff(vals, cl, b, cb))
    assert o2["g"] == 6
    assert o2["lo"] < o2["est"] < o2["hi"]


def test_wild_cluster_zero_mean_gives_p_one_and_strong_effect_small_p():
    # cluster means alternate +0.5 / -0.5: the estimate is exactly 0, so |t*| >= |t| = 0 for every draw
    vals, cl = [], []
    for g in range(10):
        m = 0.5 if g % 2 == 0 else -0.5
        vals += [m - 0.1, m + 0.1]
        cl += [g, g]
    out = d.wild_cluster(vals, cl, n_boot=2000, seed=1)
    assert out["est"] == pytest.approx(0.0, abs=1e-12)
    assert out["p_two"] == pytest.approx(1.0)
    # every cluster clearly positive: the null is rejected and the interval sits above zero
    pos = [1.0 + 0.1 * ((i * 7) % 5 - 2) for i in range(30)]
    cpos = [i // 3 for i in range(30)]
    o = d.wild_cluster(pos, cpos, n_boot=4000, seed=2)
    assert o["p_gt"] < 0.01
    assert o["p_lt"] > 0.99
    assert o["lo"] > 0
    # determinism with a seed
    assert d.wild_cluster(pos, cpos, n_boot=500, seed=9) == d.wild_cluster(pos, cpos, n_boot=500, seed=9)


def test_wild_cluster_needs_two_clusters():
    out = d.wild_cluster([1.0, 2.0], ["a", "a"])
    assert out["lo"] is None and out["p_two"] is None
    assert d.wild_cluster([], [])["est"] is None


def test_wild_cluster_before_t_ignores_later_rows():
    rows = [_row(f"s{i}", i * DAY // 2, r=(-1.0 if i % 3 else 1.2)) for i in range(20)]
    t = 6 * DAY

    def run(rs):
        first = d.first_decisions(rs, now_ms=t)
        return d.wild_cluster([x["r"] for x in first], [d.day_key(x["decided_ts"]) for x in first], n_boot=500, seed=4)

    base = run(rows)
    changed = [dict(r, r=5.0) if r["decided_ts"] > t else r for r in rows] + [_row("late", 9 * DAY, r=-3.0)]
    assert run(changed) == base


@pytest.mark.parametrize(
    ("widest", "wild", "h2", "new", "mde", "verdict"),
    [
        ({"lo": 0.05, "hi": 0.4}, {"lo": 0.02, "hi": 0.5}, 0.1, 0.2, 0.15, "✓"),
        ({"lo": 0.05, "hi": 0.4}, {"lo": 0.02, "hi": 0.5}, -0.1, 0.2, 0.15, "◯"),
        ({"lo": -0.4, "hi": -0.05}, {"lo": -0.5, "hi": -0.01}, 0.0, -0.2, 0.15, "✗"),
        ({"lo": -0.4, "hi": -0.05}, {"lo": -0.5, "hi": 0.01}, 0.0, -0.2, 0.15, "≈"),
        ({"lo": -0.3, "hi": 0.1}, {"lo": -0.3, "hi": 0.1}, 0.0, 0.0, 0.25, "◯"),
    ],
)
def test_decision_rule(widest, wild, h2, new, mde, verdict):
    assert d.decision(widest, wild, h2, new, mde)["verdict"] == verdict


def test_verify_script_runs_on_synthetic_file(tmp_path, capsys):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_demo_rr_threshold.py"
    spec = importlib.util.spec_from_file_location("verify_demo_rr_threshold", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t0 = _ms(2026, 10, 21)
    rows = []
    for i in range(60):
        r = 1.0 if i % 3 == 0 else -1.0
        base = {
            "decided_ts": t0 + i * DAY // 3,
            "r": r,
            "r_plan": r - 0.01,
            "symbol": f"C{i % 9}/USDT:USDT",
            "side": "short",
            "timeframe": "4h",
        }
        rows.append(
            {
                **base,
                "group": "refused",
                "reason": d.RR_REASON,
                "link_id": f"x{i}",
                "status": "open" if i == 59 else "resolved",
                "outcome": "target" if r > 0 else "stop",
                "example": "reward/risk 1.19 after fees < 1.5",
                "setup": "S3",
            }
        )
        if i % 4 == 0:
            rows.append({**base, "group": "taken", "link_id": f"t{i}", "status": "resolved", "outcome": "stop", "r": -1.0, "setup": "S3"})
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"schema": 2, "generated_ms": t0 + 25 * DAY, "not_executed": {"rows": rows}}))
    assert mod.main(["--in", str(f), "--cache-dir", str(tmp_path / "c"), "--smoke"]) == 0
    assert "no outcome metric" in capsys.readouterr().out
    assert mod.main(["--in", str(f), "--cache-dir", str(tmp_path / "c"), "--new-after-ms", str(t0 + 10 * DAY)]) == 0
    out = json.loads((tmp_path / "c" / "result.json").read_text())
    assert out["decision"]["verdict"] in {"✓", "≈", "✗", "◯"}
    assert out["robustness"]["h1_wild_day"]["g"] >= 2
    assert out["new_data"]["iid"]["n"] > 0


def test_decision_h1b_when_powered():
    out = d.decision({"lo": -0.25, "hi": 0.15}, {"lo": -0.25, "hi": 0.15}, 0.0, 0.0, 0.18)
    assert out["verdict"] == "◯" and out["h1b"] and "0.2" in out["why"]
