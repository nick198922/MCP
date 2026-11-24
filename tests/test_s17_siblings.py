import math

import numpy as np

from tradedesk.analytics import s17_siblings as s


def test_group_ids_and_sizes():
    keys = [("A", "long", 1), ("A", "long", 1), ("B", "short", 1), ("A", "long", 2), ("A", "long", 1)]
    g = s.group_ids(keys)
    assert g.tolist() == [0, 0, 1, 2, 0]
    assert s.group_sizes(g).tolist() == [3, 3, 1, 1, 3]
    assert s.group_sizes(np.array([], dtype=np.int64)).size == 0


def test_level_distance_long_and_short():
    assert math.isclose(s.level_distance("long", 95.0, 100.0), 0.05)
    assert math.isclose(s.level_distance("short", 104.0, 100.0), 0.04)
    assert s.level_distance("long", 101.0, 100.0) < 0  # price already through the level
    assert math.isnan(s.level_distance("long", 95.0, 0.0))


def test_rank_within_ties_and_nan():
    g = np.array([0, 0, 0, 1, 1, 2, 2, 2])
    v = [0.3, 0.1, 0.2, 0.5, 0.5, math.nan, 0.4, 0.1]
    assert s.rank_within(g, v).tolist() == [3, 1, 2, 1, 1, 0, 2, 1]


def test_rank_within_causal_per_key():
    """Ranks of a key do not move when rows of other (later) keys change or are appended."""
    g = np.array([0, 0, 1, 1])
    base = s.rank_within(g, [0.2, 0.1, 0.3, 0.4])
    g2 = np.array([0, 0, 1, 1, 2])
    later = s.rank_within(g2, [0.2, 0.1, 0.9, 0.05, 7.0])
    assert later[:2].tolist() == base[:2].tolist() == [2, 1]


def test_match_subsequence():
    sigs = [("long", 1, 0.05), ("long", 1, 0.07), ("short", 1, 0.03), ("long", 2, 0.05), ("long", 2, 0.05)]
    rows = [("long", 1, 0.07), ("long", 2, 0.05), ("long", 2, 0.05)]
    assert s.match_subsequence(rows, sigs).tolist() == [1, 3, 4]
    assert s.match_subsequence([("long", 9, 0.01)], sigs).tolist() == [-1]
    assert s.match_subsequence([("long", 1, 0.05 + 1e-12)], sigs).tolist() == [0]


def test_parse_break_close():
    c = "через 3 св. після імпульсу ця свічка закрилась на 1,234.5, вище хаю POB (тіло ≥ 1 ATR)."
    assert s.parse_break_close(c) == 1234.5
    assert s.parse_break_close("ця свічка закрилась на 104,321, нижче лоу") == 104321.0
    assert s.parse_break_close("ця свічка закрилась на 1.2345e-05, вище") == 1.2345e-05
    assert s.parse_break_close("no close here") is None


def test_pick_comparison():
    g = np.array([0, 0, 1, 1, 2, 2, 3, 3])
    a = np.array([1, 0, 1, 0, 1, 0, 0, 0], dtype=bool)
    b = np.array([1, 0, 0, 1, 0, 0, 0, 1], dtype=bool)
    r = s.pick_comparison(g, a, b)
    assert r == {"groups": 4, "both": 2, "differ": 1, "only_a": 1, "only_b": 1, "neither": 0, "multi": 0}
