"""The registered forward rule of IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward (step 1, item 481):
analytics/pm_cpi.parse_bracket / is_partition / bracket_prob / taker_fee / rule_signals, the stage helpers (row_why,
book_why, close_quote, fill, clv, sha256_json, row_digest, same_record) and the archive SD (yoy_published, archive_errors,
sigma_archive)."""

import json
import math
from pathlib import Path
from statistics import NormalDist

import pytest

from tradedesk.analytics.pm_cpi import (
    EDGE_MIN,
    ROW_DIGEST_COLUMNS,
    SD_ALT_FACTOR,
    SD_KZ,
    STAKE_MAX_USD,
    archive_errors,
    book_why,
    bracket_prob,
    brier_release,
    close_quote,
    clv,
    clv_release,
    contains,
    digest_preimage,
    fill,
    fred_levels,
    is_partition,
    market_prob,
    merge_proof_why,
    model_ok,
    parse_bracket,
    pm_resolution,
    resolution_check,
    result_cutoff,
    row_digest,
    row_why,
    rule_signals,
    same_record,
    same_target,
    sha256_json,
    sigma_archive,
    signal_figures,
    stage_why,
    stage_window,
    taker_fee,
    vintage_why,
    yoy_published,
)

TITLES = ["≤3.3%", "3.4%", "3.5%", "3.6%", "3.7%", "3.8%", "3.9%", "4.0%", "4.1%", "4.2%", "4.3%", "≥4.4%"]  # 26.11


@pytest.mark.parametrize(
    ("title", "want"),
    [("3.7%", (37, 37)), ("≤3.3%", (None, 33)), ("≥4.4%", (44, None)), ("0.7%+", (7, None)), ("-0.2%", (-2, -2)),
     ("≤-0.3%", (None, -3)), ("−0.1%", (-1, -1)), (" ≥9.9% ", (99, None)), ("More than 3.0%", None), ("", None),
     (None, None), ("3.75%", None), ("≥4.4%+", None)],
)  # fmt: skip
def test_parse_bracket(title, want):
    assert parse_bracket(title) == want


def test_partition_of_the_live_event_and_broken_ones():
    ivs = [parse_bracket(t) for t in TITLES]
    assert is_partition(ivs)
    assert not is_partition([iv for iv in ivs if iv != (37, 37)])  # a gap
    assert not is_partition(ivs[1:])  # no lower tail
    assert not is_partition(ivs + [(37, 37)])  # an overlap


def test_bracket_prob_rounding_tails_and_sum():
    nd = NormalDist(3.79, SD_KZ)
    assert bracket_prob((38, 38), 3.79, SD_KZ) == pytest.approx(nd.cdf(3.85) - nd.cdf(3.75), abs=1e-12)
    assert bracket_prob((None, 33), 3.79, SD_KZ) == pytest.approx(nd.cdf(3.35), abs=1e-12)
    assert bracket_prob((44, None), 3.79, SD_KZ) == pytest.approx(1 - nd.cdf(4.35), abs=1e-12)
    # mu on the rounding boundary 3.75 splits 3.7 / 3.8 evenly; a partition sums to 1
    assert bracket_prob((37, 37), 3.75, 0.1) == pytest.approx(bracket_prob((38, 38), 3.75, 0.1), abs=1e-12)
    assert sum(bracket_prob(parse_bracket(t), 3.79, SD_KZ) for t in TITLES) == pytest.approx(1.0, abs=1e-12)


def test_taker_fee_by_registered_type_only():
    assert taker_fee(0.5, "economics_fees") == pytest.approx(0.01 * 0.25)
    assert taker_fee(0.2, "sports_fees_v2") == pytest.approx(0.03 * 0.16)
    assert taker_fee(0.5, None) is None
    assert taker_fee(0.5, "crypto_fees") is None
    # the market's own Gamma fields come first
    assert taker_fee(0.5, "economics_fees", {"rate": 0.05, "exponent": 1}) == pytest.approx(0.05 * 0.25)
    assert taker_fee(0.5, "crypto_fees", {"rate": 0.07, "exponent": 2}) == pytest.approx(0.07 * 0.0625)
    assert taker_fee(0.5, None, None, False) == 0.0
    assert taker_fee(0.5, "economics_fees", {"rate": "x", "exponent": 1}) == pytest.approx(0.01 * 0.25)  # bad -> table


def _b(title, ask, fee_type="economics_fees", mid="m", **kw):
    return {"title": title, "ask": ask, "fee_type": fee_type, "market_id": f"{mid}{title}", **kw}


def test_rule_picks_at_most_two_by_edge_and_skips_ineligible():
    mu, sd = 3.70, 0.10
    q = {t: bracket_prob(parse_bracket(t), mu, sd) for t in TITLES}
    asks = {t: 0.99 for t in TITLES}
    asks["3.7%"] = q["3.7%"] - 0.20  # edge ~0.20 - fee
    asks["3.6%"] = q["3.6%"] - 0.10
    asks["3.8%"] = q["3.8%"] - 0.15
    rows = [_b(t, asks[t]) for t in TITLES]
    out = {r["title"]: r for r in rule_signals([rows], mu, sd)}
    assert [t for t, r in out.items() if r["pick"]] == ["3.7%", "3.8%"]
    f = taker_fee(asks["3.6%"], "economics_fees")
    assert out["3.6%"]["edge"] == pytest.approx(0.10 - f) and out["3.6%"]["edge"] >= EDGE_MIN
    assert not out["3.6%"]["pick"]  # third by edge


