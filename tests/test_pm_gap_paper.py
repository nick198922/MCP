"""Pre-registered paper rule and metrics for the Polymarket <-> Deribit gap: analytic references and causality."""

from __future__ import annotations

import math
from datetime import date

import pytest

from tradedesk.analytics.options import d1d2, ncdf
from tradedesk.analytics.pm_gap_paper import (
    MODEL_V2G,
    VOL_HAIRCUT_K,
    YEAR_S,
    EntryRule,
    accepted_rule_hashes,
    asset_permutation_p,
    binance_gap,
    brier_by_market,
    closing_value,
    cluster_mean_ci,
    delayed_entry,
    eligible,
    entry_signals,
    et_week,
    g_split,
    horizon_clv,
    mde,
    model_prob,
    open_positions,
    position_metrics,
    power_gate,
    power_stage0,
    resolution_day,
    resolution_week,
    rules_accepted,
    side_book,
    snapshot_gaps,
    verdict,
    vol_scaled_prob,
    with_snapshot_g,
    worse_entry,
)
from tradedesk.analytics.pm_threshold import settle_ts
from tradedesk.analytics.pm_touch import day_window
from tradedesk.analytics.probability import prob_touch

H = 3600
END = settle_ts(date(2026, 10, 29))  # 12:00 ET
OLD_TEXT = (
    'This market will resolve to "Yes" if the Binance 1 minute candle for BTC/USDT 12:00 in the ET timezone (noon) on the date '
    'specified in the title has a final "Close" price higher than the price specified in the title.'
)
NEW_TEXT = OLD_TEXT.replace("for BTC/USDT 12:00", "for BTC/USDT closing at 12:00")
ACC = {"h1"}


def row(ts: int, **kw: object) -> dict:
    base = {
        "ts": ts,
        "market_id": "m1",
        "asset": "BTC",
        "kind": "above",
        "direction": "above",
        "strike": 70_000.0,
        "end_ts": END,
        "yes_bid": 0.48,
        "yes_ask": 0.50,
        "yes_ask_usd": 500.0,
        "no_bid": 0.50,
        "no_ask": 0.52,
        "no_ask_usd": 500.0,
        "fee_rate": 0.0,
        "fee_exp": 1.0,
        "rules_hash": "h1",
        "rules_standard": 1,
        "model_prob": 0.60,
        "model_note": "ok",
        "model_iv": 0.50,
        "model_covered": 1,
        "spot": 70_000.0,
    }
    base.update(kw)
    return base


def test_rules_accepted_both_wordings_and_not_others():
    assert rules_accepted(OLD_TEXT, "BTC")
    assert rules_accepted(NEW_TEXT, "BTC")
    assert not rules_accepted(NEW_TEXT, "ETH")  # wrong pair
    assert not rules_accepted(NEW_TEXT.replace("higher than", "lower than"), "BTC")
    rows = [
        {"rules_hash": "a", "rules_standard": 0, "kind": "above", "asset": "BTC", "rules": NEW_TEXT},
        {"rules_hash": "b", "rules_standard": 0, "kind": "above", "asset": "BTC", "rules": "something else"},
        {"rules_hash": "c", "rules_standard": 1, "kind": "touch", "asset": "BTC", "rules": None},
        {"rules_hash": "d", "rules_standard": 0, "kind": "touch", "asset": "BTC", "rules": NEW_TEXT},
    ]
    assert accepted_rule_hashes(rows) == {"a", "c"}


def test_accepted_hashes_is_required():
    with pytest.raises(TypeError):
        eligible(row(END - 48 * H), EntryRule())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        open_positions([row(END - 48 * H)])  # type: ignore[call-arg]


