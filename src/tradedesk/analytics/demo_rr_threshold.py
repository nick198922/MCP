"""Does the demo guard's reward/risk-after-fees floor (1.5) cut profitable signals? (BACKLOG 0.5 (a), 29.10)

Works on the shadow rows of ``analytics.demo_shadow`` (as dicts, e.g. the ``rows`` of the ``--json`` file of
``scripts/demo_vs_backtest.py``): the refusals for 'reward/risk N after fees < 1.5' against the shadow plans of the
trades the demo filled ('taken'). Pure functions, no I/O; time is a parameter (``now_ms``: rows decided after it are
ignored, so results up to t never depend on later rows).

R is the row's ``r`` (live risk unit of the demo, no break-even). Means come with cluster-robust (CR1) standard errors:
plans of one day share the market move and one symbol can be refused several times, so rows are not independent.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any

from .oi_flush import cluster_se_diff, cluster_se_mean
from .smart_money import t975

RR_REASON = "reward/risk N after fees < 1.5"
_RR = re.compile(r"reward/risk\s+(-?\d+(?:\.\d+)?)\s+after fees")
DAY_MS = 86_400_000
BUCKETS = ((None, 1.0, "< 1.0"), (1.0, 1.2, "1.0–1.2"), (1.2, 1.5, "1.2–1.5"), (1.5, None, "≥ 1.5"))
Z_MDE = 2.8  # two-sided 5 %, power 80 %: 1.96 + 0.84


def rr_from_message(msg: object) -> float | None:
    """The measured reward/risk of a guard message 'reward/risk 1.19 after fees < 1.5', else None."""
    m = _RR.search(str(msg or ""))
    return float(m.group(1)) if m else None


def rr_bucket(rr: float | None) -> str:
    """Bucket label of a reward/risk value (lower edge inclusive); 'unknown' for None."""
    if rr is None:
        return "unknown"
    for lo, hi, label in BUCKETS:
        if (lo is None or rr >= lo) and (hi is None or rr < hi):
            return label
    return "unknown"


def first_decisions(rows: Iterable[Mapping[str, Any]], *, now_ms: int) -> list[Mapping[str, Any]]:
    """Rows decided by ``now_ms``, one per signal (``link_id``): the earliest decision of a signal refused in several
    cycles. Rows without a link id are kept each. Sorted by decision time."""
    best: dict[str, Mapping[str, Any]] = {}
    loose: list[Mapping[str, Any]] = []
    for r in rows:
        ts = r.get("decided_ts")
        if not isinstance(ts, int) or ts > now_ms:
            continue
        link = r.get("link_id")
        if not link:
            loose.append(r)
            continue
        cur = best.get(str(link))
        if cur is None or ts < int(cur["decided_ts"]):
            best[str(link)] = r
    return sorted([*best.values(), *loose], key=lambda r: int(r["decided_ts"]))


def day_key(ts_ms: int) -> int:
    """UTC day number of a unix-ms time."""
    return ts_ms // DAY_MS


def mean_ci(values: Sequence[float], clusters: Sequence[Hashable] | None = None) -> dict[str, Any]:
    """Mean with a 95 % t interval: iid (``clusters`` None, df n - 1) or cluster-robust CR1 (df G - 1)."""
    n = len(values)
    out: dict[str, Any] = {"n": n, "g": None, "mean": None, "se": None, "lo": None, "hi": None}
    if n == 0:
        return out
    m = math.fsum(values) / n
    out["mean"] = m
    if clusters is None:
        if n < 2:
            return out
        se, df = statistics.stdev(values) / math.sqrt(n), n - 1
        out["g"] = n
    else:
        g = len(set(clusters))
        out["g"] = g
        se_c = cluster_se_mean(values, clusters)
        if se_c is None:
            return out
        se, df = se_c, g - 1
    h = t975(df) * se
    out.update(se=se, lo=m - h, hi=m + h)
    return out


def diff_ci(a: Sequence[float], ca: Sequence[Hashable], b: Sequence[float], cb: Sequence[Hashable]) -> dict[str, Any]:
    """mean(a) - mean(b) with a CR1 interval over shared clusters (df G - 1, G = clusters of both groups)."""
    out: dict[str, Any] = {"diff": None, "se": None, "lo": None, "hi": None, "g": len(set(ca) | set(cb))}
    if not a or not b:
        return out
    d = math.fsum(a) / len(a) - math.fsum(b) / len(b)
    out["diff"] = d
    se = cluster_se_diff(a, ca, b, cb)
    if se is None:
        return out
    h = t975(out["g"] - 1) * se
    out.update(se=se, lo=d - h, hi=d + h)
    return out


def widest(cis: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The interval with the largest width among those that have one (the decision uses the widest)."""
    have = [c for c in cis if c.get("lo") is not None]
    if not have:
        return cis[0] if cis else {}
    return max(have, key=lambda c: c["hi"] - c["lo"])