def test_rule_threshold_is_net_of_the_fee_and_ineligible_reasons():
    mu, sd = 3.70, 0.10
    q = bracket_prob((37, 37), mu, sd)
    rows = [_b(t, None) for t in TITLES]
    rows[4] = _b("3.7%", q - EDGE_MIN)  # gross 8 pp, the fee takes it below
    rows[3] = _b("3.6%", 0.01, fee_type="other")
    rows[5] = _b("3.8%", 0.01, gone_ts=1)
    out = {r["title"]: r for r in rule_signals([rows], mu, sd)}
    assert out["3.7%"]["edge"] < EDGE_MIN and not out["3.7%"]["pick"]
    assert out["3.6%"]["why"] == "fee type" and out["3.8%"]["why"] == "closed" and out["3.9%"]["why"] == "no ask"


def test_rule_without_a_partition_gives_no_model():
    rows = [_b(t, 0.01) for t in TITLES if t != "3.9%"]
    out = rule_signals([rows], 3.7, 0.1)
    assert all(r["q"] is None and not r["pick"] and r["why"] == "not a partition" for r in out)
    rows = [_b(t, 0.01) for t in TITLES] + [_b("More than 3.0%", 0.01)]
    assert not any(r["pick"] for r in rule_signals([rows], 3.7, 0.1))


def test_rule_never_picks_overlapping_brackets_across_events_of_a_release():
    a = [_b("≤3.3%", 0.01, mid="a"), _b("3.4%", 0.01, mid="a"), _b("≥3.5%", 0.01, mid="a")]
    b = [_b("≤3.2%", 0.01, mid="b"), _b("≥3.3%", 0.01, mid="b")]  # another event of the same release
    out = rule_signals([a, b], 3.3, 0.3)
    picks = [(r["event"], r["title"]) for r in out if r["pick"]]
    # a:≤3.3 and b:≥3.3 tie on edge and q (market id breaks it); b:≥3.3 and b:≤3.2 overlap a:≤3.3 -> a:≥3.5 is second
    assert picks == [(0, "≤3.3%"), (0, "≥3.5%")]
    assert _overlap_free([r["interval"] for r in out if r["pick"]])


def _overlap_free(ivs):
    from tradedesk.analytics.pm_cpi import _overlap

    return not any(_overlap(a, b) for i, a in enumerate(ivs) for b in ivs[i + 1 :])


# ---------------------------------------------------------------------------------------------- model guard and q_alt


@pytest.mark.parametrize(
    ("mu", "sd"),
    [(3.7, 0.0), (3.7, -0.1), (3.7, float("nan")), (3.7, float("inf")), (float("nan"), 0.164), (float("inf"), 0.164),
     (None, 0.164), (True, 0.164), ("3.7", 0.164), (3.7, None)],
)  # fmt: skip
def test_rule_without_a_usable_model_records_why_and_never_raises(mu, sd):
    rows = [_b(t, 0.01) for t in TITLES]
    out = rule_signals([rows], mu, sd)
    assert len(out) == len(TITLES) and not model_ok(mu, sd)
    assert all(r["why"] == "model" and r["q"] is None and r["edge"] is None and not r["pick"] for r in out)
    assert out[0]["interval"] == (None, 33)


def test_model_ok_accepts_negative_inflation():
    assert model_ok(-0.4, 0.164) and model_ok(0, 1e-6)


def test_descriptive_q_at_both_alternative_sds_and_kept_input_reason():
    rows = [_b(t, 0.01) for t in TITLES]
    rows[3]["why"] = "unpaired"
    out = {r["title"]: r for r in rule_signals([rows], 3.79, SD_KZ)}
    r = out["3.8%"]
    assert r["q_narrow"] == pytest.approx(bracket_prob((38, 38), 3.79, SD_KZ / SD_ALT_FACTOR), abs=1e-12)
    assert r["q_wide"] == pytest.approx(bracket_prob((38, 38), 3.79, SD_KZ * SD_ALT_FACTOR), abs=1e-12)
    assert out["3.6%"]["why"] == "unpaired" and out["3.6%"]["edge"] is None and out["3.6%"]["q"] is not None


