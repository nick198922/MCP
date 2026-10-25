"""Polymarket price dislocations and their reversion (IDEA-20261025-pm-dislocation-reversion).

Pure functions, no I/O. Times are unix seconds, prices are YES probabilities in [0, 1] (0.05 = 5 pp).

* ``detect_dislocations``: causal detector on a quote series (``/prices-history`` points). A dislocation ends at the
  first point where the move against some point of the trailing window reaches ``min_move``; it uses only points up to
  that moment. Per-market de-duplication: no new event within ``dedupe_s`` after the previous end.
* Trade helpers work on data-api ``/trades`` rows (``asset``, ``side``, ``price``, ``size``, ``timestamp``,
  ``proxyWallet``). Every print is mapped to the YES token: a BUY of YES or a SELL of NO pushes YES up (+1), the
  mirror trades push it down (-1); a NO price ``x`` is the YES price ``1 - x``.
* ``flow_class``: concentrated (C) vs broad (B) one-sided flow inside the dislocation window, netting each wallet and
  excluding two-sided wallets (makers, neg-risk conversions) from "pressure".
* ``reversion``: R = -sign(move) * (p_exit - p_entry), in probability units.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

HOUR_S = 3600
DAY_S = 86_400


# ----------------------------------------------------------------------------- detector (quotes)


def detect_dislocations(
    pts: Sequence[tuple[int, float]],
    *,
    window_s: int = HOUR_S,
    min_move: float = 0.05,
    p_lo: float = 0.10,
    p_hi: float = 0.90,
    dedupe_s: int = 6 * HOUR_S,
) -> list[dict[str, Any]]:
    """Causal dislocation events on a sorted ``(t, p)`` quote series.

    At each point i the trailing window ``[t_i - window_s, t_i)`` is scanned for the point k with the largest
    ``|p_i - p_k|`` among points whose price lies in ``[p_lo, p_hi]``; an event fires at the first i where that move is
    ``>= min_move``. ``t_start``/``p_start`` are point k, ``t_end``/``p_end`` point i, ``sign`` is +1 (up) or -1.
    After an event no new one may start before ``t_end + dedupe_s``.
    """
    out: list[dict[str, Any]] = []
    blocked_until = -math.inf
    lo = 0
    eps = 1e-12
    for i, (ti, pi) in enumerate(pts):
        while lo < i and pts[lo][0] < ti - window_s:
            lo += 1
        if ti < blocked_until:
            continue
        best_k, best_d = -1, 0.0
        for k in range(lo, i):
            tk, pk = pts[k]
            if tk < blocked_until or not (p_lo <= pk <= p_hi):
                continue
            d = pi - pk
            if abs(d) > abs(best_d) + eps:
                best_k, best_d = k, d
        if best_k >= 0 and abs(best_d) >= min_move - eps:
            tk, pk = pts[best_k]
            out.append({"t_start": int(tk), "p_start": float(pk), "t_end": int(ti), "p_end": float(pi), "sign": 1 if best_d > 0 else -1})
            blocked_until = ti + dedupe_s
    return out


def placebo_times(
    pts: Sequence[tuple[int, float]],
    events: Sequence[dict[str, Any]],
    *,
    n: int,
    seed: int,
    p_lo: float = 0.10,
    p_hi: float = 0.90,
    min_gap_s: int = 6 * HOUR_S,
    window_s: int = HOUR_S,
    max_move: float = 0.05,
) -> list[int]:
    """Up to ``n`` seeded quote times in the same series that are not dislocations.

    A time qualifies if it has at least one earlier point in its trailing window, its quote is in ``[p_lo, p_hi]``, it is at least ``min_gap_s`` from every event end, and the
    trailing ``window_s`` move is below ``max_move`` (the same detector rule, negated).
    """
    import random

    ends = [int(e["t_end"]) for e in events]
    ok: list[int] = []
    lo = 0
    for i, (ti, pi) in enumerate(pts):
        while lo < i and pts[lo][0] < ti - window_s:
            lo += 1
        if lo == i or not (p_lo <= pi <= p_hi) or any(abs(ti - te) < min_gap_s for te in ends):  # needs a trailing window
            continue
        if any(abs(pi - pts[k][1]) >= max_move for k in range(lo, i)):
            continue
        ok.append(int(ti))
    rng = random.Random(seed)
    return sorted(rng.sample(ok, min(n, len(ok))))


# ----------------------------------------------------------------------------- trades (prints)


def yes_prints(trades: Iterable[dict[str, Any]], yes_token: str) -> list[dict[str, Any]]:
    """Normalise ``/trades`` rows to the YES token: ``t``, ``p`` (YES price), ``usd`` (notional), ``dir`` (+1/-1), ``wallet``."""
    out = []
    for r in trades:
        try:
            t, px, sz = int(r["timestamp"]), float(r["price"]), float(r["size"])
        except (KeyError, TypeError, ValueError):
            continue
        side = str(r.get("side") or "").upper()
        if side not in ("BUY", "SELL") or not (0.0 <= px <= 1.0) or sz <= 0:
            continue
        is_yes = str(r.get("asset")) == str(yes_token)
        buy = side == "BUY"
        out.append(
            {
                "t": t,
                "p": px if is_yes else 1.0 - px,
                "usd": px * sz,
                "dir": 1 if buy == is_yes else -1,
                "wallet": str(r.get("proxyWallet") or ""),
            }
        )
    out.sort(key=lambda x: x["t"])
    return out


def notional(prints: Iterable[dict[str, Any]], t0: int, t1: int) -> float:
    """USD notional of prints with ``t0 <= t < t1``."""
    return float(sum(x["usd"] for x in prints if t0 <= x["t"] < t1))


def vwap(prints: Iterable[dict[str, Any]], t0: int, t1: int) -> float | None:
    """Notional-weighted YES price of prints with ``t0 <= t <= t1`` (shares weighted by USD), or None."""
    num = den = 0.0
    for x in prints:
        if t0 <= x["t"] <= t1:
            num += x["p"] * x["usd"]
            den += x["usd"]
    return num / den if den > 0 else None


def last_print(prints: Sequence[dict[str, Any]], t: int) -> tuple[int, float] | None:
    """The last print at or before ``t`` as ``(t, p)``."""
    best = None
    for x in prints:
        if x["t"] <= t:
            best = (x["t"], x["p"])
        else:
            break
    return best


def flow_class(
    prints: Iterable[dict[str, Any]],
    t0: int,
    t1: int,
    sign: int,
    *,
    top_k: int = 3,
    c_share: float = 0.60,
    b_min_wallets: int = 20,
    b_max_share: float = 0.20,
    two_sided: float = 0.20,
) -> dict[str, Any]:
    """Classify the flow of the dislocation window ``[t0, t1]`` as ``C`` (concentrated), ``B`` (broad) or ``mixed``.

    Per wallet: gross USD with the move (``sign``) and against it. A wallet is two-sided if its smaller side is
    ``>= two_sided`` of its gross; two-sided wallets never count as pressure. Directional volume D = sum over
    one-sided wallets of their net USD with the move (wallets net against the move add nothing).
    C: the ``top_k`` largest one-sided contributors give ``>= c_share`` of D. B: ``>= b_min_wallets`` contributing
    wallets and none above ``b_max_share`` of D. D == 0 gives ``none``.
    """
    w_with: dict[str, float] = defaultdict(float)
    w_against: dict[str, float] = defaultdict(float)
    for x in prints:
        if t0 <= x["t"] <= t1:
            (w_with if x["dir"] == sign else w_against)[x["wallet"]] += x["usd"]
    contrib: dict[str, float] = {}
    n_two = 0
    for w in set(w_with) | set(w_against):
        a, b = w_with.get(w, 0.0), w_against.get(w, 0.0)
        if min(a, b) >= two_sided * (a + b) and a + b > 0:
            n_two += 1
            continue
        if a - b > 0:
            contrib[w] = a - b
    total = sum(contrib.values())
    vals = sorted(contrib.values(), reverse=True)
    if total <= 0:
        return {"cls": "none", "D": 0.0, "top_share": None, "max_share": None, "n_wallets": 0, "n_two_sided": n_two}
    top = sum(vals[:top_k]) / total
    mx = vals[0] / total
    if top >= c_share:
        cls = "C"
    elif len(vals) >= b_min_wallets and mx <= b_max_share:
        cls = "B"
    else:
        cls = "mixed"
    return {"cls": cls, "D": total, "top_share": top, "max_share": mx, "n_wallets": len(vals), "n_two_sided": n_two}


def confirmed_by_prints(p_pre: float | None, p_last: float | None, sign: int, min_move: float = 0.05) -> bool:
    """The quote move is confirmed if the last print before the (shifted) end differs from the pre-move VWAP by
    ``>= min_move`` in the direction ``sign``."""
    if p_pre is None or p_last is None:
        return False
    return sign * (p_last - p_pre) >= min_move - 1e-12


# ----------------------------------------------------------------------------- returns and power


def reversion(sign: int, p_entry: float, p_exit: float) -> float:
    """R = -sign * (p_exit - p_entry): positive when the price moves back against the dislocation."""
    return -float(sign) * (float(p_exit) - float(p_entry))


def cluster_count(keys: Iterable[Any]) -> int:
    return len(set(keys))


def mde_clustered(sd_value: float, cluster_sizes: Sequence[int], k: float = 2.8) -> dict[str, float]:
    """MDE of a mean of equally weighted observations with clusters treated as fully correlated inside (worst case).

    The mean over n observations with cluster sizes m_g and perfect intra-cluster correlation has variance
    sd^2 * sum(m_g^2) / n^2, so n_eff = n^2 / sum(m_g^2). MDE = k * sd / sqrt(n_eff).
    """
    n = float(sum(cluster_sizes))
    s2 = float(sum(m * m for m in cluster_sizes))
    if n <= 0 or s2 <= 0:
        return {"n": 0.0, "n_eff": 0.0, "mde": math.inf}
    n_eff = n * n / s2
    return {"n": n, "n_eff": n_eff, "mde": k * sd_value / math.sqrt(n_eff)}
