"""S12 demo trades against the passport of the version actually traded (IDEA-20261030-demo-setups-depth, items 6 and 9;
pre-registered in the card, section «Попередня реєстрація звірки S12»).

Pure functions, no I/O, time is a parameter.

- ``traded_version``: the rules a closed S12 demo trade ran under. The ``version`` column when the trader wrote it;
  without it, a resting limit (``rest_until`` set) is v7, otherwise the epoch of the open time
  (``setup_versions.version_of``), i.e. v4 for the trades before v7 went live.
- ``PASSPORTS``: the reference of each entry rule (R of 30 closed trades, top-100 and >= $5M, OOS 2025-03+): v4 and v9
  share the v4 entry (close of the touch bar), so both are set against the v4 base of the S12 modules report
  (2026-11-01); v7 (a limit resting at the middle of a new gap) against the v7 passport (2026-10-30).
- ``exec_costs``: the fees and the exit slippage of one demo trade in the demo's risk units next to what the backtest
  convention (``stop_management.Fees``: maker entry and target, taker stop or time exit plus ``SLIPPAGE``) would charge
  on the same prices and quantity.
- ``v7_replay``: a v7 trade replayed with the live v7 detector (``s12_rest_all``) on the live window and the passport's
  limit model (``setup_passport.limit_trade``: filled only when the price trades through the level, pessimistic in the
  fill hour). ``demo_vs_backtest.replay`` uses the v4 detector, so it cannot replay a v7 trade.

R is per trade risk (the demo: venue P&L with fees / ``risk_usd``), not on capital.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .course_s14_ema import Bars
from .demo_vs_backtest import BE_FRAC, setup_window
from .setup_passport import limit_trade
from .setup_signals import TF_MS, s12_rest_all
from .setup_versions import version_of
from .stop_management import MAKER, SLIPPAGE, TAKER, Fees, StopRule


@dataclass(frozen=True)
class Passport:
    entry: str  # the entry rule the reference describes
    oos_mean: float  # E[R] net, OOS, top-100 and >= $5M
    oos_ci: tuple[float, float]
    band_iid: tuple[float, float, float]  # 5th / 50th / 95th percentile of the mean R of 30 trades (i.i.d.)
    band_sliding: tuple[float, float, float]  # the same for 30 trades in a row (sliding windows)
    fill_rate_oos: float | None  # share of placed limits that filled (limit entries only)
    source: str


PASSPORTS: dict[str, Passport] = {
    "v4": Passport(
        "вхід за закриттям свічки дотику FVG",
        -0.009,
        (-0.075, 0.058),
        (-0.440, -0.022, 0.496),
        (-0.827, -0.092, 1.095),
        None,
        "docs/verification/2026-11-01-s12-modules.md (база v4 на списку демо)",
    ),
    "v7": Passport(
        "лімітка на середині нового FVG, 20 свічок 4h",
        -0.004,
        (-0.077, 0.069),
        (-0.343, -0.014, 0.348),
        (-0.696, -0.043, 0.808),
        0.782,
        "docs/verification/2026-10-30-passport-s12v7-s28.md",
    ),
}
PASSPORT_OF = {"v4": "v4", "v9": "v4", "v7": "v7"}  # v9 = back to the v4 entry (setup_versions)


def traded_version(row: Mapping[str, Any], *, now_ms: int) -> tuple[str, str]:
    """(version, source) of an S12 demo trade: source 'column', 'rest_until' or 'open_time' ('none' when unknown).

    An untagged trade without a resting limit opened inside the v7 epoch ran the v4 entry: v7 starts when the change was
    rebased, before it was deployed, and every v7 entry sets ``rest_until`` (``setup_versions``), so it maps to v4.
    """
    tag = row.get("version")
    if tag:
        return str(tag), "column"
    if row.get("rest_until") is not None:
        return "v7", "rest_until"
    ep = version_of(row.get("setup") or "S12", row.get("opened_ts"), now_ms=now_ms)
    if ep is None:
        return "unknown", "none"
    return ("v4" if ep.tag == "v7" else ep.tag), "open_time"


def band_verdict(mean: float, p: Passport) -> str:
    """✓ inside the i.i.d. band of 30 trades, ≈ outside it but inside the sliding one, ✗ outside both."""
    if p.band_iid[0] <= mean <= p.band_iid[2]:
        return "✓ всередині i.i.d.-смуги"
    if p.band_sliding[0] <= mean <= p.band_sliding[2]:
        return "≈ поза i.i.d., всередині ковзної"
    return "✗ поза ковзною смугою"


def scaled_band(p: Passport, n: int) -> tuple[float, float]:
    """The i.i.d. band of 30 trades rescaled to ``n`` around its median (sqrt(30 / n)); descriptive only."""
    lo, mid, hi = p.band_iid
    k = (30 / n) ** 0.5
    return mid + (lo - mid) * k, mid + (hi - mid) * k


def _f(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def exec_costs(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Fees and exit slippage of one closed demo trade, in R of the demo's risk (``risk_usd``).

    ``fee_demo_r``: the venue's fees; ``fee_model_r``: the backtest convention on the same fill, exit price and quantity
    (maker entry, maker target, taker otherwise); ``fee_entry_kind_r``: the same with the entry fee of the demo's actual
    entry kind (a taker entry pays taker). ``exit_slip_r``: the exit price against its planned level (target, stop or
    break-even stop; + = better for the demo), None for a time exit; ``model_slip_r``: the backtest's taker slippage on
    that exit (0 for a target), as a negative number. Missing inputs give None.
    """
    qty, risk, fees = _f(row.get("qty")), _f(row.get("risk_usd")), _f(row.get("fees_usd"))
    fill = _f(row.get("fill")) or _f(row.get("entry"))
    exit_px = _f(row.get("exit")) or _f(row.get("exit_px"))
    reason = row.get("exit_reason")
    out: dict[str, float | None] = dict.fromkeys(
        ("fee_demo_r", "fee_model_r", "fee_entry_kind_r", "fee_diff_r", "exit_slip_r", "model_slip_r", "slip_diff_r")
    )
    if not qty or not risk or risk <= 0 or fill is None or exit_px is None:
        return out
    exit_fee = MAKER if reason == "target" else TAKER
    entry_fee = TAKER if row.get("entry_kind") == "taker" else MAKER
    out["fee_model_r"] = qty * (MAKER * fill + exit_fee * exit_px) / risk
    out["fee_entry_kind_r"] = qty * (entry_fee * fill + exit_fee * exit_px) / risk
    if fees is not None:
        out["fee_demo_r"] = fees / risk
        out["fee_diff_r"] = out["fee_model_r"] - out["fee_demo_r"]  # + = the demo paid less than the model
    level = {"target": row.get("target"), "stop": row.get("stop"), "breakeven": row.get("be_stop")}.get(str(reason))
    lv = _f(level)
    if lv is not None:
        sgn = 1.0 if row.get("side") == "long" else -1.0
        out["exit_slip_r"] = sgn * (exit_px - lv) * qty / risk
        out["model_slip_r"] = 0.0 if reason == "target" else -SLIPPAGE * exit_px * qty / risk
        out["slip_diff_r"] = out["exit_slip_r"] - out["model_slip_r"]  # + = the demo slipped less than the model
    return out