def test_interior_bracket_q_is_capped_and_a_wider_sd_raises_the_shoulders():
    # registration п. 4: with sd = 0.164 an interior 0.1 bracket has q <= 2 Phi(0.05 / 0.164) - 1 = 0.2395
    cap = 2 * NormalDist().cdf(0.05 / SD_KZ) - 1
    assert cap == pytest.approx(0.2395, abs=1e-4)
    assert bracket_prob((37, 37), 3.70, SD_KZ) == pytest.approx(cap, abs=1e-12)
    assert max(bracket_prob((v, v), 3.79, SD_KZ) for v in range(34, 44)) <= cap + 1e-12
    # q(sd) of a bracket at distance d from mu rises with sd only for d beyond ~sd: between sd and 1.5 sd the density
    # crossover is d* = sd1 sd2 sqrt(2 ln 1.5 / (sd2^2 - sd1^2)) = 0.198 pp; the centre falls, d = 0.3 rises
    s1, s2 = SD_KZ, SD_KZ * 1.5
    d_star = s1 * s2 * (2 * math.log(1.5) / (s2**2 - s1**2)) ** 0.5
    assert d_star == pytest.approx(0.198, abs=1e-3)
    assert bracket_prob((37, 37), 3.70, s2) < bracket_prob((37, 37), 3.70, s1)
    assert bracket_prob((38, 38), 3.70, s2) < bracket_prob((38, 38), 3.70, s1)  # d = 0.1
    assert bracket_prob((39, 39), 3.70, s2) == pytest.approx(bracket_prob((39, 39), 3.70, s1), abs=1e-3)  # d = 0.2
    assert bracket_prob((40, 40), 3.70, s2) > bracket_prob((40, 40), 3.70, s1)  # d = 0.3


# --------------------------------------------------------------------------------------------- row and T-1 quote


def _row(**kw):
    r = {"status": "complete", "target": "2026-11", "cf_year_sha": "x", "cf_last_day_year": "2026-12-07",
         "yoy_headline_pct": 3.79, "books_paired": 1, "cf_year_ts": 100, "cf_month_ts": 90, "cf_month_sha": "y",
         "cf_last_day_month": "2026-12-07"}  # fmt: skip
    return {**r, **kw}


@pytest.mark.parametrize(
    ("kw", "want"),
    [({}, None), ({"status": "stale", "cf_last_day_year": "2026-12-04"}, None),
     ({"cf_last_day_year": "2026-12-03"}, None), ({"cf_last_day_year": "2026-12-02"}, "nowcast stale"),
     ({"cf_last_day_year": "2026-12-08"}, "nowcast stale"), ({"status": "failed"}, "status"),
     ({"target": "2026-10"}, "target"), ({"yoy_headline_pct": None}, "no yoy nowcast"),
     ({"cf_year_sha": None}, "no yoy nowcast"), ({"cf_last_day_year": None}, "no yoy nowcast"),
     ({"books_paired": 0}, "unpaired"), ({"status": "partial", "books_paired": 0}, None),
     ({"status": "partial", "cf_year_sha": None}, "no yoy nowcast")],
)  # fmt: skip
def test_row_why(kw, want):
    assert row_why(_row(**kw), "2026-12-07", "2026-11") == want


def test_row_why_target_ignores_zero_padding():
    # rereview finding 5: the row writes '2027-1' (as prev_target and the Cleveland charts); a slug month may be '2027-01'
    row = _row(target="2027-1", cf_last_day_year="2027-01-10")
    assert row_why(row, "2027-01-10", "2027-01") is None
    assert row_why(row, "2027-01-10", "2027-1") is None
    assert row_why(row, "2027-01-10", "2027-02") == "target"
    assert row_why(row, "2027-01-10", "2027") == "target"
    assert row_why(_row(target=None), "2026-12-07", "2026-11") == "target"
    assert same_target("2026-11", "2026-11") and not same_target("garbage", "garbage")


def test_row_why_without_a_row_and_book_why():
    assert row_why(None, "2026-12-07", "2026-11") == "no row"
    assert book_why({"book": True, "read_ts": 100}, 100) is None
    assert book_why({"book": True, "read_ts": 99}, 100) == "unpaired"
    assert book_why({"book": True, "read_ts": 99}, None) == "unpaired"
    assert book_why({"token": "t"}, 100) == "no book"


def test_t1_quote_comes_from_the_rows_books_only_and_ignores_the_nowcast_checks():
    # third review B 481, finding 1: no fallback snapshot for T-1; a row without a YoY nowcast still gives the close
    import json

    import tradedesk.analytics.pm_cpi as pc

    assert not hasattr(pc, "fallback_stamp") and not hasattr(pc, "FALLBACK_FROM")
    books = {
        "brackets": [
            {"token": "t1", "book": True, "read_ts": 50, "bid": 0.04, "ask": 0.06},  # read before the nowcast
            {"token": "t2", "book": False},
            {"token": "t3", "book": True, "read_ts": 200, "ask": 0.1},
        ]
    }
    row = _row(cf_year_sha=None, books=json.dumps(books))  # only the MoM file readable: nowcast_ts 90 > read_ts 50 of t1
    assert book_why(books["brackets"][0], 90) == "unpaired"  # the T-3 pairing check would drop it; T-1 does not apply it
    assert row_why(row, "2026-12-09", "2026-11") == "no yoy nowcast"
    q = close_quote(row, "t1")
    assert q == {"bid": 0.04, "ask": 0.06, "why": None}
    assert clv(0.02, 1000.0, q["bid"], q["ask"])["usd"] == pytest.approx(30.0)
    assert close_quote(row, "t2")["why"] == "no book" and close_quote(row, "t9")["why"] == "no book"
    assert close_quote(row, None)["why"] == "no book" and close_quote(None, "t1")["why"] == "no row"
    one_side = close_quote(row, "t3")
    assert one_side["why"] is None and clv(0.02, 10.0, one_side["bid"], one_side["ask"]) is None  # not filled in
    assert close_quote({**row, "books": "not json"}, "t1")["why"] == "no book"


