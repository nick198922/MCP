from __future__ import annotations

import pytest

from tradedesk.analytics.backtest import run_backtest, signals_momentum
from tradedesk.tools._util import timeframe_seconds

# The MCP tool strategy_backtest_momentum was removed 2026-11-18 (docs/changes/2026-11-18-mcp-remove-six.md); the pure
# backtest stays in analytics/backtest.py.

# ---------- helpers ----------


def test_timeframe_seconds_units():
    assert timeframe_seconds("1m") == 60
    assert timeframe_seconds("4h") == 4 * 3600
    assert timeframe_seconds("1d") == 86400
    assert timeframe_seconds("1w") == 7 * 86400
    # ccxt's month is a capital M; it must not be read as one minute.
    assert timeframe_seconds("1M") == 30 * 86400
    for bad in ("1x", "h", "", "x1h"):
        with pytest.raises(ValueError):
            timeframe_seconds(bad)


# ---------- pure engine ----------


def _ramp_bars(n: int, start: float = 100.0, step: float = 1.0) -> list[list[float]]:
    """n hourly bars with a monotone-rising close: open == prev close, close = +step."""
    out = []
    px = start
    for i in range(n):
        o = px
        c = px + step
        out.append([1_700_000_000_000 + i * 3_600_000, o, c, o, c, 1.0])
        px = c
    return out


def test_momentum_signals_fire_on_up_trend():
    bars = _ramp_bars(60)
    positions = signals_momentum(bars, lookback=10, threshold_pct=5.0)
    # First 10 bars warm up; by bar 15 we are above threshold → long.
    assert positions[:10] == [0] * 10
    assert positions[-1] == 1


def test_run_backtest_long_only_wins_on_up_trend():
    bars = _ramp_bars(100)
    positions = signals_momentum(bars, lookback=10, threshold_pct=1.0)
    res = run_backtest(bars, positions, fee_bps=0.0, slippage_bps=0.0, bars_per_year=24 * 365)
    # Trend is persistent → equity > 1 and no losing trades.
    assert res["equity_final"] > 1.0
    assert res["stats"]["num_trades"] == 1  # one entry, closed at the end
    assert res["stats"]["win_rate"] == 1.0


def test_run_backtest_costs_eat_into_pnl():
    bars = _ramp_bars(100)
    positions = signals_momentum(bars, lookback=10, threshold_pct=1.0)
    cheap = run_backtest(bars, positions, fee_bps=0.0, slippage_bps=0.0)
    costly = run_backtest(bars, positions, fee_bps=20.0, slippage_bps=10.0)
    assert costly["equity_final"] < cheap["equity_final"]


def test_run_backtest_rejects_mismatched_lengths():
    bars = _ramp_bars(20)
    with pytest.raises(ValueError):
        run_backtest(bars, positions=[0] * 10)


def test_run_backtest_stats_block_shape():
    bars = _ramp_bars(50)
    positions = signals_momentum(bars, lookback=5, threshold_pct=1.0)
    res = run_backtest(bars, positions, bars_per_year=24 * 365)
    s = res["stats"]
    for key in ("count", "total_return", "mean_bar_return", "stdev_bar_return", "max_drawdown", "num_trades"):
        assert key in s