def test_resolution_day_and_week_keys():
    start, end = day_window(date(2026, 10, 18))
    assert resolution_day(end) == "2026-10-18"  # 00:00 ET of the 19th
    assert resolution_day(settle_ts(date(2026, 10, 18))) == "2026-10-18"
    assert resolution_week(end) == "2026-W42"  # Sunday 18th, not Monday 19th
    assert et_week(end) == "2026-W43"
    assert et_week(settle_ts(date(2026, 10, 12))) == et_week(settle_ts(date(2026, 10, 18))) == "2026-W42"


def test_vol_scaled_prob_analytic():
    F, K, tau, s = 70_000.0, 72_000.0, 5 / 365, 0.5
    p_flat = ncdf(d1d2(F, K, tau, s)[1])
    assert vol_scaled_prob("above", p_flat, s, F, K, tau, 1.0) == p_flat
    assert vol_scaled_prob("above", p_flat, s, F, K, tau, 0.92) == pytest.approx(ncdf(d1d2(F, K, tau, 0.92 * s)[1]), abs=1e-12)
    # the skew term (p_model - N(d2)) is carried over unchanged
    assert vol_scaled_prob("above", p_flat + 0.03, s, F, K, tau, 0.92) == pytest.approx(
        ncdf(d1d2(F, K, tau, 0.92 * s)[1]) + 0.03, abs=1e-12
    )
    pt = prob_touch(F, K, s, tau)
    assert vol_scaled_prob("touch", pt, s, F, K, tau, 0.92) == pytest.approx(prob_touch(F, K, 0.92 * s, tau), abs=1e-12)
    # lower vol -> an OTM event is less likely
    assert vol_scaled_prob("touch", pt, s, F, K, tau, 0.92) < pt
    assert vol_scaled_prob("above", 0.5, None, F, K, tau, 0.92) is None
    r = row(END - 5 * 24 * H, model_prob=p_flat, model_iv=s, spot=F, strike=K)
    assert model_prob(r, VOL_HAIRCUT_K) == pytest.approx(ncdf(d1d2(F, K, 5 * 24 * H / YEAR_S, 0.92 * s)[1]), abs=1e-12)


def test_side_book_mid_fallback():
    r = row(0, no_bid=None)
    assert side_book(r, "yes") == {"ask": 0.50, "ask_usd": 500.0, "mid": pytest.approx(0.49)}
    assert side_book(r, "no")["mid"] == pytest.approx(0.51)  # 1 - YES mid


RN = EntryRule(vol_ks=(1.0,))  # risk-neutral reading only: exact analytic edges


def test_entry_signal_edge_after_fee_and_filters():
    t = END - 48 * H
    s = entry_signals(row(t), RN, ACC)
    assert [x["side"] for x in s] == ["yes"] and s[0]["edge"] == pytest.approx(0.10)
    fee = entry_signals(row(t, fee_rate=0.07, fee_exp=1.0), RN, ACC)
    assert fee[0]["cost"] == pytest.approx(0.50 + 0.07 * 0.25) and fee[0]["edge"] == pytest.approx(0.0825)
    assert entry_signals(row(t, model_prob=0.54), RN, ACC) == []  # edge 0.04 < 0.05
    assert entry_signals(row(t, yes_ask_usd=150.0), RN, ACC) == []  # depth
    assert entry_signals(row(t, model_note="touched", model_prob=1.0), RN, ACC) == []
    assert entry_signals(row(t, model_covered=0), RN, ACC) == []
    assert entry_signals(row(t), RN, set()) == []  # hash not accepted
    assert entry_signals(row(t, rules_standard=0), RN, ACC)  # accepted by hash, whatever the collector's flag
    assert entry_signals(row(END - H), RN, ACC) == []  # < 2 h to the end
    tail = entry_signals(row(t, yes_ask=0.01, yes_bid=0.005, model_prob=0.2, no_ask_usd=10.0), RN, ACC)
    assert tail == []  # YES ask outside [0.02, 0.98], NO too thin
    no = entry_signals(row(t, model_prob=0.40), RN, ACC)
    assert [x["side"] for x in no] == ["no"] and no[0]["edge"] == pytest.approx(0.60 - 0.52)
    # book and surface read too far apart -> not eligible; within 60 s or without the columns -> eligible
    assert entry_signals(row(t, book_ts=t, surface_ts=t + 61), RN, ACC) == []
    assert entry_signals(row(t, book_ts=t, surface_ts=t + 60), RN, ACC)