def test_same_record_tolerates_float_bits_but_not_structure():
    rec = {"q": 0.2395, "interval": [33, 33], "pick": True, "why": None, "shares": 2450.98}
    assert same_record(rec, {**rec, "q": 0.2395 * (1 + 1e-12), "interval": (33, 33)})
    assert not same_record(rec, {**rec, "pick": False})
    assert not same_record(rec, {**rec, "shares": 2450.99})
    assert not same_record(rec, {**rec, "why": "no ask"})
    assert not same_record(rec, {k: v for k, v in rec.items() if k != "why"})
    assert not same_record([rec], [rec, rec]) and not same_record({"x": 1}, {"x": True})
    assert same_record([rec, {"edge": None}], [dict(rec), {"edge": None}])


# ---------------------------------------------------------------------------------------------- sizing, p_in and CLV


def _fee(p, rate=0.01):
    return rate * p * (1 - p)


def test_fill_cuts_the_last_level_at_the_stake():
    out = fill([[0.02, 10_000.0]], 0.2, "economics_fees", min_size=5)
    unit = 0.02 + _fee(0.02)
    assert out["shares"] == pytest.approx(STAKE_MAX_USD / unit) and out["cost_usd"] == pytest.approx(STAKE_MAX_USD)
    assert out["p_in"] == pytest.approx(0.02) and out["fee_usd"] == pytest.approx(out["shares"] * _fee(0.02))
    assert out["stop"] == "stake" and out["levels"] == 1 and out["executable"]


def test_fill_stops_at_the_level_where_the_edge_falls_below_8pp_and_sorts_levels():
    # at 0.13: 0.2 - 0.13 - fee = 0.0689 < 0.08; levels given out of order
    out = fill([[0.13, 1000.0], [0.10, 100.0], [0.05, 100.0]], 0.2, "economics_fees")
    assert out["levels"] == 2 and out["stop"] == "edge" and out["shares"] == pytest.approx(200.0)
    assert out["p_in"] == pytest.approx(0.075)
    assert out["cost_usd"] == pytest.approx(100 * (0.05 + _fee(0.05)) + 100 * (0.10 + _fee(0.10)))


def test_fill_book_exhausted_exact_stake_and_min_size():
    out = fill([[0.05, 10.0], [0.06, 1.0]], 0.3, None, {"rate": 0.05, "exponent": 1})
    assert out["stop"] == "book" and out["shares"] == pytest.approx(11.0) and out["executable"]  # no min_size: > 0
    unit = 0.10 + _fee(0.10)
    out = fill([[0.10, STAKE_MAX_USD / unit], [0.11, 50.0]], 0.4, "economics_fees")
    assert out["levels"] == 1 and out["stop"] == "stake"
    assert not fill([[0.05, 3.0]], 0.3, "economics_fees", min_size=5)["executable"]
    assert fill([[0.05, 5.0]], 0.3, "economics_fees", min_size=5)["executable"]
    none = fill([[0.30, 100.0]], 0.3, "economics_fees", min_size=5)  # the best level already fails the edge
    assert none["p_in"] is None and none["shares"] == 0 and not none["executable"] and none["stop"] == "edge"
    assert fill([[0.05, 10.0]], 0.3, "crypto_fees")["stop"] == "fee type"
    bad = fill([[0.0, 5], [1.0, 5], [0.05, -1], [float("nan"), 5], "x", [0.05]], 0.3, "economics_fees")
    assert bad["levels"] == 0 and bad["stop"] == "book" and bad["p_in"] is None


def test_clv_per_share_and_in_usd():
    # the review's example: per share the mean is negative, in USD it is positive
    a, b = clv(0.02, 2500.0, 0.023, 0.025), clv(0.15, 330.0, 0.13, 0.15)
    assert a["per_share"] == pytest.approx(0.004) and a["usd"] == pytest.approx(10.0)
    assert b["per_share"] == pytest.approx(-0.01) and b["usd"] == pytest.approx(-3.3)
    assert a["per_share"] + b["per_share"] < 0 < a["usd"] + b["usd"]
    assert clv(0.02, 2500.0, None, 0.025) is None and clv(None, 0.0, 0.1, 0.2) is None


# --------------------------------------------------------------------------------------------------------- digests


def test_digests_detect_any_change_and_ignore_key_order():
    row = {**_row(), "day": "2026-12-07", "updated_ts": 1, "books": '{"a":1}', "books_depth_z": b"\x00\x01"}
    d = row_digest(row)
    assert d == row_digest(dict(reversed(list(row.items())))) and len(d) == 64
    assert row_digest({**row, "books_depth_z": b"\x00\x02"}) != d
    assert row_digest({**row, "updated_ts": 2}) != d
    assert row_digest({**row, "unrelated": 5}) == d  # only the decision columns
    assert sha256_json({"a": [1, 2]}) != sha256_json({"a": [2, 1]})
    with pytest.raises(ValueError):
        sha256_json({"x": float("nan")})


