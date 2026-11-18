import math

import pytest

from tradedesk.analytics import sample_pace as sp

W = sp.WEEK_MS


def test_weekly_counts_bins_zero_weeks_and_bounds():
    start = 1_000 * W
    ts = [start - 1, start, start + 1, start + W + 5, start + 3 * W - 1, start + 3 * W]
    assert sp.weekly_counts(ts, start, 3) == [2, 1, 1]
    assert sp.weekly_counts([], start, 2) == [0, 0]
    with pytest.raises(ValueError):
        sp.weekly_counts(ts, start, -1)


def test_weekly_counts_causal():
    """Counts of weeks before t do not change when events after t change."""
    start = 0
    early = [1, W + 2, 2 * W + 3]
    a = sp.weekly_counts(early + [5 * W + 1], start, 8)
    b = sp.weekly_counts(early + [4 * W, 6 * W + 9, 7 * W + 1, 7 * W + 2], start, 8)
    assert a[:4] == b[:4] == [1, 1, 1, 0]


def test_weekly_rate_t_interval():
    r = sp.weekly_rate([1, 3] * 5)
    assert r["weeks"] == 10 and r["total"] == 20 and r["mean"] == pytest.approx(2.0)
    # sd sqrt(10/9), se sd / sqrt(10) = 1/3, t(0.975, 9) = 2.2622 (Cornish-Fisher, error < 1e-3 for df >= 5)
    half = 2.2622 / 3
    assert r["lo"] == pytest.approx(2.0 - half, abs=0.01)
    assert r["hi"] == pytest.approx(2.0 + half, abs=0.01)


def test_poisson_sf():
    assert sp.poisson_sf(0, 3.0) == 1.0
    assert sp.poisson_sf(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    assert sp.poisson_sf(2, 1.0) == pytest.approx(1 - 2 * math.exp(-1.0))
    assert sp.poisson_sf(3, 0.0) == 0.0


@pytest.mark.parametrize(
    ("k", "lo", "hi"),
    [(0, 0.0, 3.6889), (1, 0.02532, 5.5716), (6, 2.2019, 13.0595)],  # chi2 quantiles / 2 (Garwood)
)
def test_poisson_ci_garwood(k, lo, hi):
    a, b = sp.poisson_ci(k)
    assert a == pytest.approx(lo, abs=2e-4)
    assert b == pytest.approx(hi, abs=2e-4)


@pytest.mark.parametrize(
    ("text", "cat"),
    [
        ("stop 18.1% from the entry > 10%", "stop_wide"),
        ("reward/risk 1.21 after fees < 1.5", "rr_low"),
        ("score 0 < 1", "score_low"),
        ("price 1.2 already at the resting level 1.3: post-only would take", "at_level"),
        ("25 open positions = max", "positions_max"),
        ("3 S40 trades at risk today: its 3 slots are taken and it never takes the reserve", "setup_slots"),
        ("S38 entries on this 4h bar unknown (book not read) = S38 cap 2 per bar", "bar_cap"),
        ("30 entries today = max", "day_caps"),
        ("open risk 900 + 100 USD > 1000", "risk_caps"),
        ("already in BTCUSDT", "already_in"),
        ("this signal was already decided", "decided"),
        ("lower rank", "rank"),
        ("something new", "other"),
    ],
)
def test_reason_category(text, cat):
    assert sp.reason_category(text) == cat


def _row(ts, kind, lid, viol=None):
    return {"ts": ts, "kind": kind, "link_id": lid, "payload": {"violations": viol} if viol is not None else {}}


def test_signal_decisions_rules():
    rows = [
        _row(1, "signal", "a"),
        _row(2, "sent", "a"),
        _row(1, "signal", "b"),
        _row(2, "refused", "b", ["stop 12.0% from the entry > 10%", "this signal was already decided"]),
        _row(3, "skipped", "b", ["25 open positions = max"]),  # a later row does not replace the first decision
        _row(1, "signal", "c"),
        _row(2, "skipped", "c", ["this signal was already decided", "score 0 < 1"]),
        _row(1, "signal", "d"),  # no decision row
        _row(2, "skipped", "e", ["score 0 < 1"]),  # no signal row: not an evaluated signal
    ]
    assert sp.signal_decisions(rows, 10) == {"a": "entered", "b": "stop_wide", "c": "score_low", "d": "no_decision"}


def test_signal_decisions_causal():
    """Decisions as of t do not change when rows at or after t change."""
    early = [_row(1, "signal", "a"), _row(2, "skipped", "a", ["score 0 < 1"]), _row(3, "signal", "b")]
    x = sp.signal_decisions(early + [_row(5, "sent", "a"), _row(6, "signal", "z")], 5)
    y = sp.signal_decisions(early + [_row(7, "refused", "b", ["already in X"])], 5)
    assert x == y == {"a": "score_low", "b": "no_decision"}


def test_days_to_reach_and_prob_reach():
    assert sp.days_to_reach(30, 6, 0.5) == pytest.approx(48.0)
    assert sp.days_to_reach(30, 31, 0.5) == 0.0
    assert math.isinf(sp.days_to_reach(30, 6, 0.0))
    assert sp.prob_reach(1, 0.25, 4.0) == pytest.approx(1 - math.exp(-1.0))
    assert sp.prob_reach(5, 1.0, 0.0) == 0.0
