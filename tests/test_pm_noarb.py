"""Static no-arbitrage pairs of Polymarket price markets (analytics/pm_noarb.py): matching, payoff floor, books, episodes."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from tradedesk.analytics import pm_noarb as nb
from tradedesk.analytics.pm_threshold import settle_ts
from tradedesk.analytics.pm_touch import day_window

D = date(2026, 11, 10)
ABOVE_END = settle_ts(D)
WS, WE = day_window(D)
WEEK_WS, WEEK_WE = day_window(D - timedelta(days=2))[0], day_window(D + timedelta(days=4))[1]
TS = WS + 3600


def mk(mid, kind, direction, strike, *, end=None, ws=None, std=1, h="h", ya=0.5, yu=100.0, na=0.5, nu=100.0, bt=TS, fr=0.07):
    if end is None:
        end = ABOVE_END if kind == "above" else WE
    if ws is None and kind == "touch":
        ws = WS
    return {
        "market_id": mid,
        "asset": "BTC",
        "kind": kind,
        "direction": direction,
        "strike": float(strike),
        "end_ts": end,
        "window_start": ws,
        "rules_standard": std,
        "rules_hash": f"{kind}{direction}{h}",
        "yes_ask": ya,
        "yes_ask_usd": yu,
        "no_ask": na,
        "no_ask_usd": nu,
        "book_ts": bt,
        "fee_rate": fr,
        "fee_exp": 1.0,
    }


def ladder_rows():
    return [
        mk("a90", "above", "above", 90),
        mk("a92", "above", "above", 92),
        mk("a94", "above", "above", 94),
        mk("a96x", "above", "above", 96, std=0),
        mk("u92", "touch", "up", 92),
        mk("u95", "touch", "up", 95),
        mk("d88", "touch", "down", 88),
        mk("d90", "touch", "down", 90),
        mk("d93", "touch", "down", 93),
        mk("wu94", "touch", "up", 94, ws=WEEK_WS, end=WEEK_WE),
    ]


def ids(pairs, fam):
    return sorted((p["a"], p["b"], p["adjacent"]) for p in pairs if p["family"] == fam)


def test_covers_daily_weekly_and_edges():
    assert nb.covers(WS, WE, ABOVE_END)
    assert nb.covers(WEEK_WS, WEEK_WE, ABOVE_END)
    assert not nb.covers(WS, WE, settle_ts(D + timedelta(days=1)))  # next day's above is outside the daily window
    assert not nb.covers(ABOVE_END - 30, WE, ABOVE_END)  # the window opens inside the settlement candle
    assert not nb.covers(WS, ABOVE_END + 59, ABOVE_END)
    assert not nb.covers(None, WE, ABOVE_END)


def test_match_pairs_families_and_orientation():
    p = nb.match_pairs(ladder_rows())
    assert ids(p, "A") == [
        (("a90", "yes"), ("a92", "no"), True),
        (("a90", "yes"), ("a94", "no"), False),
        (("a92", "yes"), ("a94", "no"), True),
    ]  # the non-standard 96 is left out before adjacency
    assert ids(p, "U") == [(("u92", "yes"), ("u95", "no"), True)]  # the weekly up 94 has another window: no ladder
    assert ids(p, "D") == [
        (("d90", "yes"), ("d88", "no"), True),
        (("d93", "yes"), ("d88", "no"), False),
        (("d93", "yes"), ("d90", "no"), True),
    ]
    assert ids(p, "TA") == [(("u92", "yes"), ("a92", "no"), None), (("wu94", "yes"), ("a94", "no"), None)]
    assert ids(p, "TA_near") == [(("u92", "yes"), ("a94", "no"), None)]  # u95 / wu94: no standard above strike above them
    assert ids(p, "TB") == [(("d90", "yes"), ("a90", "yes"), None)]
    assert ids(p, "TB_near") == [(("d93", "yes"), ("a92", "yes"), None)]  # d88 / d90: no standard above strike below
    assert {x["unit"] for x in p} == {D.isoformat()}


def test_match_is_strict_on_end_ts_rules_hash_and_window():
    rows = [mk("a90", "above", "above", 90), mk("a92", "above", "above", 92, end=ABOVE_END + 300)]
    assert ids(nb.match_pairs(rows), "A") == []  # 5 minutes apart: no guaranteed floor, not paired (card's +-10 min dropped)
    rows = [mk("a90", "above", "above", 90), mk("a92", "above", "above", 92, h="other")]
    assert ids(nb.match_pairs(rows), "A") == []
    rows = [mk("u92", "touch", "up", 92), mk("a92", "above", "above", 92, end=settle_ts(D + timedelta(days=1)))]
    assert ids(nb.match_pairs(rows), "TA") == []
    rows = [mk("u92", "touch", "up", 92, std=0), mk("a92", "above", "above", 92)]
    assert nb.match_pairs(rows) == []


def _resolve(row, closes, highs, lows, t0, candle_variant):
    """1 if YES of ``row`` wins on a minute path (index m = candle opening at t0 + 60 m)."""
    k = row["strike"]
    if row["kind"] == "above":
        m = (row["end_ts"] - t0) // 60 - (1 if candle_variant == "closing" else 0)
        return int(closes[m] > k)
    lo_m, hi_m = (row["window_start"] - t0) // 60, (row["end_ts"] - t0) // 60
    if row["direction"] == "up":
        return int(max(highs[lo_m:hi_m]) >= k)
    return int(min(lows[lo_m:hi_m]) <= k)


@pytest.mark.parametrize("variant", ["opening", "closing"])
def test_every_registered_pair_pays_at_least_one(variant):
    rows = ladder_rows()
    pairs = nb.match_pairs(rows)
    by = {r["market_id"]: r for r in rows}
    t0 = WEEK_WS
    n = (WEEK_WE - t0) // 60
    rng = random.Random(7)
    worst_wrong = 2
    for _ in range(40):
        p = 92.0 + rng.uniform(-3, 3)
        closes, highs, lows = [], [], []
        for _m in range(n):
            o = p
            p += rng.gauss(0, 0.08)
            closes.append(p)
            highs.append(max(o, p) + abs(rng.gauss(0, 0.03)))
            lows.append(min(o, p) - abs(rng.gauss(0, 0.03)))
        out = {mid: _resolve(r, closes, highs, lows, t0, variant) for mid, r in by.items()}
        for pr in pairs:
            pay = sum(out[m] if s == "yes" else 1 - out[m] for m, s in (pr["a"], pr["b"]))
            assert pay >= 1, (pr["id"], variant)
        worst_wrong = min(worst_wrong, out["d90"] + (1 - out["a90"]))  # YES(down) + NO(above): no floor
    assert worst_wrong == 0


def test_leg_book_and_pair_status():
    assert nb.leg_book(mk("x", "above", "above", 1), "yes", TS) == "ok"
    assert nb.leg_book(mk("x", "above", "above", 1, ya=None), "yes", TS) == "no_book"
    assert nb.leg_book(mk("x", "above", "above", 1, ya=1.0), "yes", TS) == "no_book"
    assert nb.leg_book(mk("x", "above", "above", 1, nu=0.0), "no", TS) == "no_book"
    assert nb.leg_book(mk("x", "above", "above", 1, bt=None), "yes", TS) == "stale"
    assert nb.leg_book(mk("x", "above", "above", 1, bt=TS - 121), "yes", TS) == "stale"

    def status(**b):
        rows = [mk("a90", "above", "above", 90, **b), mk("a92", "above", "above", 92)]
        return [x["status"] for x in nb.snapshot_pairs(rows, TS)]

    assert status() == ["ok"]
    assert status(yu=49.99) == ["shallow"]
    assert status(nu=1.0) == ["ok"]  # only the side bought (YES of the lower strike) counts
    assert status(bt=TS - 61) == ["skew"]
    assert status(ya=None) == ["no_book"]


def test_cost_fee_and_threshold():
    rows = [mk("a90", "above", "above", 90, ya=0.4), mk("a92", "above", "above", 92, na=0.5, fr=0.01)]
    by = {r["market_id"]: r for r in rows}
    (pr,) = nb.match_pairs(rows)
    assert nb.leg_cost(by["a90"], "yes") == pytest.approx(0.4 + 0.07 * 0.4 * 0.6)
    assert nb.pair_cost(pr, by) == pytest.approx(0.4 + 0.0168 + 0.5 + 0.0025)
    assert nb.is_violation(0.995) and nb.is_violation(0.9)
    assert not nb.is_violation(0.9951) and not nb.is_violation(1.0)


def test_package_equal_shares_notional_floor():
    rows = [mk("a90", "above", "above", 90, ya=0.9, yu=60.0), mk("a92", "above", "above", 92, na=0.05, nu=500.0, fr=0.0)]
    by = {r["market_id"]: r for r in rows}
    (pr,) = nb.match_pairs(rows)
    pk = nb.package(pr, by)
    assert pk["shares"] == pytest.approx(60 / 0.9)
    assert pk["notional_usd"] == pytest.approx(60 / 0.9 * 0.95)
    assert pk["floor_gain_usd"] == pytest.approx(60 / 0.9 * (1 - nb.pair_cost(pr, by)))
    for a in (0.02, 0.3, 0.7, 0.98):  # both legs >= $50 at the best level => the equal-share package costs >= $50
        for b in (0.02, 0.5, 0.97):
            rows = [mk("a90", "above", "above", 90, ya=a, yu=50.0), mk("a92", "above", "above", 92, na=b, nu=50.0)]
            assert nb.package(nb.match_pairs(rows)[0], {r["market_id"]: r for r in rows})["notional_usd"] >= 50 - 1e-9


def test_episode_tracker():
    tr = nb.EpisodeTracker()
    tr.update(1, {"p": True, "q": True})
    tr.update(2, {"p": True, "q": False})
    tr.update(3, {"r": True})
    tr.update(4, {"r": True, "p": False})
    eps = {e["id"]: e for e in tr.close()}
    assert eps["p"] == {"id": "p", "first_ts": 1, "last_ts": 2, "n_snapshots": 2, "ended_by": "absent", "next_ts": 3}
    assert eps["q"]["ended_by"] == "held" and eps["q"]["n_snapshots"] == 1
    assert eps["r"]["ended_by"] == "cutoff" and eps["r"]["n_snapshots"] == 2


def test_h1_verdict_branches_are_disjoint_and_cover_everything():
    assert nb.h1_verdict(5, 10, 10) == ("confirmed", "5 independent dates with a counted violation >= 5")
    assert nb.h1_verdict(7, 35, 900)[0] == "confirmed"
    assert nb.h1_verdict(0, 35, 300) == ("rejected", "kill: zero counted violations over the adequate sample")
    assert nb.h1_verdict(0, 29, 5000)[0] == "inconclusive"
    assert nb.h1_verdict(0, 35, 299)[0] == "inconclusive"
    assert nb.h1_verdict(4, 29, 5000)[0] == "inconclusive"
    assert nb.h1_verdict(1, 35, 900) == ("rejected", "H1 not met: 1 dates < 5 (rare violations, not a kill)")
    assert nb.h1_verdict(4, 35, 900) == ("rejected", "H1 not met: 4 dates < 5 (rare violations, not a kill)")
    seen = {}
    for n in range(0, 8):
        for days, pairs in ((29, 299), (29, 300), (30, 299), (30, 300)):
            v, why = nb.h1_verdict(n, days, pairs)
            branch = "confirmed" if n >= 5 else ("inconclusive" if (days < 30 or pairs < 300) else ("kill" if n == 0 else "not_met"))
            assert (v, branch) in {
                ("confirmed", "confirmed"),
                ("inconclusive", "inconclusive"),
                ("rejected", "kill"),
                ("rejected", "not_met"),
            }
            assert why.startswith("kill:") == (branch == "kill")  # "kill" is only zero counted violations
            seen[branch] = True
    assert set(seen) == {"confirmed", "inconclusive", "kill", "not_met"}
    with pytest.raises(ValueError):
        nb.h1_verdict(-1, 35, 900)


def test_crossed_book():
    assert nb.crossed(mk("x", "above", "above", 1, ya=0.4, na=0.59))
    assert not nb.crossed(mk("x", "above", "above", 1, ya=0.4, na=0.6))  # sums to exactly 1: not crossed
    assert not nb.crossed(mk("x", "above", "above", 1, ya=0.4, na=None))  # cannot tell


H0 = 1_000_000


def vrec(pid, ts, *, dt=0, crossed=False, fam="A", unit="2026-11-10"):
    return {"id": pid, "ts": ts, "book_ts": (ts - 10, ts - 10 + dt), "crossed": crossed, "family": fam, "unit": unit}


def test_check_ts_window():
    all_ts = [H0, H0 + 42, H0 + 1799, H0 + 3600, H0 + 9001]
    assert nb.check_ts(H0, all_ts) == H0 + 3600  # the 42 s and the 29:59 snapshots are skipped
    assert nb.check_ts(H0 + 3600, all_ts) is None  # the next one >= 30 min later is 5401 s later: no check snapshot
    assert nb.check_ts(H0 + 3600, [H0 + 3600, H0 + 9000]) == H0 + 9000  # exactly 90 min: still a check
    assert nb.check_ts(H0, [H0, H0 + 1800]) == H0 + 1800  # exactly 30 min: a check
    assert nb.check_ts(H0 + 9001, all_ts) is None  # end of the sample


def test_classify_rules_each_one_matters():
    all_ts = [H0, H0 + 42, H0 + 3600, H0 + 7200, H0 + 7242, H0 + 10800, H0 + 30000]
    v = [
        vrec("crossed_sync", H0, dt=0, crossed=True),  # rule (a) wins over (b)
        vrec("sync5", H0, dt=5),
        vrec("skew6", H0, dt=-6),  # no later record: skew-suspect
        vrec("surv", H0, dt=30),
        vrec("surv", H0 + 3600, dt=30),  # its own check (7200) has no record -> skew-suspect
        vrec("only42", H0 + 7200, dt=30),
        vrec("only42", H0 + 7242, dt=30),  # 42 s later does not count as surviving (check = 10800)
        vrec("nextcross", H0, dt=30),
        vrec("nextcross", H0 + 3600, dt=0, crossed=True),  # the check snapshot's violation is crossed: no survival
        vrec("gap", H0 + 10800, dt=30),
        vrec("gap", H0 + 30000, dt=30),  # next snapshot > 90 min later: no check snapshot
    ]
    got = [(c["id"], c["ts"] - H0, c["h1_class"]) for c in nb.classify_violations(v, all_ts)]
    assert got == [
        ("crossed_sync", 0, "crossed"),
        ("sync5", 0, "sync"),
        ("skew6", 0, "skew_suspect"),
        ("surv", 0, "survived"),
        ("surv", 3600, "skew_suspect"),
        ("only42", 7200, "skew_suspect"),
        ("only42", 7242, "skew_suspect"),
        ("nextcross", 0, "skew_suspect"),
        ("nextcross", 3600, "crossed"),
        ("gap", 10800, "skew_suspect"),
        ("gap", 30000, "skew_suspect"),
    ]


def test_violation_dates_lines():
    v = nb.classify_violations(
        [
            vrec("a", H0, fam="A", unit="d1"),
            vrec("u", H0, fam="U", unit="d2"),
            vrec("ta", H0, fam="TA", unit="d3"),
            vrec("tb", H0, fam="TB", unit="d4", crossed=True),
            vrec("tn", H0, fam="TA_near", unit="d5"),
            vrec("d", H0, fam="D", unit="d6", dt=20),
        ],
        [H0],
    )
    assert nb.violation_dates(v) == ["d1", "d2", "d3"]  # headline: primary, counted only
    assert nb.violation_dates(v, nb.LITERAL) == ["d1", "d3"]  # the card's literal A + TA line
    assert nb.violation_dates(v, nb.PRIMARY, nb.H1_CLASSES) == ["d1", "d2", "d3", "d4", "d6"]  # before the filters
    assert nb.PRIMARY == ("A", "U", "D", "TA", "TB") and nb.LITERAL == ("A", "TA")


def test_classification_does_not_depend_on_rows_after_the_check_snapshot():
    all_ts = [H0, H0 + 3600, H0 + 7200]
    base = [vrec("p", H0, dt=30), vrec("p", H0 + 3600, dt=30)]
    a = nb.classify_violations(base, all_ts)[:1]
    b = nb.classify_violations([*base, vrec("p", H0 + 7200, dt=30), vrec("q", H0 + 7200)], all_ts)[:1]
    assert a == b and a[0]["h1_class"] == "survived"


def test_lower_fee_model():
    row = mk("a90", "above", "above", 90, ya=0.5, fr=0.07)
    assert nb.leg_cost(row, "yes") == pytest.approx(0.5 + 0.07 * 0.25)
    assert nb.leg_cost(row, "yes", nb.FEE_LOWER) == pytest.approx(0.5 + 0.5 * 0.07 * 0.25)
    with pytest.raises(ValueError):
        nb.leg_cost(row, "yes", "other")


def test_match_without_the_rules_filter_counts_non_standard_rows():
    rows = ladder_rows()
    assert len(ids(nb.match_pairs(rows), "A")) == 3
    assert len(ids(nb.match_pairs(rows, standard_only=False), "A")) == 6  # + 90/96, 92/96, 94/96


def test_weekly_ladder_unit_is_the_last_window_day():
    rows = [*ladder_rows(), mk("wu96", "touch", "up", 96, ws=WEEK_WS, end=WEEK_WE)]
    p = nb.match_pairs(rows)
    (u,) = [x for x in p if x["family"] == "U" and x["a"][0] == "wu94"]
    assert u["unit"] == (D + timedelta(days=4)).isoformat()
    (ta,) = [x for x in p if x["family"] == "TA" and x["a"][0] == "wu94"]
    assert ta["unit"] == D.isoformat()  # a cross pair settles with its above leg


@pytest.mark.parametrize("variant", ["opening", "closing"])
@pytest.mark.parametrize("level", [88.0, 90.0, 92.0, 93.0, 94.0, 95.0])
def test_floor_holds_when_the_price_sits_exactly_on_a_strike(variant, level):
    """Flat path at a strike: close = High = Low = K (above is strict, touch is not); A, TA and TB must still pay >= 1."""
    rows = [*ladder_rows(), mk("d92", "touch", "down", 92)]
    pairs = nb.match_pairs(rows)
    assert {"A", "TA", "TB", "U", "D"} <= {p["family"] for p in pairs}
    n = (WEEK_WE - WEEK_WS) // 60
    flat = [level] * n
    out = {r["market_id"]: _resolve(r, flat, flat, flat, WEEK_WS, variant) for r in rows}
    for pr in pairs:
        pay = sum(out[m] if s == "yes" else 1 - out[m] for m, s in (pr["a"], pr["b"]))
        assert pay >= 1, (pr["id"], level, variant)
    if level == 92.0:  # TB is tight at equality: YES(down 92) wins, YES(above 92) loses
        assert out["d92"] == 1 and out["a92"] == 0 and out["u92"] == 1


def test_earlier_snapshots_do_not_depend_on_later_rows():
    snaps = [
        (TS + 3600 * i, [mk("a90", "above", "above", 90, bt=TS + 3600 * i), mk("a92", "above", "above", 92, bt=TS + 3600 * i)])
        for i in range(4)
    ]
    before = [nb.snapshot_pairs(r, t) for t, r in snaps[:2]]
    snaps[3][1][0]["yes_ask"] = None
    snaps[2][1].append(mk("a94", "above", "above", 94, bt=TS + 7200))
    assert [nb.snapshot_pairs(r, t) for t, r in snaps[:2]] == before
    tr_a, tr_b = nb.EpisodeTracker(), nb.EpisodeTracker()
    for t in (1, 2, 3):
        tr_a.update(t, {"p": t < 3})
        tr_b.update(t, {"p": t < 3})
    tr_b.update(4, {"p": True})  # a later violation does not rewrite the closed episode
    assert tr_a.close()[0] == tr_b.close()[0]