def test_digest_preimage_reproduces_the_row_digest_from_the_stage_file_alone():
    # rereview finding 9: T-3.json keeps the preimage (blobs as their sha256), so its row_digest is checkable offline
    import hashlib
    import json

    blob = b"\x78\x9c\x00\x01"
    row = {**_row(), "day": "2026-12-07", "updated_ts": 1, "books": '{"a":1}', "books_depth_z": blob, "extra": 1}
    pre = digest_preimage(row)
    assert set(pre) == set(ROW_DIGEST_COLUMNS) and "extra" not in pre
    assert pre["books_depth_z"] == {"sha256": hashlib.sha256(blob).hexdigest()}
    assert pre["books_early"] is None  # a missing column is null
    stored = json.loads(json.dumps(pre))  # as written into and read back from T-3.json
    assert row_digest(stored) == row_digest(row)
    assert row_digest({**stored, "updated_ts": 2}) != row_digest(row)


R = "2026-12-10"


@pytest.mark.parametrize(
    ("stage", "now", "want"),
    [("T-3", "2026-12-07T21:04:59+00:00", "early"), ("T-3", "2026-12-07T21:05:00+00:00", None),
     ("T-3", "2026-12-07T18:00:00+00:00", "early"), ("T-3", "2026-12-09T12:00:00+00:00", None),
     ("T-3", "2026-12-09T12:00:01+00:00", "late"), ("T-3", "2026-12-09T14:00:00+02:00", None),
     ("T-3", "2026-12-09T15:30:00+00:00", "late"), ("T-7", "2026-12-03T21:05:00+00:00", None),
     ("T-7", "2026-12-03T20:00:00+00:00", "early"), ("T-7", "2026-12-20T00:00:00+00:00", None),
     ("T-1", "2026-12-09T21:05:00+00:00", None), ("T-1", "2026-12-09T16:00:00+00:00", "early"),
     ("result", "2026-12-10T14:59:00+00:00", "early"), ("result", "2026-12-10T15:00:00+00:00", None),
     ("T-2", "2026-12-08T22:00:00+00:00", "stage"), ("T-3", "2026-12-07T22:00:00", "naive time")],
)  # fmt: skip
def test_stage_window_refuses_a_stage_file_outside_its_window(stage, now, want):
    # rereview finding 4: fixed instants only (no real clock)
    from datetime import datetime

    assert stage_why(stage, R, datetime.fromisoformat(now)) == want


def test_stage_window_bounds_and_a_bad_release_day():
    from datetime import UTC, datetime

    assert stage_window("T-3", R) == (datetime(2026, 12, 7, 21, 5, tzinfo=UTC), datetime(2026, 12, 9, 12, 0, tzinfo=UTC))
    assert stage_window("T-1", R) == (datetime(2026, 12, 9, 21, 5, tzinfo=UTC), None)
    assert stage_why("T-3", "2026-13-01", datetime(2026, 12, 8, tzinfo=UTC)) == "stage"


_PATH = "docs/forward/pm-cpi/2026-12-10/T-3.json"


def _log(*commits):
    out = []
    for sha, when, status, who in commits:
        out += [f"C {sha} {when} {who[1]} {who[0]}", "", f"{status}\t{_PATH}"]
    return "\n".join(out) + "\n"


GH = ("GitHub", "noreply@github.com")
DL = "2026-12-09T12:00:00+00:00"


@pytest.mark.parametrize(
    ("log", "want"),
    [(_log(("m1", "2026-12-07T22:10:00+00:00", "A", GH)), (None, "m1")),
     (_log(("m1", "2026-12-09T12:00:00+00:00", "A", GH)), (None, "m1")),
     (_log(("m1", "2026-12-09T12:00:01+00:00", "A", GH)), ("late", "m1")),
     (_log(("m1", "2026-12-09T13:00:00+02:00", "A", GH)), (None, "m1")),
     (_log(("m2", "2026-12-09T22:00:00+00:00", "M", GH), ("m1", "2026-12-07T22:10:00+00:00", "A", GH)),
      ("changed after", "m1")),
     (_log(("m2", "2026-12-09T22:00:00+00:00", "D", GH), ("m1", "2026-12-07T22:10:00+00:00", "A", GH)),
      ("changed after", "m1")),
     (_log(("m1", "2026-12-07T22:10:00+00:00", "M", GH)), ("not added", "m1")),
     (_log(("m1", "2026-12-07T22:10:00+00:00", "A", ("Mykola Khanetskyi (agents)", "a@b.c"))),
      ("not a GitHub merge", "m1")),
     ("", ("not merged", None)), ("garbage line\n", ("unreadable", None)),
     (_log(("m1", "not-a-date", "A", GH)), ("unreadable", "m1"))],
)  # fmt: skip
def test_merge_proof_needs_one_github_merge_adding_the_file_before_the_deadline(log, want):
    # rereview finding 1 (coordinator's proof): git log origin/main --first-parent --name-status
    #   --format='C %H %cI %ce %cn' -- docs/forward/pm-cpi/<release>/T-3.json
    from datetime import datetime

    assert merge_proof_why(log, datetime.fromisoformat(DL)) == want


def test_merge_proof_parses_a_real_git_log_shape():
    # the shape git prints on this repository (checked on origin/main 27.11): a merge line, a blank line, the status
    log = f"C 7437142 2026-11-26T23:25:13+00:00 noreply@github.com GitHub\n\nA\t{_PATH}\n"
    from datetime import datetime

    assert merge_proof_why(log, datetime.fromisoformat(DL)) == (None, "7437142")


