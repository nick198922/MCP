"""scripts/verify_pm_gap_24h_gaps.py: aggregate ranges print coarse buckets below n = 5 (standing item 49)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_pm_gap_24h_gaps import SMALL_N, agg_range  # noqa: E402

ENTRY_END = "entry->end h"
MOMENTS = "collector moments in +-1 h of +24 h"


def test_n3_prints_buckets_not_exact_bounds():
    out = agg_range(ENTRY_END, [60.1, 92.6, 75.0])  # the review's case: two of three exact values leaked
    assert out == f"{ENTRY_END} (n<{SMALL_N}) all >=48 h"
    assert "60.1" not in out and "92.6" not in out


def test_n3_spanning_buckets():
    assert agg_range("last market row, h after entry", [3.2, 30.0, 50.5]) == "last market row, h after entry (n<5) <24 h .. >=48 h"
    assert agg_range("last market row, h before end", [24.0, 47.9]) == "last market row, h before end (n<5) all 24-48 h"


def test_n3_counts_use_count_buckets():
    assert agg_range(MOMENTS, [0, 0, 0]) == f"{MOMENTS} (n<5) all 0"
    assert agg_range(MOMENTS, [0, 2, 4]) == f"{MOMENTS} (n<5) 0 .. >=3"


def test_n6_unchanged_min_max():
    v = [60.1, 92.6, 75.0, 30.04, 120.0, 49.99]
    assert agg_range(ENTRY_END, v) == f"{ENTRY_END} {min(v):.1f}-{max(v):.1f}" == f"{ENTRY_END} 30.0-120.0"
    assert agg_range(MOMENTS, [0, 0, 1, 2, 0, 0]) == f"{MOMENTS} 0.0-2.0"


def test_threshold_is_five():
    assert agg_range(ENTRY_END, [1.0, 2.0, 3.0, 4.0]).endswith("all <24 h")
    assert agg_range(ENTRY_END, [1.0, 2.0, 3.0, 4.0, 5.0]) == f"{ENTRY_END} 1.0-5.0"