def mde(se: float | None) -> float | None:
    """Minimum detectable mean (two-sided 5 %, power 80 %) for a standard error."""
    return None if se is None else Z_MDE * se


def n_needed(se: float | None, n: int, effect: float) -> int | None:
    """Sample size at which an effect of ``effect`` R becomes detectable, keeping the per-row effective SD
    (se * sqrt(n), the design effect included) fixed."""
    if se is None or n <= 0 or effect <= 0:
        return None
    return math.ceil((Z_MDE * se * math.sqrt(n) / effect) ** 2)


def open_bounds(resolved: Sequence[float], open_rr: Sequence[float | None], stop_r: float = -1.0) -> dict[str, Any]:
    """Mean R if every open plan ended at its stop (``stop_r``) or at its target (+its reward/risk; an unknown one
    counts as 1.0): the range the open plans can move the mean in."""
    worst = [*resolved, *(stop_r for _ in open_rr)]
    best = [*resolved, *((1.0 if x is None else x) for x in open_rr)]
    return {
        "n_open": len(open_rr),
        "worst": math.fsum(worst) / len(worst) if worst else None,
        "best": math.fsum(best) / len(best) if best else None,
    }


def without_top(values: Sequence[float], k: int) -> float | None:
    """Mean after dropping the ``k`` largest values (the best contributors)."""
    rest = sorted(values)[: max(0, len(values) - k)]
    return math.fsum(rest) / len(rest) if rest else None


def between_ss(values: Sequence[float], labels: Sequence[Hashable]) -> float:
    """Between-group sum of squares sum_i n_i (m_i - m)^2: the spread of per-label means."""
    m = math.fsum(values) / len(values) if values else 0.0
    groups: dict[Hashable, list[float]] = {}
    for v, lab in zip(values, labels, strict=True):
        groups.setdefault(lab, []).append(v)
    return math.fsum(len(g) * (math.fsum(g) / len(g) - m) ** 2 for g in groups.values())


def label_permutation_p(values: Sequence[float], labels: Sequence[Hashable], *, n_perm: int = 10_000, seed: int = 20261029) -> float | None:
    """Permutation p of ``between_ss``: labels (symbols) shuffled across rows, counts per label kept. Unlike Cochran's Q
    it needs no per-label variance, so labels whose few plans all hit the stop (variance ~0) do not blow it up."""
    if len(set(labels)) < 2 or len(values) < 3:
        return None
    import random

    rng = random.Random(seed)
    obs = between_ss(values, labels)
    lab = list(labels)
    hit = 0
    for _ in range(n_perm):
        rng.shuffle(lab)
        if between_ss(values, lab) >= obs - 1e-12:
            hit += 1
    return (hit + 1) / (n_perm + 1)


def overlap_share(rows: Sequence[Mapping[str, Any]], hold_ms: Mapping[str, int]) -> dict[str, Any]:
    """Share of rows that have another row of the same symbol and side decided within the earlier one's holding
    horizon (``hold_ms`` by timeframe; 7 days for an unknown one): such plans share one price path."""
    by: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for r in rows:
        by.setdefault((str(r.get("symbol")), str(r.get("side"))), []).append((int(r["decided_ts"]), str(r.get("timeframe"))))
    hit = 0
    for xs in by.values():
        xs.sort()
        flags = [False] * len(xs)
        for i, (t, tf) in enumerate(xs):
            h = hold_ms.get(tf, 7 * DAY_MS)
            for j in range(i + 1, len(xs)):
                if xs[j][0] - t >= h:
                    break
                flags[i] = flags[j] = True
        hit += sum(flags)
    return {"n": len(rows), "overlapping": hit, "share": hit / len(rows) if rows else None, "keys": len(by)}