def _vchart(target, head, core, stamp=None):
    cats = [{"label": d} for d, _ in head]
    ch = {"chart": {"subcaption": target}, "categories": [{"category": cats}],
          "dataset": [{"seriesname": "CPI Inflation", "data": [{"value": v} for _, v in head]},
                      {"seriesname": "Core CPI Inflation", "data": [{"value": v} for _, v in core]}]}  # fmt: skip
    if stamp:
        ch["chart"]["_comment"] = stamp
    return ch


def _copy(stamp, head_nov, core_nov):
    return [
        _vchart("2026-10", [("10/20", 3.0), ("10/27", 3.1)], [("10/20", 2.9), ("10/27", 2.9)]),
        _vchart("2026-11", head_nov, core_nov, stamp),
    ]


OLD = _copy("2026-11-25 00:00", [("11/18", 3.7), ("11/25", 3.79)], [("11/18", 2.87), ("11/25", 2.87)])


def test_vintage_check_passes_only_without_overwrites_and_with_a_new_step():
    # V1 / V2 (registration п. 4): decided by the stored copies alone
    new = _copy("2026-12-02 00:00", [("11/18", 3.7), ("11/25", 3.79), ("12/02", 3.83)],
                [("11/18", 2.87), ("11/25", 2.87), ("12/02", 2.87)])  # fmt: skip
    assert vintage_why(OLD, new, "2026-11") is None
    assert vintage_why(OLD, new) is None
    flat = _copy("2026-12-02 00:00", [("11/18", 3.7), ("11/25", 3.79), ("12/02", 3.79)],
                 [("11/18", 2.87), ("11/25", 2.87), ("12/02", 2.87)])  # fmt: skip
    assert vintage_why(OLD, flat, "2026-11") == "no step 2026-11"
    assert vintage_why(OLD, flat) is None  # V2 asks no step
    over = _copy("2026-12-02 00:00", [("11/18", 3.71), ("11/25", 3.79), ("12/02", 3.83)],
                 [("11/18", 2.87), ("11/25", 2.87), ("12/02", 2.87)])  # fmt: skip
    assert vintage_why(OLD, over, "2026-11") == "overwritten CPI Inflation 2026-11 2026-11-18"
    core_over = _copy("2026-12-02 00:00", [("11/18", 3.7), ("11/25", 3.79)], [("11/18", 2.86), ("11/25", 2.87)])
    assert vintage_why(OLD, core_over) == "overwritten Core CPI Inflation 2026-11 2026-11-18"
    assert vintage_why(OLD, OLD) == "same stamp"
    assert vintage_why(OLD, _copy(None, [("11/18", 3.7)], [("11/18", 2.87)])) == "same stamp"
    assert vintage_why(OLD, [_vchart("2025-1", [("01/05", 1.0)], [("01/05", 1.0)], "2026-12-02 00:00")]).startswith("no shared points")


# ------------------------------------------------------------------------------------------------------ archive SD


def test_yoy_published_rounds_half_up_on_exact_levels_and_skips_gaps():
    lv = {"2024-03": "100.000", "2025-03": "102.050", "2024-04": "100", "2025-04": "102.04", "2025-10": "1"}
    out = yoy_published(lv)
    assert out == {"2025-3": 2.1, "2025-4": 2.0}  # 2.05 -> 2.1 (a float ratio would give 2.0499...)
    assert yoy_published({"2025-10": "x", "2026-10": "100"}) == {}


def _chart(target, pts, release_label=None, after=()):
    cats = [{"label": d} for d, _ in pts]
    vals = [{"value": v} for _, v in pts]
    if release_label:
        cats.append({"vline": "true", "label": release_label})
        cats += [{"label": d} for d, _ in after]
        vals += [{"value": v} for _, v in after]
    return {"chart": {"subcaption": target}, "categories": [{"category": cats}],
            "dataset": [{"seriesname": "CPI Inflation", "data": vals}]}  # fmt: skip


def _archive(late_value=3.3, extra=()):
    return [
        _chart("2023-12", [("01/05", 3.0)], "CPI Dec"),  # a target before `first` (the tests use first = 2024-1)
        _chart("2024-1", [("02/05", 3.0), ("02/09", 3.1), ("02/10", 3.2), ("02/13", late_value)], "CPI Jan",
               after=[("02/14", 9.9)]),
        _chart("2024-2", [("03/01", 3.0), ("03/08", 3.4), ("03/12", 3.5)], "CPI Feb"),
        _chart("2024-3", [("04/01", 3.0)]),  # no release line (as 2025-10)
        *extra,
    ]  # fmt: skip


def test_archive_errors_take_the_last_point_three_days_before_the_release():
    pub = {"2024-1": 3.1, "2024-2": 3.6, "2023-12": 3.4}
    rows = {r["target"]: r for r in archive_errors(_archive(), pub, before="2024-12-01", first="2024-1")}
    assert set(rows) == {"2024-1", "2024-2", "2024-3"}
    assert rows["2024-1"]["release"] == "2024-02-13" and rows["2024-1"]["day"] == "2024-02-10"
    assert rows["2024-1"]["error"] == pytest.approx(3.1 - 3.2)
    assert rows["2024-2"]["day"] == "2024-03-08" and rows["2024-2"]["error"] == pytest.approx(3.6 - 3.4)
    assert rows["2024-3"]["why"] == "no release" and rows["2024-3"]["error"] is None
    sd, n = sigma_archive(rows.values())
    assert n == 2 and sd == pytest.approx(((0.1**2 + 0.2**2) / 2) ** 0.5)
    assert archive_errors(_archive(), {}, "2024-12-01", "2024-1")[0]["why"] == "no actual"
    assert sigma_archive([]) == (None, 0)


