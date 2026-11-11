import csv
import json
import math
from pathlib import Path

import pytest

from tradedesk.analytics.pm_sharp_clv import (
    BANDS,
    bridge_delta,
    cluster_se,
    drop_top_clusters,
    kickoff_utc,
    norm_team,
    p_group,
    quote_at,
    resolve_teams,
    sharp_probs,
    taker_net,
    team_sim,
)

FIX = Path(__file__).parent / "fixtures"


def test_sharp_probs_proportional_analytic():
    # 1/2 + 1/4 + 1/4 = 1 -> no margin, probabilities are the inverse odds
    assert sharp_probs([2.0, 4.0, 4.0]) == pytest.approx((0.5, 0.25, 0.25))
    # 5 % margin spread proportionally: 1/1.9 + 1/3.8 + 1/3.8 = 1.0526...
    p = sharp_probs([1.9, 3.8, 3.8])
    assert p == pytest.approx((0.5, 0.25, 0.25))
    assert sum(p) == pytest.approx(1.0)


def test_sharp_probs_power_shrinks_longshot_more():
    odds = [1.30, 5.5, 11.0]
    prop, power = sharp_probs(odds), sharp_probs(odds, "power")
    assert sum(power) == pytest.approx(1.0, abs=1e-9)
    assert power[2] < prop[2]  # long shot gets less probability under the power method
    assert power[0] > prop[0]


@pytest.mark.parametrize("bad", [[2.0, 4.0], [2.0, None, 4.0], [2.0, 1.0, 4.0], [2.0, "x", 4.0]])
def test_sharp_probs_rejects_bad_odds(bad):
    assert sharp_probs(bad) is None


def test_sharp_probs_unknown_method():
    with pytest.raises(ValueError):
        sharp_probs([2.0, 4.0, 4.0], "shin")


def test_p_group_boundaries():
    assert [p_group(x) for x in (0.0, 0.15, 0.150001, 0.39999, 0.40, 0.59999, 0.60, 1.0)] == [
        BANDS[0], BANDS[0], BANDS[1], BANDS[1], BANDS[2], BANDS[2], BANDS[3], BANDS[3]
    ]  # fmt: skip


def test_quote_at_last_point_strictly_before_and_freshness():
    pts = [{"t": 100, "p": 0.40}, {"t": 160, "p": 0.42}, {"t": 200, "p": 0.99}]
    assert quote_at(pts, 200, 120) == (0.42, 160)  # the point at t_ref itself is excluded
    assert quote_at(pts, 200, 30) is None  # 40 s old > 30 s
    assert quote_at([], 200, 120) is None
    assert quote_at([{"t": "bad"}, {"p": 0.3}, {"t": 150, "p": 1.7}], 200, 120) is None


def test_quote_at_ignores_everything_from_t_ref_on():
    base = [{"t": 1000 + 60 * i, "p": 0.30 + 0.001 * i} for i in range(10)]
    t_ref = 1000 + 60 * 6
    before = quote_at(base, t_ref, 120)
    later = [{"t": r["t"], "p": 0.9} if r["t"] >= t_ref else r for r in base] + [{"t": t_ref + 1, "p": 0.01}]
    assert quote_at(list(reversed(later)), t_ref, 120) == before


def test_bridge_delta_analytic():
    d = bridge_delta([(0.10, 0.12), (0.12, 0.13), (0.70, 0.66), (0.50, 0.50)])
    assert d[BANDS[0]]["n"] == 2 and d[BANDS[0]]["mean"] == pytest.approx(0.015)
    assert d[BANDS[3]]["mean"] == pytest.approx(-0.04)
    assert d[BANDS[2]]["mean"] == pytest.approx(0.0)
    assert BANDS[1] not in d


def test_real_football_data_sample_pinnacle_margin():
    # 2026-11-12, football-data.co.uk mmz4281/2526/E0.csv, first rows (Pinnacle closing triple present)
    rows = list(csv.DictReader((FIX / "football_data_2526_E0_head.csv").open(encoding="utf-8-sig")))
    assert rows
    for r in rows:
        odds = [float(r[c]) for c in ("PSCH", "PSCD", "PSCA")]
        margin = sum(1 / o for o in odds) - 1
        assert 0.0 < margin < 0.06
        assert sum(sharp_probs(odds, "power")) == pytest.approx(1.0, abs=1e-9)


def test_real_gamma_soccer_event_is_three_yes_no_markets():
    # 2026-11-12, Gamma /events?series_id=10188&closed=true (EPL), trimmed
    ev = json.loads((FIX / "polymarket_gamma_epl_event_2526.json").read_text())
    titles = [m["groupItemTitle"] for m in ev["markets"]]
    assert len(titles) == 3 and sum("Draw" in t for t in titles) == 1
    for m in ev["markets"]:
        assert len(json.loads(m["clobTokenIds"])) == 2
        assert m["gameStartTime"].startswith(ev["startTime"][:10])