def test_entry_band_takes_the_less_favourable_reading():
    # OTM touch, 3 days: the 0.92 haircut lowers P(touch) by ~3.4 pp
    F, K, s, t = 70_000.0, 72_100.0, 0.55, END - 72 * H
    tau = 72 * H / YEAR_S
    p1, p2 = prob_touch(F, K, s, tau), prob_touch(F, K, 0.92 * s, tau)
    r = row(
        t,
        kind="touch",
        direction="up",
        strike=K,
        spot=F,
        model_iv=s,
        model_prob=p1,
        yes_ask=round(p1 - 0.06, 2),
        yes_bid=round(p1 - 0.08, 2),
    )
    r.update(no_ask=1 - r["yes_bid"], no_bid=1 - r["yes_ask"])
    assert [x["side"] for x in entry_signals(r, RN, ACC)] == ["yes"]  # 6 pp under the risk-neutral reading
    assert entry_signals(r, EntryRule(), ACC) == []  # 6 - 3.4 < 5 pp under the band
    (y,) = entry_signals(r, EntryRule(vol_ks=(1.0,), threshold=0.0), ACC)
    (yb,) = entry_signals(r, EntryRule(threshold=0.0), ACC)
    assert y["p_win"] == pytest.approx(p1) and yb["p_win"] == pytest.approx(p2, abs=1e-12)
    # the NO side is judged on the higher YES probability (the risk-neutral one here)
    r2 = dict(r, yes_ask=round(p1 + 0.10, 2), yes_bid=round(p1 + 0.08, 2))
    r2.update(no_ask=1 - r2["yes_bid"], no_bid=1 - r2["yes_ask"])
    (n,) = entry_signals(r2, EntryRule(), ACC)
    assert n["side"] == "no" and n["p_win"] == pytest.approx(1 - p1)


def test_open_positions_first_per_market_side_and_causal():
    rows = [row(END - (100 - i) * H, model_prob=0.50 + 0.01 * (i % 12)) for i in range(0, 90, 3)]
    rows += [row(END - (100 - i) * H, market_id="m2", model_prob=0.40) for i in range(0, 90, 7)]
    pos = open_positions(rows, EntryRule(), ACC)
    keys = [(p["market_id"], p["side"]) for p in pos]
    assert len(keys) == len(set(keys)) == 2
    first_m1 = min(r["ts"] for r in rows if r["market_id"] == "m1" and r["model_prob"] >= 0.55)
    assert next(p for p in pos if p["market_id"] == "m1")["ts"] == first_m1
    # positions before t do not change when data after t changes
    cut = END - 60 * H
    later = [dict(r, model_prob=0.99 - r["model_prob"], yes_ask=0.30) if r["ts"] > cut else r for r in rows]
    before = [p for p in pos if p["ts"] <= cut]
    assert [p for p in open_positions(later, EntryRule(), ACC) if p["ts"] <= cut] == before
    assert open_positions([r for r in rows if r["ts"] <= cut], EntryRule(), ACC) == before


def test_delayed_entry_next_snapshot_or_dropped():
    t = END - 48 * H
    (pos,) = open_positions([row(t)], RN, ACC)
    nxt = [row(t), row(t + H, yes_ask=0.52, yes_bid=0.50)]
    d = delayed_entry(pos, nxt, RN, ACC)
    assert d is not None and d["ts"] == t + H and d["cost"] == pytest.approx(0.52) and d["delayed_from"] == t
    assert delayed_entry(pos, [row(t), row(t + H, yes_ask=0.57, yes_bid=0.55)], RN, ACC) is None  # gap gone
    assert delayed_entry(pos, [row(t), row(t + 3 * H)], RN, ACC) is None  # no snapshot within 2 h