def test_archive_sd_does_not_change_with_data_after_the_cut():
    pub = {"2024-1": 3.1, "2024-2": 3.6, "2025-1": 1.0}
    base = sigma_archive(archive_errors(_archive(), pub, "2024-12-01", "2024-1"))
    later = _chart("2025-1", [("02/03", 5.0), ("02/12", 5.0)], "CPI Jan")  # released after `before`
    assert sigma_archive(archive_errors(_archive(late_value=7.7, extra=[later]), pub, "2024-12-01", "2024-1")) == base
    # a release on or after `before` is left out entirely
    assert all(r["target"] != "2024-2" for r in archive_errors(_archive(), pub, "2024-03-12", "2024-1"))


# ----------------------------------------------------------------------------- the result stage (item 491)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_fred_levels_give_the_published_12_month_change():
    # the real tail of CPIAUCNS (FRED, 2026-11-27): 2025-10 empty (never published), so 2026-10 has no 12-month change
    csv = json.loads((FIXTURES / "fred_cpiaucns_tail_2026-11-27.json").read_text())["csv"]
    lv = fred_levels(csv)
    assert "2025-10" not in lv and lv["2026-10"] == "337.511" and lv["2025-11"] == "324.122"
    pub = yoy_published(lv)
    assert pub["2026-9"] == 3.7  # by hand: 336.940 / 324.800 - 1 = 3.7377 % -> 3.7
    assert "2026-10" not in pub and "2026-11" not in pub  # the target of 10.12 is not published in the fixture
    # a synthetic level of 2026-11 (not a published value): 336.000 / 324.122 - 1 = 3.6647 % -> 3.7
    assert yoy_published(fred_levels(csv + "2026-11-01,336.000\n"))["2026-11"] == 3.7
    assert fred_levels(None) == {} and fred_levels("h\n2026-12-01,.\nbad,1\n") == {}


def test_pm_resolution_reads_the_payout_only_when_resolved():
    real = json.loads((FIXTURES / "polymarket_cpi_resolved_market_2026-11-27.json").read_text())["market"]
    assert pm_resolution(real) == {"state": "resolved", "yes": 0.0, "closed": True, "uma_status": "resolved"}
    open_ = json.loads((FIXTURES / "polymarket_cpi_event_market_2026-11-27.json").read_text())["market"]
    assert pm_resolution(open_)["state"] == "open" and pm_resolution(open_)["yes"] is None
    assert pm_resolution({**real, "umaResolutionStatus": "proposed"})["state"] == "pending"
    assert pm_resolution({**real, "outcomePrices": '["1", "0"]'})["yes"] == 1.0
    assert pm_resolution({**real, "outcomePrices": '["0.5", "0.5"]'})["yes"] == 0.5  # a split pays 0.5
    assert pm_resolution({**real, "outcomePrices": ["1.0", "0.0"]})["yes"] == 1.0
    # review C of item 491, blocker 3: 'resolved' with last trading prices (summing to 1) is not a payout, it waits
    for bad in ({"outcomes": '["No", "Yes"]'}, {"outcomePrices": '["1", "1"]'}, {"outcomePrices": "x"},
                {"outcomePrices": '["nan", "1"]'}, {"outcomePrices": '[true, false]'}, {"outcomePrices": '["0.9995", "0.0005"]'},
                {"outcomePrices": '["0.0005", "0.9995"]'}, {"outcomePrices": '["0.3", "0.7"]'}):  # fmt: skip
        assert pm_resolution({**real, **bad}) == {"state": "unreadable", "yes": None, "closed": True, "uma_status": "resolved"}
    assert pm_resolution(None)["state"] == "unreadable"


def test_result_cutoff_is_30_days_after_the_window_opens():
    from datetime import UTC, datetime, timedelta

    # registration п. 7, result (review C of item 491, blocker 2): from the release day alone
    assert result_cutoff("2026-12-10") == datetime(2027, 1, 9, 15, 0, tzinfo=UTC)
    assert result_cutoff("2027-03-11") == stage_window("result", "2027-03-11")[0] + timedelta(days=30)


def test_resolution_check_writes_both_values():
    brs = [{"event_id": "e1", "market_id": "a", "title": "≤3.6%", "yes": 0.0},
           {"event_id": "e1", "market_id": "b", "title": "3.7%", "yes": 1.0},
           {"event_id": "e1", "market_id": "c", "title": "≥3.8%", "yes": 0.0},
           {"event_id": "e2", "market_id": "d", "title": "3.7%", "yes": 0.0},
           {"event_id": "e2", "market_id": "e", "title": "3.8%+", "yes": 1.0}]  # fmt: skip
    got = resolution_check(brs, 3.7)
    assert got == [{"event_id": "e1", "pm_yes": ["b"], "cpi_yes": ["b"], "agree": True},
                   {"event_id": "e2", "pm_yes": ["e"], "cpi_yes": ["d"], "agree": False}]  # fmt: skip
    assert resolution_check(brs[:3], None)[0]["cpi_yes"] == [] and not resolution_check(brs[:3], None)[0]["agree"]
    assert contains((None, 33), 3.3) and not contains((None, 33), 3.4) and contains((44, None), 4.4) and not contains(None, 3.3)