def test_norm_team_and_similarity():
    assert norm_team("1. FC Köln") == "koln"
    assert team_sim("Arsenal FC", "Arsenal") == 1.0
    # "city"/"united" are kept, so the two Manchester clubs stay apart
    assert team_sim("Manchester City", "Man United") < 0.75


def test_resolve_teams_overrides_ties_and_threshold():
    fd = ["Barcelona", "Espanol", "Man City", "Man United", "Ath Madrid", "Real Madrid"]
    # substring trap: without an override "RCD Espanyol de Barcelona" would map to "Barcelona"
    auto = resolve_teams(["RCD Espanyol de Barcelona"], fd, {})
    assert auto["RCD Espanyol de Barcelona"] == "Barcelona"
    m = resolve_teams(
        ["RCD Espanyol de Barcelona", "FC Barcelona", "Manchester City", "Unknown XI"],
        fd,
        {"RCD Espanyol de Barcelona": "Espanol", "Manchester City": "Man City"},
    )
    assert m == {"RCD Espanyol de Barcelona": "Espanol", "FC Barcelona": "Barcelona", "Manchester City": "Man City", "Unknown XI": None}
    # an override to a name not in the football-data list is rejected rather than trusted
    assert resolve_teams(["X"], fd, {"X": "Nowhere"}) == {"X": None}
    # a tie between two candidates gives None ("Madrid" is contained in both)
    assert resolve_teams(["Madrid"], ["Ath Madrid", "Real Madrid"], {}) == {"Madrid": None}


def test_kickoff_utc_bst_and_gmt():
    # 15/08/2025 20:00 UK summer time (BST, UTC+1) = 19:00 UTC
    assert kickoff_utc("15/08/2025", "20:00") == 1755284400
    # 10/01/2026 15:00 UK winter time (GMT) = 15:00 UTC; two-digit year accepted
    assert kickoff_utc("10/01/26", "15:00") == 1768057200
    assert kickoff_utc("10/01/2026", "") is None


def test_taker_net_analytic():
    # registered form |g| - 0.05 p (1 - p): g = -0.02, p = 0.10 -> 0.02 - 0.0045
    assert taker_net(-0.02, 0.10) == pytest.approx(0.0155)
    # directional: the pre-set side is "sell" (+1), so a negative g is a loss before the fee
    assert taker_net(-0.02, 0.10, direction=1) == pytest.approx(-0.0245)
    assert taker_net(-0.02, 0.70, direction=-1) == pytest.approx(0.02 - 0.0105)


def test_cluster_se_analytic():
    # one observation per cluster: CR1 = sample SD / sqrt(n)
    v = [0.0, 1.0, 2.0, 3.0]
    assert cluster_se(v, ["a", "b", "c", "d"]) == pytest.approx((5 / 3) ** 0.5 / 2)
    # two clusters of identical pairs: residuals -0.5 each, cluster sums (-1, +1) -> sqrt((1 + 1) * 2 / 1) / 4 = 0.5
    assert cluster_se([0.0, 0.0, 1.0, 1.0], ["a", "a", "b", "b"]) == pytest.approx(0.5)
    assert math.isnan(cluster_se([1.0, 2.0], ["a", "a"]))


def test_drop_top_clusters_removes_largest_contributions():
    v = [5.0, 5.0, 0.1, -0.2, 0.3, -3.0]
    c = ["big", "big", "x", "y", "z", "neg"]
    kv, kc = drop_top_clusters(v, c, 0.2)  # ceil(0.2 * 5) = 1 cluster: "big" (|sum| 10)
    assert "big" not in kc and len(kv) == 4
    kv, kc = drop_top_clusters(v, c, 0.4)  # two clusters: "big" and "neg"
    assert set(kc) == {"x", "y", "z"}
    assert drop_top_clusters([], [], 0.05) == ([], [])


def test_g_at_kickoff_ignores_points_after_reference():
    # no look-ahead: points at or after T (the in-play jump) do not change p_PM(T) or g
    T = 1_000_000
    pre = [{"t": T - 600, "p": 0.10}, {"t": T - 60, "p": 0.12}]
    post = [{"t": T, "p": 0.90}, {"t": T + 300, "p": 0.99}]
    p_sharp = 0.10
    g1 = quote_at(pre, T, 120)[0] - p_sharp
    g2 = quote_at(pre + post, T, 120)[0] - p_sharp
    assert g1 == g2 == pytest.approx(0.02)