def v7_replay(
    row: Mapping[str, Any],
    bars: Bars,
    path: Bars,
    *,
    now_ms: int,
    hold_ms: int,
    window: int = 999,
    fees: Fees = Fees(),
) -> dict[str, Any]:
    """Replay of a closed v7 demo trade (module docstring). ``bars``: candles of the signal timeframe covering the live
    window; ``path``: 1h candles from the signal-bar close to the end of the hold. Keys: ``verdict`` ('replayed',
    'no_signal', 'no_data', 'not_filled'), the detector's levels and their differences from the demo's in R of the
    detector's risk, ``rest_until_diff_h``, ``r_bt`` (net R of the passport model, pessimistic in the fill hour, today's
    break-even rule), ``bt_exit`` and ``fill_bar_ms`` (open of the 1h bar the model fills in)."""
    tf = str(row.get("timeframe"))
    sig_ts = int(row["signal_ts"])
    side = str(row.get("side"))
    out: dict[str, Any] = {"verdict": "no_data"}
    if tf not in TF_MS or bars.bar_ms != TF_MS[tf]:
        return out
    win = setup_window(bars, sig_ts, now_ms, window)
    if win is None:
        return out
    same = [s for s in s12_rest_all(win, tf, lo=len(win) - 1) if s.signal_ts == sig_ts and s.side == side]
    if not same:
        return {"verdict": "no_signal"}
    s = same[0]
    risk = abs(s.entry - s.stop)
    out.update(
        bt_entry=s.entry,
        bt_stop=s.stop,
        bt_target=s.target,
        entry_diff_r=abs(float(row["entry"]) - s.entry) / risk,
        stop_diff_r=abs(float(row["stop"]) - s.stop) / risk,
        target_diff_r=abs(float(row["target"]) - s.target) / risk,
    )
    until = int(s.indicators["rest_until_ms"])
    ru = row.get("rest_until")
    out["rest_until_diff_h"] = None if ru is None else (int(ru) - until) / 3_600_000
    t0 = sig_ts + TF_MS[tf]
    keep = path.t + path.bar_ms <= now_ms
    p = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], path.bar_ms)
    sgn = 1.0 if side == "long" else -1.0
    if side != "long":
        p = p.mirrored()
    rule = StopRule("B", be_frac=BE_FRAC)
    o, tk = limit_trade(p, t0, until, sgn * s.entry, sgn * s.stop, sgn * s.target, hold_ms, rule, fees)
    if tk < 0:
        out["verdict"] = "not_filled"
        return out
    out["fill_bar_ms"] = tk
    if o is None or o.exit_kind == "data_end":
        out["verdict"] = "no_data"
        return out
    out.update(verdict="replayed", r_bt=o.r_net, bt_exit=o.exit_kind)
    return out
