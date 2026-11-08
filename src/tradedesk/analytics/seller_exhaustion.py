"""Seller Exhaustion Constant (SEC) of BTC as a short veto (IDEA-20261030-demo-setups-depth, item «виснаження продавців
BTC забороняє шорти»; registration «Попередня реєстрація — виснаження продавців забороняє шорти»; source
LIB-20261104-seller-exhaustion).

Pure functions, no I/O; time is a parameter (ms, UTC day opens).

* `daily_series`: a Bitview ``day1`` array (index 0 = ``start_ms``) as {day open ms: value}, non-finite values skipped;
* `sec_series`: SEC of day k = share in profit (percent) / 100 * pstdev(30 daily log returns ending at day k) * sqrt(30);
  the returns of days k-29 .. k need the closes of days k-30 .. k (31 consecutive days), otherwise the day has no SEC;
  nothing after day k is used;
* `sec_sign`: +1 on a day with SEC < threshold (shorts vetoed, longs kept), 0 otherwise or without SEC (no effect);
* `short_masks`: (kept, dropped) masks over the shorts only: a short on a +1 day is dropped, on a 0 day kept; longs
  are in neither mask (the veto does not act on them);
* `long_masks`: the descriptive split of the longs by the same sign (low-SEC longs vs the rest).

Decisions use `regime_side.sign_before`: the sign of the last UTC day whose bar has closed by the decision time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

DAY_MS = 86_400_000
WINDOW = 30
THRESHOLD = 0.05  # Glassnode / the post's level, fixed in the registration (not tuned)


def daily_series(values: Sequence[float | None], start_ms: int) -> dict[int, float]:
    """{day open ms: value} for a daily array whose index 0 is the UTC day opening at ``start_ms``."""
    out: dict[int, float] = {}
    for i, v in enumerate(values):
        if v is None:
            continue
        x = float(v)
        if math.isfinite(x):
            out[int(start_ms) + i * DAY_MS] = x
    return out


def sec_series(share_pct: Mapping[int, float], price: Mapping[int, float], window: int = WINDOW) -> dict[int, float]:
    """{day open ms: SEC} for every day that has a share and ``window`` + 1 consecutive positive daily prices ending at
    it. SEC = share / 100 * pstdev(window log returns) * sqrt(window)."""
    out: dict[int, float] = {}
    for t, sh in share_pct.items():
        closes = [price.get(t - j * DAY_MS) for j in range(window, -1, -1)]
        if any(c is None or not c > 0 for c in closes):
            continue
        lr = np.diff(np.log(np.asarray(closes, dtype=float)))
        out[t] = float(sh) / 100.0 * float(np.std(lr)) * math.sqrt(window)
    return out


def sec_sign(sec: Mapping[int, float], threshold: float = THRESHOLD) -> dict[int, int]:
    """{day open ms: +1 if SEC < threshold else 0}."""
    return {t: (1 if math.isfinite(v) and v < threshold else 0) for t, v in sec.items()}


def short_masks(sides: Sequence[str], sign: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """(kept, dropped) over the shorts: dropped = short on a +1 day, kept = short on a 0 day; longs in neither."""
    sh = np.array([s == "short" for s in sides], dtype=bool)
    sg = np.asarray(sign, dtype=np.int64)
    return sh & (sg == 0), sh & (sg == 1)


def long_masks(sides: Sequence[str], sign: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """(rest, low-SEC) over the longs: the descriptive split of the longs by the same day sign."""
    lg = np.array([s == "long" for s in sides], dtype=bool)
    sg = np.asarray(sign, dtype=np.int64)
    return lg & (sg == 0), lg & (sg == 1)
