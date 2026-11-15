"""Playbook rule P1 on history: the IV-vs-RV condition of the iron condor (IDEA-20261114-options-playbook, 4.8).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). The condor itself (construction, credit, settlement)
is `condor_dvol` (P1'); this module only measures the P1 condition as the weekly generator does
(`option_ideas.build_ideas`: ``iv_rv = atm_iv / realized_vol(closes)``) and as the MCP tool does
(`deribit_vrp` -> ``current.spread_vs_rv30`` = DVOL - RV30), both without look-ahead.

Units: vols are decimals (0.45 = 45 %); RV is close-to-close, annualised with 365 (`option_ideas.realized_vol`).
Daily bars are stamped with their open time (00:00 UTC); a bar is known once it closed (``stamp + 1 day <= t``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .option_ideas import realized_vol
from .put_skew import iv_at_strike
from .trend_spread import DAY_MS

RV_DAYS = 30
THRESH = 1.0  # P1: ATM IV / RV30 >= 1.0 (generator design 14.11, not fitted on this history)


def rv_known_at(day_stamps: Sequence[int], closes: Sequence[float], t_ms: int, days: int = RV_DAYS) -> float | None:
    """`option_ideas.realized_vol` of the last ``days`` returns of the daily closes known at ``t_ms`` (no look-ahead)."""
    known = [c for t, c in zip(day_stamps, closes, strict=True) if t + DAY_MS <= t_ms]
    return realized_vol(known, days)


def atm_iv_forward(exp: dict[str, Any]) -> float | None:
    """Mark IV at the expiry's forward (own mark if a strike sits exactly on F, else linear in ln(K/F))."""
    if not exp.get("strikes"):
        return None
    iv, _how = iv_at_strike(exp, exp["F"])
    return iv if iv is not None and iv > 0 else None


def iv_rv_ratio(iv: float | None, rv: float | None) -> float | None:
    """ATM IV / RV30 (the generator's ``ctx['iv_rv']``); None if either is missing or RV is not positive."""
    if iv is None or rv is None or rv <= 0:
        return None
    return iv / rv


def dvol_known_at(candles: Sequence[tuple[int, float]], t_ms: int) -> float | None:
    """Close of the last daily DVOL candle that closed by ``t_ms`` (candle stamped d closes at d + 1 day)."""
    best: tuple[int, float] | None = None
    for t, v in candles:
        if t + DAY_MS <= t_ms and (best is None or t > best[0]):
            best = (int(t), float(v))
    return best[1] if best else None


def condition_met(ratio: float | None, thresh: float = THRESH) -> bool | None:
    """P1 condition: ratio >= thresh; None when the ratio is not measurable."""
    if ratio is None:
        return None
    return ratio >= thresh