def test_worse_entry_one_cent_above_the_ask_with_the_fee_at_that_price():
    t = END - 48 * H
    (pos,) = open_positions([row(t)], RN, ACC)
    w = worse_entry(pos)
    assert w["ask"] == pytest.approx(0.51) and w["cost"] == pytest.approx(0.51) and w["ts"] == t and w["side"] == "yes"
    (pf,) = open_positions([row(t, fee_rate=0.07, fee_exp=1.0)], RN, ACC)
    assert pf["fee_rate"] == 0.07 and pf["fee_exp"] == 1.0
    assert worse_entry(pf)["cost"] == pytest.approx(0.51 + 0.07 * 0.51 * 0.49)  # fee upper bound at the worse price
    assert pos["cost"] == pytest.approx(0.50)  # the original is untouched


def test_asset_permutation_within_week_analytic():
    # 4 weeks, one BTC and one ETH each, BTC always 1 above ETH: within-week permutations flip each week's sign with
    # probability 1/2, so |diff*| >= 1 only when all four agree: p = 2 / 2**4 = 0.125
    weeks = ["w1", "w2", "w3", "w4"]
    base = [0.0, 3.0, -2.0, 5.0]
    vals = [x + d for x in base for d in (1.0, 0.0)]
    assets = ["BTC", "ETH"] * 4
    cl = [w for w in weeks for _ in (0, 1)]
    r = asset_permutation_p(vals, assets, cl)
    assert r["diff"] == pytest.approx(1.0) and r["n_a"] == r["n_b"] == 4 and r["n_perm"] == 10_000
    assert r["p"] == pytest.approx(0.125, abs=0.015)
    # equal within every week: no difference, p = 1
    assert asset_permutation_p([1.0, 1.0, 2.0, 2.0], assets[:4], ["w1", "w1", "w2", "w2"])["p"] == pytest.approx(1.0)
    # each week holds one asset only: within-week permutation cannot move anything, whatever the gap (p = 1)
    one = asset_permutation_p([1.0, 1.0, 0.0, 0.0], ["BTC", "BTC", "ETH", "ETH"], ["w1", "w1", "w2", "w2"], n_perm=200)
    assert one["diff"] == pytest.approx(1.0) and one["p"] == pytest.approx(1.0)
    # other assets are ignored; a missing asset gives no test
    assert asset_permutation_p(vals + [99.0], assets + ["SOL"], cl + ["w1"], n_perm=200)["diff"] == pytest.approx(1.0)
    assert asset_permutation_p([1.0, 2.0], ["BTC", "BTC"], ["w1", "w2"]) == {"n_a": 2, "n_b": 0, "diff": None, "p": None, "n_perm": 0}


def test_closing_value_windows_and_no_outcome_fallback():
    e = END - 48 * H
    mk = [row(END - 30 * H), row(END - 5 * H, yes_bid=0.70, yes_ask=0.72), row(END - 90 * 60, yes_bid=0.80, yes_ask=0.82)]
    assert closing_value(mk, "yes", END, e) == (pytest.approx(0.81), "close")
    assert closing_value(mk[:2], "yes", END, e) == (pytest.approx(0.71), "late")
    assert closing_value(mk[:1], "yes", END, e) == (None, "missing")  # never the settled outcome
    # a snapshot inside the last hour is not the closing line
    assert closing_value([row(END - 30 * 60)], "yes", END, e) == (None, "missing")


def test_closing_value_never_the_entry_snapshot():
    e = END - 2 * H  # entry exactly at end - 2 h: allowed by the rule, inside the closing window
    (pos,) = open_positions([row(e)], EntryRule(), ACC)
    assert closing_value([row(e)], "yes", END, e) == (None, "missing")
    m = position_metrics(pos, [row(e)], 1)
    assert m["clv"] is None and m["close_src"] == "missing"
    later = [row(e), row(END - H, yes_bid=0.60, yes_ask=0.62)]
    assert closing_value(later, "yes", END, e) == (pytest.approx(0.61), "close")