def test_brier_of_the_model_and_the_market_by_hand():
    rows = [
        # event 0: mid 0.2; ask / 2 = 0.3 (no bid); (bid + 1) / 2 = 0.7 (no ask) -> sum 1.2, normalised 1/6, 1/4, 7/12
        {"event": 0, "market_id": "a", "q": 0.2, "bid": 0.1, "ask": 0.3},
        {"event": 0, "market_id": "b", "q": 0.5, "bid": None, "ask": 0.6},
        {"event": 0, "market_id": "c", "q": 0.3, "bid": 0.4, "ask": None},
        {"event": 1, "market_id": "d", "q": 0.6, "bid": None, "ask": None},  # no quote at all: in neither score
        {"event": 1, "market_id": "e", "q": 0.4, "bid": None, "ask": None},
        {"event": 2, "market_id": "f", "q": 1.0, "bid": 0.5, "ask": 0.6},  # no payout: in neither score
        {"event": 3, "market_id": "g", "q": 0.9, "bid": 0.7, "ask": 0.9},  # p 0.8, 0 -> normalised 1, 0
        {"event": 3, "market_id": "h", "q": 0.1, "bid": None, "ask": None},
        {"event": 4, "market_id": "i", "q": None, "bid": 0.1, "ask": 0.2},  # no model: not an event of the score
    ]
    pay = {"a": 0.0, "b": 1.0, "c": 0.0, "d": 1.0, "e": 0.0, "g": 1.0, "h": 0.0, "i": 1.0}
    got = brier_release(rows, pay)
    ev = {e["event"]: e for e in got["events"]}
    assert set(ev) == {0, 1, 2, 3} and ev[1]["why"] == "no quotes" and ev[2]["why"] == "no payout"
    assert ev[0]["model"] == pytest.approx(0.04 + 0.25 + 0.09)  # 0.38
    assert ev[0]["market"] == pytest.approx(1 / 36 + 9 / 16 + 49 / 144)  # 134/144
    assert ev[0]["market_raw"] == pytest.approx(0.04 + 0.49 + 0.49)
    assert ev[3]["model"] == pytest.approx(0.02) and ev[3]["market"] == pytest.approx(0.0)
    assert got["n_events"] == 2 and got["model"] == pytest.approx(0.20) and got["market"] == pytest.approx(67 / 144)
    assert got["delta"] == pytest.approx(0.20 - 67 / 144) and got["market_raw"] == pytest.approx(0.53)
    assert brier_release(rows[3:5], pay) == {"events": [{**ev[1]}], "n_events": 0, "model": None, "market": None,
                                             "delta": None, "market_raw": None}  # fmt: skip
    assert market_prob(None, None) == 0.0 and market_prob(float("nan"), 0.4) == 0.2


def test_signal_figures_and_clv_of_the_release_by_hand():
    f = {"shares": 400.0, "p_in": 0.105, "cost_usd": 42.5, "fee_usd": 0.5, "executable": True}
    out_fee = taker_fee(0.14, "economics_fees")  # 0.01 * 0.14 * 0.86 = 0.001204 per share
    got = signal_figures(f, 0.14, 0.16, 1.0, out_fee)
    assert got["clv"]["mid"] == pytest.approx(0.15) and got["clv"]["usd"] == pytest.approx(400 * 0.045)  # 18.0
    assert got["usd_per_cost"] == pytest.approx(18.0 / 42.5) and got["net_usd"] == pytest.approx(17.5)
    # review C of item 491, R1 (п. 8 «виконуваний вихід … − комісія»): both taker fees of the round trip
    assert got["exit_fee_usd"] == pytest.approx(0.4816)
    assert got["exit_usd"] == pytest.approx(400 * (0.14 - 0.105) - 0.5 - 0.4816)  # 13.0184
    assert signal_figures(f, 0.14, 0.16, 1.0)["exit_usd"] is None  # no exit fee known: no exit figure
    assert got["paper_usd"] == pytest.approx(400 - 42.5) and signal_figures(f, 0.14, 0.16, 0.0)["paper_usd"] == pytest.approx(-42.5)
    no = signal_figures({**f, "executable": False}, 0.14, 0.16, 1.0, out_fee)  # not executable: no CLV, the paper result stays
    assert no["clv"] is None and no["net_usd"] is None and no["exit_usd"] is None and no["paper_usd"] == pytest.approx(357.5)
    side = signal_figures(f, None, 0.16, None, out_fee)  # one side of the T-1 book missing, no payout: nothing filled in
    assert side == {"clv": None, "usd_per_cost": None, "net_usd": None, "exit_fee_usd": None, "exit_usd": None, "paper_usd": None}
    assert clv_release([got, no, {"clv": {"usd": -3.0}}]) == {"usd": pytest.approx(15.0), "n": 2}
    assert clv_release([no]) == {"usd": None, "n": 0}
