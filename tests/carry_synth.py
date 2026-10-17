"""Deterministic synthetic market data for the carry backtests and the paper engine (no network).

Shapes match ``simulate_carry_portfolio`` (A) and ``simulate_perp_spread_portfolio`` (B) inputs. Funding events are
stamped a few hundred ms after the 8h grid, as real venues do; funding regimes switch every 20 days so both entries
and exits happen.
"""

from __future__ import annotations

import random
from typing import Any

H4 = 4 * 3_600_000
H8 = 8 * 3_600_000
DAY = 86_400_000
T0 = 1_700_000_000_000 // H8 * H8


def _funding(rng: random.Random, n: int, regime: list[float]) -> list[dict[str, Any]]:
    out, level = [], 0.0
    for i in range(n):
        if i % 60 == 0:
            level = rng.choice(regime)
        out.append({"ts_ms": T0 + i * H8 + rng.randint(0, 900), "rate": level + rng.gauss(0, 0.00003)})
    return out


def _bars(rng: random.Random, n: int, p0: float, vol: float) -> list[dict[str, Any]]:
    out, prev = [], p0
    for i in range(n):
        c = prev * (1 + rng.gauss(0, vol))
        hi = max(prev, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(prev, c) * (1 - abs(rng.gauss(0, vol / 2)))
        out.append({"ts": T0 + i * H4, "close": c, "high": hi, "low": lo})
        prev = c
    return out


def markets_a(seed: int = 1, nsym: int = 12, days: int = 60) -> dict[str, dict[str, Any]]:
    rng = random.Random(seed)
    mk = {}
    for k in range(nsym):
        f = _funding(rng, days * 3, [-0.0001, 0.0, 0.0002, 0.0004, 0.0008])
        sp = _bars(rng, days * 6, 10 + k, 0.01)
        pr = [dict(b, close=b["close"] * (1 + 0.002 * rng.random()), high=b["high"] * 1.003) for b in sp]
        mk[f"S{k:02d}/USDT:USDT"] = {"funding": f, "spot": [{"ts": b["ts"], "close": b["close"]} for b in sp], "perp": pr}
    return mk


def markets_b(seed: int = 2, nsym: int = 10, days: int = 60) -> dict[str, dict[str, Any]]:
    rng = random.Random(seed)
    mk = {}
    for k in range(nsym):
        fa = _funding(rng, days * 3, [0.0, 0.0001, 0.0002])
        fb = _funding(rng, days * 3, [-0.0002, 0.0, 0.0003, 0.0009])
        ba = _bars(rng, days * 6, 5 + k, 0.02)
        bb = [
            dict(
                b,
                close=b["close"] * (1 + rng.gauss(0, 0.002)),
                high=b["high"] * (1 + abs(rng.gauss(0, 0.004))),
                low=b["low"] * (1 - abs(rng.gauss(0, 0.004))),
            )
            for b in ba
        ]
        mk[f"S{k:02d}/USDT:USDT"] = {"a": {"funding": fa, "bars": ba}, "b": {"funding": fb, "bars": bb}}
    return mk