def test_position_metrics_clv_and_pnl():
    t = END - 48 * H
    mk = [
        row(t),
        row(t + 6 * H, yes_bid=0.52, yes_ask=0.54),
        row(t + 24 * H, yes_bid=0.58, yes_ask=0.60),
        row(END - 90 * 60, yes_bid=0.90, yes_ask=0.92),
    ]
    (pos,) = open_positions(mk[:1], EntryRule(), ACC)
    m = position_metrics(pos, mk, 1)
    assert m["clv_24h"] == pytest.approx(0.59 - 0.50) and m["src_24h"] == "ok"
    assert m["clv_6h"] == pytest.approx(0.53 - 0.50) and m["src_6h"] == "ok"
    assert m["clv"] == pytest.approx(0.91 - 0.50)
    assert m["clv_gross"] == pytest.approx(0.91 - 0.49)
    assert m["pnl"] == pytest.approx(0.50) and m["ret_capital"] == pytest.approx(1.0)
    short = position_metrics({**pos, "ts": END - 20 * H}, mk, None)
    assert short["clv_24h"] is None and short["src_24h"] == "n/a" and short["pnl"] is None
    assert horizon_clv(pos, mk[:1], 24) == (None, "missing")  # defined, but no snapshot near +24 h


def test_brier_by_market_analytic():
    rows = [row(END - 48 * H), row(END - 24 * H), row(END - 24 * H, market_id="m2", model_note="pre_window")]
    (b,) = brier_by_market(rows, {"m1": 1, "m2": 0}, EntryRule(), ACC)
    assert b["n"] == 2
    assert b["bm"] == pytest.approx(0.16)  # (0.60 - 1)^2
    assert b["bp"] == pytest.approx(0.51**2)  # YES mid 0.49
    assert b["diff"] == pytest.approx(0.16 - 0.51**2)
    assert b["week_res"] == resolution_week(END)


def test_cluster_ci_resamples_clusters_not_rows():
    c = cluster_mean_ci([0.03] * 6, list("aabbcc"), days=list("xyzxyz"))
    assert c["mean"] == pytest.approx(0.03) and c["lo"] == pytest.approx(0.03) and c["hi"] == pytest.approx(0.03)
    assert c["n_days"] == 3 and c["n_clusters"] == 3
    assert cluster_mean_ci([0.1, 0.2], ["a", "a"])["lo"] is None
    # four clusters of ten identical values, alternating +1 / -1: the cluster CI is far wider than a row bootstrap's
    vals = [1.0] * 10 + [-1.0] * 10 + [1.0] * 10 + [-1.0] * 10
    cl = [c for c in "abcd" for _ in range(10)]
    by_cluster = cluster_mean_ci(vals, cl)
    by_row = cluster_mean_ci(vals, list(range(40)))
    assert by_cluster["mean"] == pytest.approx(0.0) == by_row["mean"]
    assert by_cluster["hi"] - by_cluster["lo"] > 2.5 * (by_row["hi"] - by_row["lo"])
    assert by_cluster["se"] == pytest.approx(0.5, rel=0.05)  # SD of +-1 cluster means / sqrt(4)
    assert by_row["se"] == pytest.approx(1 / math.sqrt(40), rel=0.05)


