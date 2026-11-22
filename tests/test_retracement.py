from __future__ import annotations

import pytest

from tradedesk.analytics.retracement import DEFAULT_LEVELS, fib_levels, summarise_retracement

# The MCP tool fib_retracement was removed 2026-11-18 (docs/changes/2026-11-18-mcp-remove-six.md); the pure ladder stays.

# ---------- pure math ----------


def test_fib_levels_bullish_drop():
    # P0=100, L=50 → 50% level = 75, 100% level pins at P0.
    rows = fib_levels(100.0, 50.0, levels=[0.0, 0.5, 1.0])
    assert [r["price"] for r in rows] == pytest.approx([50.0, 75.0, 100.0])


def test_fib_levels_bearish_rally():
    # P0=100, L=150 → 0% at L, 50% at 125, 100% at P0.
    rows = fib_levels(100.0, 150.0, levels=[0.0, 0.5, 1.0])
    assert [r["price"] for r in rows] == pytest.approx([150.0, 125.0, 100.0])


def test_default_levels_match_fibonacci():
    assert DEFAULT_LEVELS == (0.236, 0.382, 0.5, 0.618, 0.786)


def test_summarise_retracement_applicable_flag():
    big = summarise_retracement(100.0, 90.0, min_drop_pct=2.0)
    assert big["direction"] == "drop" and big["applicable"] is True
    small = summarise_retracement(100.0, 99.0, min_drop_pct=2.0)
    assert small["direction"] == "drop" and small["applicable"] is False


def test_summarise_retracement_flat_input():
    out = summarise_retracement(100.0, 100.0, min_drop_pct=0.0)
    assert out["direction"] == "flat" and out["applicable"] is False


def test_summarise_retracement_rejects_non_positive():
    assert summarise_retracement(0.0, 10.0)["applicable"] is False
    assert summarise_retracement(10.0, 0.0)["applicable"] is False
