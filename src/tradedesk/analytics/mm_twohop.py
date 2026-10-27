"""Two-hop detection of exchange deposit addresses of market makers (IDEA-20261017-market-makers, part B2).

Part B counted only MM transfers whose receiver carries an exchange label; almost all MM flow goes to unlabelled
addresses instead. Here a receiver R of an MM transfer is an exchange deposit address if R forwards ("sweeps") the
same token to a labelled exchange address within ``window`` seconds of receiving it from an MM wallet, and most of
R's outgoing ERC-20 transfers go to labelled exchange addresses.

Pure functions, no I/O. Timestamps are unix seconds (block time); shares are decimals (0.8 = 80 %); funding rates are
decimals per funding interval (0.0001 = 0.01 %).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from tradedesk.analytics.mm_onchain import Bar, Transfer, shift, utc_day

DAY = 86_400


@dataclass(frozen=True)
class DepositVerdict:
    is_deposit: bool
    n_out: int  # outgoing ERC-20 rows of the receiver in the fetched page
    n_out_ex: int  # of them to labelled exchange addresses
    n_mm_in: int  # incoming rows from MM wallets in the page
    n_sweeps: int  # MM incoming rows followed by a same-contract transfer R -> exchange within the window
    first_sweep_ts: int | None  # time of the outgoing leg of the first sweep

    @property
    def ex_share(self) -> float:
        return self.n_out_ex / self.n_out if self.n_out else 0.0


def classify_receiver(
    addr: str,
    rows: Iterable[Transfer],
    mm_wallets: Mapping[str, object] | set[str],
    ex: set[str],
    *,
    window: int = DAY,
    min_ex_share: float = 0.8,
    min_sweeps: int = 1,
    until: int | None = None,
) -> DepositVerdict:
    """Registered B2 rule for one receiver ``addr`` from its own ERC-20 transfers ``rows`` (both directions).

    Deposit address iff (a) at least ``min_sweeps`` incoming transfers from an MM wallet of contract C at time t are
    followed by an outgoing transfer of C from ``addr`` to a labelled exchange address at t' with t <= t' <= t + window,
    and (b) at least ``min_ex_share`` of the outgoing rows go to labelled exchange addresses. With ``until`` only rows
    with ts < until are used (point-in-time: rows at or after ``until`` cannot change the result)."""
    a = addr.lower()
    rs = [r for r in rows if until is None or r.ts < until]
    out = [r for r in rs if r.src == a]
    out_ex = sorted((r.ts, r.contract) for r in out if r.dst in ex)
    mm_in = [r for r in rs if r.dst == a and r.src in mm_wallets]
    by_contract: dict[str, list[int]] = {}
    for ts, c in out_ex:
        by_contract.setdefault(c, []).append(ts)
    n_sweeps, first = 0, None
    for r in mm_in:
        hit = next((t for t in by_contract.get(r.contract, ()) if r.ts <= t <= r.ts + window), None)
        if hit is not None:
            n_sweeps += 1
            first = hit if first is None else min(first, hit)
    n_out = len(out)
    share = len(out_ex) / n_out if n_out else 0.0
    ok = n_sweeps >= min_sweeps and share >= min_ex_share
    return DepositVerdict(ok, n_out, len(out_ex), len(mm_in), n_sweeps, first)


def known_since(
    addr: str,
    rows: Sequence[Transfer],
    mm_wallets: Mapping[str, object] | set[str],
    ex: set[str],
    *,
    window: int = DAY,
    min_ex_share: float = 0.8,
    min_sweeps: int = 1,
) -> int | None:
    """Point-in-time: the earliest time T (an outgoing-to-exchange row time + 1 s) at which ``classify_receiver`` on
    rows with ts < T says "deposit"; None if never. Depends only on rows before the returned T."""
    a = addr.lower()
    for t in sorted({r.ts for r in rows if r.src == a and r.dst in ex}):
        v = classify_receiver(a, rows, mm_wallets, ex, window=window, min_ex_share=min_ex_share, min_sweeps=min_sweeps, until=t + 1)
        if v.is_deposit:
            return t + 1
    return None


def day_end(day: str) -> int:
    """Unix seconds of 00:00 UTC of the day after ``day`` (the registered entry time for events of ``day``)."""
    from datetime import date

    return (date.fromisoformat(shift(day, 1)) - date(1970, 1, 1)).days * DAY


def exchange_bound(
    transfers: Iterable[Transfer],
    ex: set[str],
    deposits: Mapping[str, int | None] | set[str],
    *,
    point_in_time: bool = False,
) -> list[Transfer]:
    """MM outgoing transfers that reach an exchange: receiver labelled (``ex``) or a detected deposit address.

    ``deposits`` maps a deposit address to the time from which it is known (``known_since``) or is a plain set
    (address-level rule). With ``point_in_time`` a transfer on day d counts only if the address was known before the
    entry time 00:00 UTC of d + 1."""
    out = []
    for t in transfers:
        if t.dst in ex:
            out.append(t)
        elif t.dst in deposits:
            if not point_in_time:
                out.append(t)
                continue
            k = deposits[t.dst] if isinstance(deposits, Mapping) else None
            if k is not None and k <= day_end(utc_day(t.ts)):
                out.append(t)
    return out


def net_daily(gross: Mapping[tuple[str, str], float], withdrawals: Mapping[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """{(symbol, day): gross deposits minus withdrawals from exchanges to MM wallets}, positive days only."""
    out = {}
    for k, g in gross.items():
        n = g - withdrawals.get(k, 0.0)
        if n > 0:
            out[k] = n
    return out


def funding_short(funding: Sequence[tuple[int, float]], entry_ms: int, exit_ms: int) -> float:
    """Funding P&L of a short (decimal of notional): sum of rates settled in (entry + 60 s, exit). Binance stamps
    settlements either exactly on the hour or a few ms after it, so the 00:00 settlement at entry is not received
    and the 00:00 settlement at exit (``exit_ms``, the close of the last day) is excluded in both cases."""
    return sum(r for ts, r in funding if entry_ms + 60_000 < ts < exit_ms)


def short_net(
    bars: Mapping[str, Bar], funding: Sequence[tuple[int, float]], day: str, h: int, cost: float
) -> tuple[float, float, float] | None:
    """(net, gross, funding) of a short: entry at the open of d + 1, exit at the close of d + h, minus round-trip
    ``cost``, plus funding received. None if a bar is missing. Uses no data after the close of d + h."""
    a, b = bars.get(shift(day, 1)), bars.get(shift(day, h))
    if a is None or b is None or a.open <= 0 or b.close <= 0:
        return None
    gross = -(b.close / a.open - 1.0)
    f = funding_short(funding, day_end(day) * 1000, day_end(shift(day, h)) * 1000)
    return gross - cost + f, gross, f


def placebo_days(day: str, event_days: Sequence[str], rng_sample: list[int], lo: int = 7, hi: int = 120) -> list[str]:
    """Placebo days d - k for the given offsets ``rng_sample`` (drawn by the caller from [lo, hi]) that lie more than
    7 days from every event day of the same token."""
    from datetime import date

    ev = [date.fromisoformat(x).toordinal() for x in event_days]
    out = []
    for k in rng_sample:
        if not lo <= k <= hi:
            continue
        c = shift(day, -k)
        o = date.fromisoformat(c).toordinal()
        if all(abs(o - e) > 7 for e in ev):
            out.append(c)
    return out


def finite(xs: Iterable[float]) -> list[float]:
    return [x for x in xs if math.isfinite(x)]


def cluster_boot(
    values: Sequence[float], clusters: Sequence[object], *, n_boot: int = 10_000, seed: int = 20261027
) -> tuple[float, float, float, float]:
    """(mean, CI95 low, CI95 high, one-sided p of mean <= 0): percentile bootstrap resampling whole clusters.
    p = (1 + #{bootstrap means <= 0}) / (1 + n_boot)."""
    import numpy as np

    if not values:
        return math.nan, math.nan, math.nan, math.nan
    g: dict[object, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        g.setdefault(c, []).append(float(v))
    sums = np.array([sum(v) for v in g.values()])
    cnts = np.array([len(v) for v in g.values()], dtype=float)
    k = len(sums)
    idx = np.random.default_rng(seed).integers(0, k, size=(n_boot, k))
    means = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    p = (1 + int((means <= 0).sum())) / (1 + n_boot)
    return float(sum(values) / len(values)), float(lo), float(hi), p
