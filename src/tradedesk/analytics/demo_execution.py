"""Execution quality of the demo trades (IDEA-20261102-standing-improvements, item 3): how far the demo's fills, fees
and stop exits are from the cost assumptions of the setup backtests, in R.

Pure functions, no I/O, time is a parameter (``now_ms``: only trades closed by then count).

One closed ``demo_trade`` row (dict of its columns) gives:

- **Entry.** ``exec_edge_r = dir * (entry - fill) / risk_unit``: the fill against the demo's own planned entry (+ = the
  fill was better). ``latency_min``: minutes from the signal-bar close to the fill. ``entry_kind``: maker / taker / mixed
  (``None`` = written before the column, 22.10).
- **Fees.** ``fee_r`` = the venue's open + close fees / ``risk_usd``; ``model_fee_r`` = the backtest model of
  ``stop_management`` (maker ``MAKER`` on the entry and a target exit, taker ``TAKER`` on a stop, break-even or time
  exit) on the same quantity and prices; ``fee_gap_r = model_fee_r - fee_r`` (+ = cheaper than the model).
- **Stop exit.** For a ``stop`` / ``breakeven`` exit: ``stop_slip_r = dir * (exit - level) / risk_unit`` with level the
  break-even stop when it was moved, else the stop; the model's slippage is ``-SLIPPAGE * level / risk_unit``;
  ``stop_gap_r = stop_slip_r - model_slip_r`` (+ = better than the model). ``hours_to_close``: fill to close.
- **P&L check.** ``pnl_gap_r = (pnl_usd - (dir * qty * (exit - fill) - fees_usd)) / risk_usd``: the journal's P&L
  against its own prices; a large gap means the closed-pnl row belongs to another position or misses a part.
- **Gap.** ``gap_r = exec_edge_r + fee_gap_r + stop_gap_r`` (each missing part counts as 0): the execution difference
  between the demo and the backtest's cost model, R per trade, + = the demo did better.

R unit = ``risk_usd`` of the row (the demo's risk: distance from the fill to the stop plus both taker fees, times the
quantity), so ``risk_unit = risk_usd / qty`` per unit of the base. Signs: + is always in favour of the trade.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .setup_passport import mean_ci as cluster_boot_ci
from .stop_management import MAKER, SLIPPAGE, TAKER

DAY_MS = 86_400_000
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
MATERIAL_R = 0.05  # registered material threshold, R per trade
PNL_FLAG_R = 0.05  # registered P&L consistency flag, R
TAKER_EXITS = ("stop", "breakeven", "time")
STOP_EXITS = ("stop", "breakeven")


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def model_fee_usd(qty: float, fill: float, exit_px: float, exit_reason: str | None) -> float:
    """Backtest fee model in USD: maker entry; maker exit at the target, taker otherwise (stop, break-even, time)."""
    exit_rate = MAKER if exit_reason == "target" else TAKER
    return qty * (MAKER * abs(fill) + exit_rate * abs(exit_px))


def trade_metrics(row: Mapping[str, Any], now_ms: int) -> dict[str, Any] | None:
    """Execution metrics of one closed demo trade (module doc); None when it is not closed by ``now_ms`` or lacks the
    numbers (quantity, fill, exit, risk)."""
    if row.get("status") != "closed":
        return None
    closed = row.get("closed_ts")
    if closed is None or int(closed) > now_ms:
        return None
    qty, fill, exit_px, risk = _f(row.get("qty")), _f(row.get("fill")), _f(row.get("exit")), _f(row.get("risk_usd"))
    if not qty or not fill or not exit_px or not risk or qty <= 0 or risk <= 0:
        return None
    d = 1.0 if row.get("side") == "long" else -1.0
    ru = risk / qty
    entry, stop = _f(row.get("entry")), _f(row.get("stop"))
    reason = row.get("exit_reason")
    out: dict[str, Any] = {
        "link_id": row.get("link_id"),
        "symbol": row.get("symbol"),
        "setup": row.get("setup"),
        "timeframe": row.get("timeframe"),
        "side": row.get("side"),
        "signal_ts": int(row.get("signal_ts") or 0),
        "closed_ts": int(closed),
        "exit_reason": reason,
        "entry_kind": row.get("entry_kind"),
        "exit_kind": row.get("exit_kind"),
        "r_multiple": _f(row.get("r_multiple")),
        "fill": fill,
        "risk_unit": ru,
    }
    out["exec_edge_r"] = None if entry is None else d * (entry - fill) / ru
    tf = TF_MS.get(str(row.get("timeframe")))
    opened = row.get("opened_ts")
    out["latency_min"] = None if tf is None or opened is None else (int(opened) - (out["signal_ts"] + tf)) / 60_000
    out["hours_to_close"] = None if opened is None else (int(closed) - int(opened)) / 3_600_000
    fees = _f(row.get("fees_usd"))
    out["fee_r"] = None if fees is None else fees / risk
    out["model_fee_r"] = model_fee_usd(qty, fill, exit_px, reason) / risk
    out["fee_gap_r"] = None if fees is None else out["model_fee_r"] - out["fee_r"]
    be_stop = _f(row.get("be_stop"))
    level = be_stop if be_stop is not None and row.get("be_ts") is not None else stop
    if reason in STOP_EXITS and level is not None:
        out["stop_level"] = level
        out["stop_slip_r"] = d * (exit_px - level) / ru
        out["model_slip_r"] = -SLIPPAGE * abs(level) / ru
        out["stop_gap_r"] = out["stop_slip_r"] - out["model_slip_r"]
    else:
        out["stop_level"] = out["stop_slip_r"] = out["model_slip_r"] = out["stop_gap_r"] = None
    target = _f(row.get("target"))
    out["target_slip_r"] = d * (exit_px - target) / ru if reason == "target" and target is not None else None
    pnl = _f(row.get("pnl_usd"))
    out["pnl_gap_r"] = None if pnl is None or fees is None else (pnl - (d * qty * (exit_px - fill) - fees)) / risk
    out["pnl_flag"] = out["pnl_gap_r"] is not None and abs(out["pnl_gap_r"]) > PNL_FLAG_R
    out["gap_r"] = sum(x for x in (out["exec_edge_r"], out["fee_gap_r"], out["stop_gap_r"]) if x is not None)
    return out


def stress_gap(m: Mapping[str, Any], extra: float) -> float:
    """``gap_r`` of a ``trade_metrics`` row with ``extra`` (decimal of the price) more slippage on every market side:
    a stop / break-even exit (at its level) and a taker entry (at the fill)."""
    g = float(m["gap_r"])
    if m.get("stop_level") is not None:
        g -= extra * abs(m["stop_level"]) / m["risk_unit"]
    if m.get("entry_kind") == "taker":
        g -= extra * abs(m["fill"]) / m["risk_unit"]
    return g


def metrics(rows: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """``trade_metrics`` of every row closed by ``now_ms``, in closing order."""
    out = [m for m in (trade_metrics(r, now_ms) for r in rows) if m is not None]
    return sorted(out, key=lambda m: (m["closed_ts"], str(m["link_id"])))


def t975(df: int) -> float:
    """Two-sided 95 % Student t quantile (table to 30, then a normal-corrected approximation)."""
    table = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131)
    table += (2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
    if df < 1:
        return math.nan
    if df <= 30:
        return table[df - 1]
    z = 1.959964
    return z + (z**3 + z) / (4 * df)


def describe(values: Sequence[float], days: Sequence[int], *, n_boot: int = 10_000, seed: int = 0) -> dict[str, Any]:
    """n, mean, median, sd, t interval, iid bootstrap and day-cluster bootstrap intervals (95 %), and the n the t interval
    needs for a half-width of ``MATERIAL_R`` at this sd."""
    v = [float(x) for x in values]
    n = len(v)
    out: dict[str, Any] = {"n": n, "mean": None, "median": None, "sd": None, "t": None, "boot": None, "day_boot": None}
    out["n_days"] = len(set(days))
    out["n_needed"] = None
    if n == 0:
        return out
    m = math.fsum(v) / n
    s = sorted(v)
    out["mean"] = m
    out["median"] = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    if n < 2:
        return out
    sd = math.sqrt(math.fsum((x - m) ** 2 for x in v) / (n - 1))
    out["sd"] = sd
    h = t975(n - 1) * sd / math.sqrt(n)
    out["t"] = (m - h, m + h)
    b = cluster_boot_ci(v, list(range(n)), n_boot=n_boot, seed=seed)
    out["boot"] = (b["lo"], b["hi"])
    c = cluster_boot_ci(v, list(days), n_boot=n_boot, seed=seed)
    out["day_boot"] = None if math.isnan(c["lo"]) else (c["lo"], c["hi"])
    out["n_needed"] = math.ceil((1.959964 * sd / MATERIAL_R) ** 2)
    return out


def verdict(mean: float | None, ci: tuple[float, float] | None, material: float = MATERIAL_R) -> str:
    """Registered verdict of a gap (+ = the demo did better than the backtest's model):

    ``✓`` the CI lies inside [-material, +material]; ``✗-`` the backtest is optimistic (CI below 0, mean <= -material);
    ``✗+`` pessimistic (CI above 0, mean >= material); ``≈`` the CI excludes 0 but |mean| < material;
    ``◯`` the power gate fails (the CI holds 0 and is wider than ±material).

    Priority (the registration did not set one; fixed here after the review): ``✓`` wins over ``≈`` when the CI lies
    inside ±material and also excludes 0, e.g. (-0.009, [-0.014, -0.006]) is ``✓``: a systematic but immaterial shift,
    which the report states next to the mark."""
    if mean is None or ci is None or any(math.isnan(x) for x in ci):
        return "◯"
    lo, hi = ci
    if lo >= -material and hi <= material:
        return "✓"
    if hi < 0:
        return "✗-" if mean <= -material else "≈"
    if lo > 0:
        return "✗+" if mean >= material else "≈"
    return "◯"


def day_of(ms: int) -> int:
    return ms // DAY_MS


def summarise(ms: Sequence[Mapping[str, Any]], key: str, **kw: Any) -> dict[str, Any]:
    """``describe`` of ``key`` over the rows that have it (clusters: the UTC day of the signal) plus its verdict on the
    day-cluster interval (the t interval when the clusters are too few)."""
    pick = [m for m in ms if m.get(key) is not None]
    d = describe([m[key] for m in pick], [day_of(m["signal_ts"]) for m in pick], **kw)
    d["verdict"] = verdict(d["mean"], d["day_boot"] or d["t"])
    return d


def decision_edges(
    signals: Sequence[Mapping[str, Any]], taken: set[str], now_ms: int, *, rr_min: float = 1.5, skip_setups: tuple[str, ...] = ("S12",)
) -> list[dict[str, Any]]:
    """Selection check of the demo's entry (exploratory, added after the review): for every audited signal
    (``exec_audit`` row of kind ``signal``: ``ts``, ``link_id``, ``setup``, ``payload`` with ``signal`` {side, entry,
    stop} and ``proposal`` {entry, rr}) logged by ``now_ms``, the edge of the demo's planned entry (the last price at
    the decision) against the signal's entry, in the signal's risk:
    ``edge_r = dir * (signal.entry - proposal.entry) / |signal.entry - signal.stop|`` (+ = the decision price is better).

    ``group``: ``taken`` (the link has a ``demo_trade`` row), ``refused_rr`` (not taken and the proposal's reward/risk
    < ``rr_min``), ``refused_other``. First row per link only; ``skip_setups`` (S12 enters at the gap middle by design)
    and rows without the numbers are dropped. ``signal_ts`` is the audit time (the day cluster of ``summarise``)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in signals:
        ts, lid = row.get("ts"), row.get("link_id")
        if ts is None or int(ts) > now_ms or lid is None or lid in seen:
            continue
        seen.add(lid)
        if row.get("setup") in skip_setups:
            continue
        p = row.get("payload") or {}
        s, pr = p.get("signal") or {}, p.get("proposal") or {}
        e0, stop, e1 = _f(s.get("entry")), _f(s.get("stop")), _f(pr.get("entry"))
        if e0 is None or stop is None or e1 is None or s.get("side") not in ("long", "short") or e0 == stop:
            continue
        d = 1 if s["side"] == "long" else -1
        rr = _f(pr.get("rr")) or 0.0
        group = "taken" if lid in taken else ("refused_rr" if rr < rr_min else "refused_other")
        out.append(
            {
                "link_id": lid,
                "setup": row.get("setup"),
                "side": s["side"],
                "signal_ts": int(ts),
                "group": group,
                "rr": rr,
                "edge_r": d * (e0 - e1) / abs(e0 - stop),
            }
        )
    return out