def test_mde_power_and_gate():
    assert mde(0.1, 4) == pytest.approx(0.14)
    vals = [0.0, 1.0, 0.0, 1.0, 2.0, 2.0]
    p = power_stage0(vals, list("aabbcc"), projected_clusters=48)
    assert "mean" not in p
    assert p["sd_cluster"] == pytest.approx(math.sqrt(0.75))  # cluster means 0.5, 0.5, 2.0
    assert p["mde_now"] == pytest.approx(2.8 * p["se"])
    assert p["mde_projected"] == pytest.approx(p["mde_now"] * math.sqrt(3 / 48))
    # deviations only: shifting every value leaves the power numbers unchanged
    q = power_stage0([v + 0.3 for v in vals], list("aabbcc"), projected_clusters=48)
    assert q["se"] == pytest.approx(p["se"], abs=1e-9) and q["mde_projected"] == pytest.approx(p["mde_projected"], abs=1e-9)
    assert power_gate(0.015) == "full" and power_gate(0.04) == "partial" and power_gate(0.06) == "underpowered"
    assert power_gate(None) == "underpowered"


def test_verdict_rules():
    ok = {"mean": 0.03, "lo": 0.01, "hi": 0.05, "n_clusters": 12, "n_days": 60}
    pnl = {"mean": 0.01, "lo": -0.05, "hi": 0.07}
    nob = {"mean": -0.002, "lo": -0.01, "hi": 0.005, "n_clusters": 12, "n_days": 60}
    against = {**nob, "mean": 0.004, "lo": 0.001}
    kill = {"mean": -0.01, "lo": -0.03, "hi": 0.015, "n_clusters": 12, "n_days": 60}
    assert verdict(ok, pnl, nob) == ("confirmed", "clv")
    assert verdict(ok, {"hi": -0.01}, nob) == ("inconclusive", "pnl_contradicts")
    assert verdict(ok, {"hi": None}, nob) == ("inconclusive", "pnl_contradicts")
    # Brier against Polymarket's favour while CLV is confirmed: a conflict, not a kill
    assert verdict(ok, pnl, against) == ("inconclusive", "conflict_brier")
    # Brier kills when the CLV is not confirmed
    assert verdict({**ok, "lo": -0.01}, pnl, against) == ("rejected", "brier")
    assert verdict(kill, pnl, nob) == ("rejected", "clv")
    assert verdict(kill, pnl, against) == ("rejected", "clv")
    assert verdict({**kill, "lo": -0.05, "hi": 0.03}, pnl, nob) == ("inconclusive", "ci_spans")
    # minimum sample applies to every branch: 5 clusters or 15 days -> inconclusive, whatever the CI
    for small in ({"n_clusters": 5}, {"n_days": 15}):
        assert verdict({**ok, **small}, pnl, nob)[0] == "inconclusive"
        assert verdict({**kill, **small}, pnl, nob)[0] == "inconclusive"
        assert verdict({**ok, "lo": -0.01}, pnl, {**against, **small})[0] == "inconclusive"
    assert verdict({**ok, "n_clusters": 5}, pnl, {**against, "n_clusters": 5}) == ("inconclusive", "min_sample")


# ----------------------------------------------------------------------------- variant v2-g (H8: forward x (1 + g))

V2G = EntryRule(model=MODEL_V2G)


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def test_v2g_above_matches_the_first_order_benchmark_and_the_exact_digital():
    # ATM, 1 day, IV 50 %, g = 0.1 %: delta ~ phi(d2) g / (sigma sqrt tau) ~ 1.5 pp (the report's reference)
    F, K, s, g = 70_000.0, 70_000.0, 0.50, 0.001
    t = END - 24 * H
    tau = 24 * H / YEAR_S
    p0 = ncdf(d1d2(F, K, tau, s)[1])
    r = row(t, model_prob=p0, model_iv=s, spot=F, strike=K, binance_last=F * (1 + g))
    assert binance_gap(r) == pytest.approx(g, abs=1e-15)
    p1 = model_prob(r, 1.0, MODEL_V2G)
    assert p1 == pytest.approx(ncdf(d1d2(F * (1 + g), K, tau, s)[1]), abs=1e-12)
    d2 = d1d2(F, K, tau, s)[1]
    bench = _phi(d2) * g / (s * math.sqrt(tau))
    assert p1 - p0 == pytest.approx(bench, rel=0.01) and p1 - p0 == pytest.approx(0.0152, abs=5e-4)
    # v1 is untouched by binance_last
    assert model_prob(r) == p0 and model_prob(r, VOL_HAIRCUT_K) == pytest.approx(ncdf(d1d2(F, K, tau, 0.92 * s)[1]), abs=1e-12)
    # the vol haircut is applied at the shifted forward; the smile-slope term of the stored probability is carried over
    assert model_prob(r, VOL_HAIRCUT_K, MODEL_V2G) == pytest.approx(ncdf(d1d2(F * (1 + g), K, tau, 0.92 * s)[1]), abs=1e-12)
    skew = dict(r, model_prob=p0 + 0.03)
    assert model_prob(skew, 1.0, MODEL_V2G) == pytest.approx(p1 + 0.03, abs=1e-12)


