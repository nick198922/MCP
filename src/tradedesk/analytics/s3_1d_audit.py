"""S3 on 1d: the demo's run of full stops against the passport (IDEA-20261030-demo-setups-depth, item «S3 на 1d,
звірка демо з паспортом»; pre-registered in the card, section «Попередня реєстрація звірки S3 1d»).

Pure helpers, no I/O: which exits are a full stop, the binomial tail of k stops in n trades, the day clusters of the
demo's signals before a cutoff, the probability that clusters of the same sizes drawn from the passport's signal days
are all full stops (hypergeometric within a day, days independent), and a quantile table. Rates are decimals.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

DAY_MS = 86_400_000
FULL_STOP = ("stop", "stop_gap")  # stop_management exit kinds at the original stop (not break-even, not time)


def is_full_stop(kind: str | None) -> bool:
    """True for an exit at the original stop (``stop`` or a gap through it)."""
    return kind in FULL_STOP


def binom_upper_tail(p: float, n: int, k: int) -> float:
    """P(X >= k) for X ~ Binomial(n, p); 1.0 for k <= 0, 0.0 for k > n."""
    if not 0.0 <= p <= 1.0 or n < 0:
        raise ValueError("p in [0, 1] and n >= 0")
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return float(sum(math.comb(n, j) * p**j * (1.0 - p) ** (n - j) for j in range(k, n + 1)))


def day_sizes(signal_ts: Sequence[int], cutoff_ms: int, bucket_ms: int = DAY_MS) -> list[int]:
    """Sizes of the clusters (UTC days by default; ``bucket_ms`` = 7 days gives the passport's ``t // WEEK_MS``
    weeks) of the signals strictly before ``cutoff_ms``, largest first."""
    c = Counter(int(t) // bucket_ms for t in signal_ts if int(t) < cutoff_ms)
    return sorted(c.values(), reverse=True)


def all_true_windows(flags_in_time_order: Sequence[bool], n: int) -> dict[str, float]:
    """Windows of ``n`` consecutive trades: how many there are and the share in which every flag is true."""
    f = [bool(x) for x in flags_in_time_order]
    if n < 1 or len(f) < n:
        return {"windows": 0.0, "share": math.nan, "count": 0.0}
    run = cnt = 0
    for i, x in enumerate(f):
        run = run + 1 if x else 0
        if i >= n - 1 and run >= n:
            cnt += 1
    w = len(f) - n + 1
    return {"windows": float(w), "share": cnt / w, "count": float(cnt)}


def cluster_all_stop_prob(day_n: Sequence[int], day_stop: Sequence[int], sizes: Sequence[int]) -> dict[str, Any]:
    """Probability that clusters of ``sizes`` trades, each drawn from one random passport signal day with at least that
    many trades (``m`` of its ``n_d`` trades without replacement), are all full stops.

    ``day_n[d]`` trades and ``day_stop[d]`` full stops on passport day ``d``. Per size m: q_m = mean over the days with
    n_d >= m of C(s_d, m) / C(n_d, m); the clusters are independent days, so p = prod q_m. NaN when some size has no
    eligible day. ``eligible`` lists the number of days each size was drawn from."""
    if len(day_n) != len(day_stop):
        raise ValueError("day_n and day_stop differ in length")
    qs: list[float] = []
    elig: list[int] = []
    for m in sizes:
        terms = [math.comb(s, m) / math.comb(n, m) for n, s in zip(day_n, day_stop, strict=True) if n >= m]
        elig.append(len(terms))
        qs.append(sum(terms) / len(terms) if terms else math.nan)
    p = math.prod(qs) if qs and all(math.isfinite(q) for q in qs) else math.nan
    return {"p": p, "q": qs, "eligible": elig, "sizes": list(sizes)}


def first_touch(t: Sequence[int], high: Sequence[float], low: Sequence[float], side: str, t0: int, level: float) -> int | None:
    """Open time of the first bar opening at or after ``t0`` whose range reaches ``level`` from the trade's adverse
    side (a long's stop below: low <= level; a short's: high >= level). None if no bar touches it. Bars after the
    first touch never change the answer."""
    for ti, hi, lo in zip(t, high, low, strict=True):
        if int(ti) < t0:
            continue
        if (side == "long" and lo <= level) or (side == "short" and hi >= level):
            return int(ti)
    return None


def by_quantile(x: Sequence[float], y: Sequence[float], q: int = 4) -> list[dict[str, float]]:
    """Mean of ``y`` within the ``q`` quantile bins of ``x`` (edges from ``x``; ties go to the lower bin)."""
    pairs = sorted((float(a), float(b)) for a, b in zip(x, y, strict=True) if math.isfinite(a) and math.isfinite(b))
    if not pairs or q < 1:
        return []
    out: list[dict[str, float]] = []
    n = len(pairs)
    for i in range(q):
        part = pairs[(i * n) // q : ((i + 1) * n) // q]
        if not part:
            continue
        out.append({"lo": part[0][0], "hi": part[-1][0], "n": float(len(part)), "mean": sum(b for _, b in part) / len(part)})
    return out
