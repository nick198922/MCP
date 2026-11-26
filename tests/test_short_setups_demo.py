"""Step-4 verdict of IDEA-20261024-short-setups-demo (analytics/short_setups_demo.py): analytic expectations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from tradedesk.analytics import short_setups_demo as ss


def _s(mean: float, ci: tuple[float, float], mde: float = 0.2, n: int = 40, clusters: int = 15) -> dict:
    return {"n": n, "clusters": clusters, "mean": mean, "ci": list(ci), "mde80": mde, "p_gt0": 0.01}


def test_holm_fixed_family_of_two() -> None:
    adj = ss.holm_fixed({"S38": 0.01, "S39": 0.04})
    assert adj["S38"] == pytest.approx(0.02) and adj["S39"] == pytest.approx(0.04)
    missing = ss.holm_fixed({"S38": 0.01})  # S39 without a p enters as 1: the family stays m = 2
    assert missing["S38"] == pytest.approx(0.02) and missing["S39"] == pytest.approx(1.0)
    assert ss.holm_fixed({"S38": 0.4, "S39": 0.3}) == pytest.approx({"S39": 0.6, "S38": 0.6})  # monotone step-down


@pytest.mark.parametrize(
    ("boot", "ct", "p", "new", "want"),
    [
        (_s(0.5, (0.1, 0.9), n=29), _s(0.5, (0.1, 0.9)), 0.01, 0.3, "◯"),  # n < 30
        (_s(0.5, (0.1, 0.9), clusters=9), _s(0.5, (0.1, 0.9)), 0.01, 0.3, "◯"),  # < 10 days
        (_s(-0.4, (-0.7, 0.04)), _s(-0.4, (-0.8, 0.049)), 0.9, None, "✗"),  # +0.05R excluded by both
        (_s(-0.4, (-0.7, 0.04)), _s(-0.4, (-0.8, 0.06)), 0.9, None, "◯"),  # t interval reaches +0.05R
        (_s(-0.4, (-0.7, 0.04), mde=0.9), _s(-0.4, (-0.8, 0.04), mde=0.9), 0.9, None, "✗"),  # power gate does not block ✗
        (_s(0.03, (0.01, 0.045)), _s(0.03, (0.01, 0.048)), 0.01, 0.1, "✗"),  # significant but below +0.05R
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75)), 0.02, 0.2, "✓"),
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75)), 0.06, 0.2, "◯"),  # Holm p ≥ 0.05
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75)), None, 0.2, "◯"),  # no Holm p never gives ✓ / ≈
        (_s(0.4, (0.1, 0.7), mde=0.5), _s(0.4, (0.05, 0.75)), 0.02, 0.2, "≈"),  # underpowered
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75), mde=0.31), 0.02, 0.2, "≈"),  # max of both MDE
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75)), 0.02, -0.1, "≈"),  # new trades of the other sign
        (_s(0.4, (0.1, 0.7)), _s(0.4, (0.05, 0.75)), 0.02, None, "≈"),  # no new trades: not confirmed
        (_s(0.4, (0.1, 0.7)), _s(0.4, (-0.01, 0.75)), 0.02, 0.2, "◯"),  # one lower bound ≤ 0
        (_s(0.1, (-0.2, 0.4)), _s(0.1, (-0.3, 0.5)), 0.3, 0.2, "◯"),
    ],
)
def test_verdict_step4_table(boot: dict, ct: dict, p: float | None, new: float | None, want: str) -> None:
    assert ss.verdict_step4(boot, ct, p_holm=p, new_mean=new) == want


def test_verdict_step4_missing_ci_and_mean_gate() -> None:
    assert ss.verdict_step4({**_s(0.4, (0.1, 0.7)), "ci": None}, _s(0.4, (0.1, 0.7)), p_holm=0.01, new_mean=0.1) == "◯"
    # bootstrap CI present, the cluster-t one missing: still ◯ (both intervals are required)
    assert ss.verdict_step4(_s(0.4, (0.1, 0.7)), {**_s(0.4, (0.1, 0.7)), "ci": None}, p_holm=0.01, new_mean=0.1) == "◯"
    # mean below +0.05R with a significant CI wider than +0.05R on the upper side: ≈, not ✓
    assert ss.verdict_step4(_s(0.045, (0.001, 0.09)), _s(0.045, (0.002, 0.09)), p_holm=0.01, new_mean=0.1) == "≈"


def test_card_status() -> None:
    assert ss.card_status({"S38": "✗", "S39": "✓"}) == "confirmed"
    assert ss.card_status({"S38": "✗", "S39": "✗"}) == "rejected"
    assert ss.card_status({"S38": "✗", "S39": "◯"}) == "inconclusive"
    assert ss.card_status({"S38": "≈", "S39": "◯"}) == "inconclusive"
    assert ss.card_status({}) == "inconclusive"


def test_step4_card_status_s39_precedence() -> None:
    assert ss.S39_BLIND_DEADLINE_MS == int(datetime(2026, 11, 28, 5, tzinfo=UTC).timestamp() * 1000)
    assert ss.s39_blind(ss.S39_BLIND_DEADLINE_MS - 1) and not ss.s39_blind(ss.S39_BLIND_DEADLINE_MS)
    assert not ss.s39_blind(None)  # unknown commit time: treated as late
    both_x = {"S38": "✗", "S39": "✗"}
    assert ss.step4_card_status(both_x, True) == "rejected"
    assert ss.step4_card_status(both_x, False) == "inconclusive"  # S39 for the record only: never rejected
    assert ss.step4_card_status({"S38": "◯", "S39": "✓"}, False) == "inconclusive"
    assert ss.step4_card_status({"S38": "◯", "S39": "✓"}, True) == "confirmed"
    assert ss.step4_card_status({"S38": "✓", "S39": "◯"}, False) == "confirmed"


def test_days_to_gate() -> None:
    assert ss.days_to_gate(0.40, 23) == 41  # (0.40 / 0.30)² · 23 = 40.9
    assert ss.days_to_gate(0.60, 10) == 40  # exactly 4 · 10
    assert ss.days_to_gate(0.20, 15) == 15  # already below the gate
    assert ss.days_to_gate(None, 15) is None and ss.days_to_gate(0.5, 0) is None


def _jr(lid: str, setup: str = "S38", r: float | None = 0.5, c: int | None = 1000, status: str = "closed") -> dict:
    return {"link_id": lid, "setup": setup, "status": status, "closed_ts": c, "r_multiple": r}


def test_manifest_line_canonical() -> None:
    assert ss.manifest_line(_jr("a", r=1.25, c=1_794_000_000_000)) == "a|closed|1794000000000|1.250000"
    assert ss.manifest_line(_jr("a", r=-1e-9)) == "a|closed|1000|0.000000"  # no negative zero
    assert ss.manifest_line(_jr("a", r=None, c=None, status="missed")) == "a|missed|None|None"
    assert ss.manifest_line(_jr("a", r=0.1234564)) == ss.manifest_line(_jr("a", r=0.1234561))  # six decimals


def test_sample_manifest_hash_and_diff() -> None:
    rows = [_jr("b", r=-1.0, c=2000), _jr("a", r=0.5), _jr("z", setup="S39", r=2.0)]
    other = [_jr("m", r=None, c=None, status="missed")]
    m = ss.sample_manifest(rows, other)
    want = hashlib.sha256(b"a|closed|1000|0.500000\nb|closed|2000|-1.000000").hexdigest()
    assert m["S38"] == {"n_closed": 2, "link_ids": ["a", "b"], "sha256": want, "other_terminal": ["m|missed"]}
    assert m["S39"]["n_closed"] == 1 and m["S39"]["other_terminal"] == []
    assert ss.sample_manifest(list(reversed(rows)), other) == m  # row order does not matter
    assert ss.manifest_diff(m, m) == []
    # an R change with the same link_ids and counts: only the hash shows it, and no R value is printed
    d = ss.manifest_diff(m, ss.sample_manifest([_jr("b", r=-0.9, c=2000), *rows[1:]], other))
    assert len(d) == 1 and "S38: sha256" in d[0] and "same link_ids" in d[0] and "0.9" not in d[0] and "-1.0" not in d[0]
    d = ss.manifest_diff(m, ss.sample_manifest([*rows, _jr("c")], other))
    assert any("added ['c']" in x for x in d) and any("n_closed 2 registered, 3 now" in x for x in d)
    d = ss.manifest_diff(m, ss.sample_manifest(rows, []))
    assert d == ["S38: other terminal rows registered ['m|missed'] / now []"]
    d = ss.manifest_diff(m, ss.sample_manifest(rows[:1] + rows[2:], other))
    assert any("gone ['a']" in x for x in d) and any("sha256" in x and "same link_ids" not in x for x in d)
    assert ss.manifest_diff({"S38": m["S38"]}, m) == ["S39: not registered"]


def test_regime_at_uses_closed_bars_only() -> None:
    day = 86_400_000
    t = [0, day, 2 * day, 3 * day]
    dirs = [1, -1, 0, 1]
    assert ss.regime_at(t, dirs, 2 * day, day) == -1  # bar of day 1 closes exactly at 2·day
    assert ss.regime_at(t, dirs, 2 * day - 1, day) == 1
    assert ss.regime_at(t, dirs, day - 1, day) == 0  # nothing closed yet
    # later bars (and their directions) do not change an earlier answer
    assert ss.regime_at([*t, 4 * day, 5 * day], [*dirs, -1, -1], 2 * day + 5, day) == -1
    assert ss.regime_at(t[:2], dirs[:2], 2 * day + 5, day) == -1


def test_best_cluster_and_without_top() -> None:
    assert ss.best_cluster([1.0, 2.0, -5.0, 2.5], ["a", "a", "b", "c"]) == "a"  # 3.0 > 2.5
    assert ss.best_cluster([1.0, 1.0], [2, 1]) == 1  # tie: smallest key
    assert ss.best_cluster([], []) is None
    assert ss.without_top([0.1, 3.0, -1.0, 3.0, 2.0], 2) == [0, 2, 4]
    assert ss.without_top([1.0, 2.0], 5) == [] and ss.without_top([1.0], 0) == [0]