def test_v2g_touch_shifts_by_the_barrier_formula_and_the_sign_by_direction():
    F, s, g, t = 70_000.0, 0.55, 0.0014, END - 72 * H
    tau = 72 * H / YEAR_S
    for direction, K in (("down", 0.97 * F), ("up", 1.03 * F)):
        p0 = prob_touch(F, K, s, tau)
        r = row(t, kind="touch", direction=direction, strike=K, model_prob=p0, model_iv=s, spot=F, binance_last=F * (1 + g))
        p1 = model_prob(r, 1.0, MODEL_V2G)
        assert p1 == pytest.approx(prob_touch(F * (1 + g), K, s, tau), abs=1e-12)
        # Binance above the index: a dip is less likely, a reach more likely
        assert (p1 < p0) if direction == "down" else (p1 > p0)
    bad = row(t, kind="touch", direction=None, model_prob=0.3, binance_last=F * (1 + g))
    assert model_prob(bad, 1.0, MODEL_V2G) is None  # shift_delta needs the barrier side


def test_v2g_with_g_zero_is_identical_to_v1():
    t = END - 72 * H
    F, K, s = 70_000.0, 72_100.0, 0.55
    p_touch = prob_touch(F, K, s, 72 * H / YEAR_S)
    touch = row(t, kind="touch", direction="up", strike=K, model_iv=s, model_prob=p_touch, yes_ask=round(p_touch - 0.09, 2))
    touch.update(yes_bid=touch["yes_ask"] - 0.02, no_ask=1 - touch["yes_ask"] + 0.02, no_bid=1 - touch["yes_ask"])
    rows = [row(END - 48 * H), row(END - 24 * H, market_id="m2", model_prob=0.40), dict(touch, market_id="m3"), row(END - 30 * H)]
    rows = [dict(r, binance_last=r["spot"]) for r in rows]
    for r in rows:
        assert binance_gap(r) == 0.0
        for k in (1.0, VOL_HAIRCUT_K):
            assert model_prob(r, k, MODEL_V2G) == model_prob(r, k)
    strip = [{k: v for k, v in p.items() if k != "model"} for p in open_positions(rows, EntryRule(), ACC)]
    v2 = open_positions(rows, V2G, ACC)
    assert len(strip) == 3 and [{k: v for k, v in p.items() if k != "model"} for p in v2] == strip
    assert {p["model"] for p in v2} == {MODEL_V2G}
    out = {"m1": 1, "m2": 0, "m3": 0}
    for k in (1.0, VOL_HAIRCUT_K):
        assert brier_by_market(rows, out, V2G, ACC, k) == brier_by_market(rows, out, EntryRule(), ACC, k)


def test_v2g_without_binance_last_is_not_computed_never_zero():
    t = END - 48 * H
    for miss in ({}, {"binance_last": None}, {"binance_last": 0.0}, {"binance_last": float("nan")}):
        r = row(t, **miss)
        assert binance_gap(r) is None
        assert model_prob(r, 1.0, MODEL_V2G) is None and model_prob(r, VOL_HAIRCUT_K, MODEL_V2G) is None
        assert entry_signals(r, V2G, ACC) == [] and entry_signals(r, EntryRule(), ACC)  # v1 enters, v2-g has no reading
        assert brier_by_market([r], {"m1": 1}, V2G, ACC) == []
    with pytest.raises(ValueError, match="model"):
        model_prob(row(t), 1.0, "v3")


def test_snapshot_g_reaches_threshold_rows_of_the_same_moment_and_asset_only():
    t, F, g = END - 48 * H, 70_000.0, 0.0014
    touch = row(t, market_id="t1", kind="touch", direction="down", strike=0.97 * F, binance_last=F * (1 + g))
    above = row(t, market_id="a1")  # threshold rows carry no binance_last in the collector
    other_asset = row(t, market_id="e1", asset="ETH", spot=2_500.0)
    other_ts = row(t + H, market_id="a1")
    assert snapshot_gaps([touch, above, other_asset, other_ts]) == {(t, "BTC"): pytest.approx(g)}
    out = {(r["market_id"], r["ts"]): r for r in with_snapshot_g([touch, above, other_asset, other_ts])}
    assert binance_gap(out[("a1", t)]) == pytest.approx(g) and binance_gap(out[("t1", t)]) == pytest.approx(g)
    assert binance_gap(out[("e1", t)]) is None and binance_gap(out[("a1", t + H)]) is None  # no reading: never g = 0
    assert model_prob(out[("a1", t + H)], 1.0, MODEL_V2G) is None
    assert binance_gap(above) is None  # without the snapshot step a threshold row has no g of its own
    assert model_prob(out[("a1", t)], 1.0, MODEL_V2G) > model_prob(above)  # g > 0 lifts P(above)


def test_g_split_flags_v1_positions_whose_edge_rests_on_the_shift():
    # YES on a dip, 3 days: v1 edge 5.5 pp; with Binance 0.14 % above the index the dip is less likely and the edge < 5 pp
    F, s, g, t = 70_000.0, 0.55, 0.0014, END - 72 * H
    K, tau = 0.97 * F, 72 * H / YEAR_S
    p0, p1 = prob_touch(F, K, s, tau), prob_touch(F * (1 + g), K, s, tau)
    assert p0 - p1 > 0.006  # the shift moves the probability by more than the 0.5 pp margin below
    dip = row(t, market_id="dip", kind="touch", direction="down", strike=K, model_iv=s, model_prob=p0, binance_last=F * (1 + g))
    dip.update(yes_ask=p0 - 0.055, yes_bid=p0 - 0.065, no_ask=1 - p0 + 0.065, no_bid=1 - p0 + 0.055)
    # ATM threshold market, YES: g > 0 raises P(above), so the edge grows and the position stays
    above = row(t, market_id="above", model_prob=0.60, binance_last=F * (1 + g))
    nobin = row(t, market_id="nobin")
    rows = [dip, above, nobin]
    v1 = open_positions(rows, RN, ACC)
    assert sorted(p["market_id"] for p in v1) == ["above", "dip", "nobin"]
    split = {x["market_id"]: x for x in g_split(v1, rows, RN)}
    d = split["dip"]
    assert d["g_status"] == "g_dependent" and d["group"] == "touch:down:yes" and d["g"] == pytest.approx(g)
    assert d["edge"] == pytest.approx(0.055) and d["edge_v2g"] == pytest.approx(0.055 - (p0 - p1), abs=1e-12)
    assert split["above"]["g_status"] == "robust" and split["above"]["group"] == "above:above:yes"
    assert split["nobin"]["g_status"] == "no_binance" and split["nobin"]["edge_v2g"] is None
    # v2-g itself does not open the dip
    assert sorted(p["market_id"] for p in open_positions(rows, EntryRule(vol_ks=(1.0,), model=MODEL_V2G), ACC)) == ["above"]
