"""Profile a Hyperliquid vault (or any account) from public data: equity, follower flows, positions, episodes.

Research tool (not an MCP tool): reverse-engineers what a vault leader actually does, so a setup can be judged
before anyone copies it. All network calls go to the public ``https://api.hyperliquid.xyz/info`` endpoint,
read-only, no keys. Everything the API returns is untrusted data (vault names and descriptions are free text).

Sources and their limits:
* ``vaultDetails.portfolio.allTime`` — official account value and cumulative PnL (PnL excludes deposits and
  withdrawals), ~40–80 points over the vault's life;
* ``userFunding`` — one row per coin per funding period with the position size ``szi``; old rows are daily
  aggregates (``nSamples`` hours, ``szi`` = average). This is the only position history that covers the whole
  life of the vault, so daily positions, leverage and episodes are rebuilt from it (daily resolution, intraday
  trades are invisible, PnL is a mark-to-market approximation checked against the official PnL);
* ``userFillsByTime`` — exact fills, but the API serves only the most recent ones (≈ 2 months for an active vault);
* ``userNonFundingLedgerUpdates`` — follower deposits / withdrawals. With ``alwaysCloseOnWithdraw`` a withdrawal
  closes a slice of every position: those close fills carry the withdrawal's timestamp and are not the leader's
  decisions, so they are separated (``forced``).

Usage:
    uv run python scripts/research/vault_profile.py <vault_address> [--fills FILE] [--data-dir DIR] [--refresh]
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

API = "https://api.hyperliquid.xyz/info"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# Equal-weight "broad alts" factor; fixed list, chosen before looking at the vault, all listed on Hyperliquid.
ALT_BASKET = ("ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "DOT", "LTC", "UNI", "AAVE", "NEAR")
SIZE_CHANGE = 0.15  # a day-to-day change in |position| above 15 % counts as an add / a trim


# ----------------------------------------------------------------------------------------------- fetching (I/O)


def _post(body: dict[str, Any]) -> Any:
    last: Exception | None = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(API, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - retry any transport error, re-raise after the last attempt
            last = exc
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"Hyperliquid info {body.get('type')} failed: {last}")


def _paged(body: dict[str, Any], start: int) -> list[dict[str, Any]]:
    """Page a time-ordered endpoint by ``startTime`` until a page brings nothing new.

    The next page starts at the newest timestamp seen, not one ms after it: aggregated funding rows of many coins
    share one timestamp, and a page cut in the middle of them would otherwise drop the rest.
    """
    unique: dict[str, dict[str, Any]] = {}
    t = start
    while True:
        batch = _post({**body, "startTime": t}) or []
        before = len(unique)
        for r in batch:
            unique[json.dumps(r, sort_keys=True)] = r
        if not batch or len(unique) == before:
            break
        newest = max(int(r["time"]) for r in batch)
        t = newest if newest > t else newest + 1
        time.sleep(0.3)
    return sorted(unique.values(), key=lambda r: int(r["time"]))


def _cached(path: Path, refresh: bool, produce: Any) -> Any:
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    data = produce()
    path.write_text(json.dumps(data))
    return data


def load_data(vault: str, data_dir: Path, fills_file: Path | None = None, refresh: bool = False) -> dict[str, Any]:
    data_dir.mkdir(parents=True, exist_ok=True)
    details = _cached(data_dir / "details.json", refresh, lambda: _post({"type": "vaultDetails", "vaultAddress": vault}))
    state = _cached(data_dir / "state.json", refresh, lambda: _post({"type": "clearinghouseState", "user": vault}))
    first = int(series(details, "accountValueHistory")[0][0]) - 7 * DAY_MS
    if fills_file is not None:
        fills = json.loads(fills_file.read_text())
    else:
        fills = _cached(
            data_dir / "fills.json", refresh, lambda: _paged({"type": "userFillsByTime", "user": vault, "aggregateByTime": True}, first)
        )
    funding = _cached(data_dir / "funding.json", refresh, lambda: _paged({"type": "userFunding", "user": vault}, first))
    ledger = _cached(data_dir / "ledger.json", refresh, lambda: _paged({"type": "userNonFundingLedgerUpdates", "user": vault}, first))
    return {"details": details, "state": state, "fills": fills, "funding": funding, "ledger": ledger}


def load_candles(coins: set[str], start: int, end: int, data_dir: Path, refresh: bool = False) -> dict[str, dict[int, tuple]]:
    """Daily candles {coin: {day_ms: (open, high, low, close)}}; coins without data are skipped."""
    out: dict[str, dict[int, tuple]] = {}
    for coin in sorted(coins):
        path = data_dir / f"candles_{coin}_1d.json"
        req = {"coin": coin, "interval": "1d", "startTime": start - 40 * DAY_MS, "endTime": end + DAY_MS}
        try:
            rows = _cached(path, refresh, lambda req=req: _post({"type": "candleSnapshot", "req": req}) or [])
        except RuntimeError:
            continue
        if rows:
            out[coin] = {int(r["t"]): (float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in rows}
    return out


# ------------------------------------------------------------------------------------------------ pure analytics


def ymd(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def day_of(ms: float) -> int:
    return int(ms) // DAY_MS * DAY_MS


def series(details: dict[str, Any], key: str, window: str = "allTime") -> list[tuple[int, float]]:
    for name, block in details.get("portfolio") or []:
        if name == window:
            return [(int(t), float(v)) for t, v in block.get(key) or []]
    return []


def max_drawdown(values: list[float]) -> tuple[float, int, int]:
    """Largest peak-to-trough fall of a positive NAV series: (fraction, peak index, trough index)."""
    best, peak_i, out = 0.0, 0, (0.0, 0, 0)
    for i, v in enumerate(values):
        if v > values[peak_i]:
            peak_i = i
        dd = 1 - v / values[peak_i] if values[peak_i] > 0 else 0.0
        if dd > best:
            best, out = dd, (dd, peak_i, i)
    return out


def dietz_base(av: dict[int, float], flows: list[tuple[int, float]] | None, t0: int, t1: int) -> float:
    """Capital an official interval's PnL was earned on.

    Normally the account value at the interval's start. When the net flows inside the interval exceed it (a vault's
    launch: a $100 seed, then millions deposited), modified Dietz instead: start value + half the net flows; dividing
    by the seed would make the return explode. Dietz is not used everywhere: followers deposit into falls and
    withdraw into rallies, and on BredoStrategy it doubled the return seen by a follower who stayed from day one.
    """
    start = av.get(t0, 0.0)
    inside = sum(f for t, f in flows if t0 < t <= t1) if flows else 0.0
    return start + 0.5 * inside if abs(inside) > start else start


def follower_returns(details: dict[str, Any], min_days: float) -> list[float]:
    """allTimePnl / (equity − allTimePnl) of followers in the vault for at least ``min_days`` (leader excluded).

    A follower who never added or withdrew earns exactly the vault's time-weighted return; adds pull the ratio
    down, partial withdrawals push it up, so the median of the long-standing followers is a cross-check.
    """
    out = []
    leader = str(details.get("leader", "")).lower()
    for f in details.get("followers") or []:
        if str(f.get("user", "")).lower() == leader or float(f.get("daysFollowing") or 0) < min_days:
            continue
        eq, pnl = float(f.get("vaultEquity") or 0), float(f.get("allTimePnl") or 0)
        if eq - pnl > 0:
            out.append(pnl / (eq - pnl))
    return out


def twr_curve(
    account_value: list[tuple[int, float]], pnl: list[tuple[int, float]], flows: list[tuple[int, float]] | None = None
) -> list[tuple[int, float]]:
    """Time-weighted NAV (start 1.0): each interval's return = ΔPnL / capital at work (``dietz_base``).

    Follower deposits move account value but not PnL, so this strips the flows out. Without ``flows`` the base is
    the account value at the interval's start (a flow inside the interval is attributed to its end).
    """
    av = dict(account_value)
    nav, out = 1.0, []
    for (t0, p0), (t1, p1) in zip(pnl, pnl[1:], strict=False):
        base = dietz_base(av, flows, t0, t1)
        if not out:
            out.append((t0, nav))
        if base > 0:
            nav = max(0.0, nav * (1 + (p1 - p0) / base))  # a loss above the capital at work wipes the unit out
        out.append((t1, nav))
    return out


def flows(ledger: list[dict[str, Any]], leader: str | None = None) -> dict[str, Any]:
    """Money in and out of the vault, total and by month (USD), plus the leader's own withdrawals.

    Deposit rows carry no depositor address, so the leader's own deposits cannot be separated.
    """
    by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    dep = wd = commission = leader_wd = 0.0
    n_dep = n_wd = n_leader = 0
    leader = (leader or "").lower()
    for row in ledger:
        d = row.get("delta") or {}
        kind, month = d.get("type"), ymd(row["time"])[:7]
        if kind in ("vaultDeposit", "vaultCreate"):
            usd = float(d.get("usdc") or 0)
            dep += usd
            n_dep += 1
            by_month[month][0] += usd
        elif kind == "vaultWithdraw":
            usd = float(d.get("netWithdrawnUsd") or d.get("requestedUsd") or 0)
            wd += usd
            n_wd += 1
            commission += float(d.get("commission") or 0)
            by_month[month][1] += usd
            if leader and str(d.get("user", "")).lower() == leader:
                leader_wd += usd
                n_leader += 1
        elif kind == "vaultDistribution":  # payout of the remaining equity when a vault is closed
            usd = float(d.get("usdc") or 0)
            wd += usd
            by_month[month][1] += usd
    return {
        "deposits": dep,
        "withdrawals": wd,
        "n_deposits": n_dep,
        "n_withdrawals": n_wd,
        "commission": commission,
        "leader_withdrawals": leader_wd,
        "n_leader_withdrawals": n_leader,
        "by_month": dict(by_month),
    }


def trim_after_close(av: list[tuple[int, float]], pnl: list[tuple[int, float]]) -> tuple[list, list]:
    """Drop the tail after a vault was emptied (account value ≈ 0 for the rest of the series)."""
    if not av:
        return av, pnl
    peak = max(v for _, v in av)
    last = max((i for i, (_, v) in enumerate(av) if v > 0.01 * peak), default=len(av) - 1)
    cut = av[min(last + 1, len(av) - 1)][0]
    return [p for p in av if p[0] <= cut], [p for p in pnl if p[0] <= cut]


def daily_flows(ledger: list[dict[str, Any]]) -> dict[int, float]:
    """Net follower flow per UTC day: deposits − net withdrawals (USD; the leader's commission stays in the vault)."""
    out: dict[int, float] = defaultdict(float)
    for row in ledger:
        d = row.get("delta") or {}
        if d.get("type") in ("vaultDeposit", "vaultCreate"):
            out[day_of(row["time"])] += float(d.get("usdc") or 0)
        elif d.get("type") == "vaultWithdraw":
            out[day_of(row["time"])] -= float(d.get("netWithdrawnUsd") or d.get("requestedUsd") or 0)
        elif d.get("type") == "vaultDistribution":
            out[day_of(row["time"])] -= float(d.get("usdc") or 0)
    return dict(out)


def daily_equity(flow_by_day: dict[int, float], pnl: list[tuple[int, float]], days: list[int]) -> dict[int, float]:
    """Capital working on each day: net flows before the day + official PnL at its start + half of the day's flow.

    Interpolating the sparse official account value instead is wrong when big deposits land between its points.
    """
    flow_days = sorted(flow_by_day)
    cum = [0.0]
    for d in flow_days:
        cum.append(cum[-1] + flow_by_day[d])
    out = {}
    for day in days:
        before = cum[bisect.bisect_left(flow_days, day)]
        out[day] = before + interp(pnl, day) + 0.5 * flow_by_day.get(day, 0.0)
    return out


def ledger_flows(ledger: list[dict]) -> list[tuple[int, float]]:
    """Timestamped net flows into the vault (deposits +, withdrawals and distributions −), USD."""
    out = []
    for row in ledger:
        d = row.get("delta") or {}
        kind = d.get("type")
        if kind in ("vaultDeposit", "vaultCreate"):
            out.append((int(row["time"]), float(d.get("usdc") or 0)))
        elif kind == "vaultWithdraw":
            out.append((int(row["time"]), -float(d.get("netWithdrawnUsd") or d.get("requestedUsd") or 0)))
        elif kind == "vaultDistribution":
            out.append((int(row["time"]), -float(d.get("usdc") or 0)))
    return sorted(out)


def anchored_equity(
    av: list[tuple[int, float]], flows: list[tuple[int, float]], day_pnl: dict[int, float], days: list[int]
) -> dict[int, float]:
    """Equity working on each day, using only information up to that day (no look-ahead).

    Start from the last official account value at or before the day opens, then add the rebuilt PnL of the full
    days since that point and the flows since its timestamp; flows during the day count half. ``daily_equity``
    interpolates the official PnL between weekly points instead, which leaks the rest of the
    week into the denominator and makes the leverage path unusable for a sizing test.
    """
    ts = [t for t, _ in av]
    out = {}
    for day in days:
        i = bisect.bisect_right(ts, day) - 1
        if i < 0 or av[i][1] <= 0:
            continue
        t0, base = av[i]
        eq = base + sum(p for d, p in day_pnl.items() if day_of(t0) < d < day)
        eq += sum(f for t, f in flows if t0 < t < day) + 0.5 * sum(f for t, f in flows if day <= t < day + DAY_MS)
        out[day] = eq
    return out


def withdrawal_times(ledger: list[dict[str, Any]]) -> list[int]:
    """Timestamps of follower withdrawals that closed positions (``closingCost`` > 0)."""
    out = []
    for row in ledger:
        d = row.get("delta") or {}
        if d.get("type") == "vaultWithdraw" and float(d.get("closingCost") or 0) > 0:
            out.append(int(row["time"]))
    return sorted(out)


@dataclass
class Fill:
    t: int
    coin: str
    px: float
    delta: float  # signed size change (+ buy, − sell)
    start: float  # position before the fill
    closed_pnl: float
    fee: float
    maker: bool
    oid: int
    forced: bool = False  # a close caused by a vault withdrawal (a follower's or the leader's own), not a trading decision

    @property
    def end(self) -> float:
        return self.start + self.delta

    @property
    def close_gain(self) -> float:
        """For a reducing fill: price move from the position's average entry, signed for the side (+ = profit).

        The entry is implied by Hyperliquid's ``closedPnl`` = (px − entry) × size (long) or (entry − px) × size (short).
        """
        size = abs(self.delta)
        if size == 0 or self.start == 0:
            return float("nan")
        side = 1 if self.start > 0 else -1
        entry = self.px - side * self.closed_pnl / size
        return side * (self.px / entry - 1) if entry > 0 else float("nan")


def parse_fills(raw: list[dict[str, Any]], forced_times: list[int] | None = None, tol_ms: int = 1000) -> list[Fill]:
    forced_times = forced_times or []
    out = []
    for r in raw:
        delta = float(r["sz"]) * (1 if r["side"] == "B" else -1)
        start = float(r["startPosition"])
        f = Fill(
            int(r["time"]),
            r["coin"],
            float(r["px"]),
            delta,
            start,
            float(r["closedPnl"]),
            float(r["fee"]),
            not r.get("crossed", True),
            int(r["oid"]),
        )
        reduces = start != 0 and abs(f.end) < abs(start) and (f.end == 0 or math.copysign(1, f.end) == math.copysign(1, start))
        if reduces and forced_times:
            i = bisect.bisect_left(forced_times, f.t - tol_ms)
            f.forced = i < len(forced_times) and forced_times[i] <= f.t + tol_ms
        out.append(f)
    out.sort(key=lambda f: (f.t, f.coin))
    return out


@dataclass
class Episode:
    coin: str
    side: int  # +1 long, −1 short
    t_open: int
    t_close: int | None = None  # None = still open at the end of the data
    truncated: bool = False  # already open when the data starts
    opens: list[Fill] = field(default_factory=list)
    closes: list[Fill] = field(default_factory=list)
    max_size: float = 0.0
    max_notional: float = 0.0

    @property
    def realized(self) -> float:
        return sum(f.closed_pnl for f in self.closes)

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.opens + self.closes)

    @property
    def entry_vwap(self) -> float:
        sz = sum(abs(f.delta) for f in self.opens)
        return sum(abs(f.delta) * f.px for f in self.opens) / sz if sz else float("nan")

    def add_clusters(self, gap_ms: int = HOUR_MS) -> list[list[Fill]]:
        """Opening fills grouped into decisions: a gap longer than ``gap_ms`` starts a new add."""
        out: list[list[Fill]] = []
        for f in self.opens:
            if out and f.t - out[-1][-1].t <= gap_ms:
                out[-1].append(f)
            else:
                out.append([f])
        return out

    def add_direction(self, gap_ms: int = HOUR_MS) -> Counter:
        """For each add after the first: 'down' if bought below the running average entry (long), else 'up'."""
        res: Counter = Counter()
        sz = cost = 0.0
        for k, cl in enumerate(self.add_clusters(gap_ms)):
            c_sz = sum(abs(f.delta) for f in cl)
            c_px = sum(abs(f.delta) * f.px for f in cl) / c_sz
            if k > 0 and sz > 0:
                better = c_px < cost / sz if self.side > 0 else c_px > cost / sz
                res["down" if better else "up"] += 1
            sz += c_sz
            cost += c_sz * c_px
        return res


def build_episodes(fills: list[Fill]) -> list[Episode]:
    """Rebuild per-coin positions from fills: an episode runs from flat to flat (a flip closes one and opens the next)."""
    eps: list[Episode] = []
    cur: dict[str, Episode] = {}
    for f in fills:
        ep = cur.get(f.coin)
        if ep is None and f.start != 0:  # position existed before the first fill we see
            ep = Episode(f.coin, 1 if f.start > 0 else -1, f.t, truncated=True, max_size=abs(f.start), max_notional=abs(f.start) * f.px)
            cur[f.coin] = ep
            eps.append(ep)
        if ep is None:
            ep = Episode(f.coin, 1 if f.delta > 0 else -1, f.t)
            cur[f.coin] = ep
            eps.append(ep)
        end = f.end
        if abs(end) < 1e-9 or (end * ep.side < 0):  # flat or flipped
            ep.closes.append(f)
            ep.t_close = f.t
            del cur[f.coin]
            if abs(end) >= 1e-9:  # the remainder opens the opposite episode
                nxt = Episode(f.coin, 1 if end > 0 else -1, f.t, max_size=abs(end), max_notional=abs(end) * f.px)
                cur[f.coin] = nxt
                eps.append(nxt)
            continue
        (ep.opens if abs(end) > abs(f.start) else ep.closes).append(f)
        if abs(end) > ep.max_size:
            ep.max_size = abs(end)
            ep.max_notional = max(ep.max_notional, abs(end) * f.px)
    return eps


def daily_positions(funding: list[dict[str, Any]]) -> dict[str, dict[int, tuple[float, float, float]]]:
    """{coin: {day: (avg szi while open, exposure szi = szi × hours/24, funding USD)}} from ``userFunding`` rows.

    Old rows are daily aggregates (``nSamples`` hours, average size); recent rows are hourly. A coin missing on a
    day had no position at any funding time that day.
    """
    acc: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    for row in funding:
        d = row.get("delta") or {}
        if d.get("type") != "funding":
            continue
        n = float(d.get("nSamples") or 1)
        cell = acc[d["coin"]][day_of(row["time"])]
        cell[0] += float(d["szi"]) * n  # size × hours
        cell[1] += n
        cell[2] += float(d["usdc"])
    return {c: {day: (v[0] / v[1], v[0] / 24, v[2]) for day, v in sorted(days.items())} for c, days in acc.items()}


@dataclass
class DailyEpisode:
    coin: str
    side: int
    start: int
    end: int  # last day with a position
    open_at_end: bool
    days: list[int]
    sizes: list[float]  # avg szi per day (signed)
    adds: list[int] = field(default_factory=list)  # days where |size| grew > SIZE_CHANGE
    trims: list[int] = field(default_factory=list)


def daily_episodes(pos: dict[str, dict[int, tuple[float, float, float]]], last_day: int) -> list[DailyEpisode]:
    """Group consecutive position days per coin into episodes (a missing day or a sign flip ends one)."""
    out = []
    for coin, days in pos.items():
        ep: DailyEpisode | None = None
        for day in sorted(days):
            szi = days[day][0]
            if szi == 0:
                continue
            side = 1 if szi > 0 else -1
            if ep is None or day - ep.end > DAY_MS or side != ep.side:
                ep = DailyEpisode(coin, side, day, day, False, [], [])
                out.append(ep)
            elif ep.sizes:
                prev = abs(ep.sizes[-1])
                if abs(szi) > prev * (1 + SIZE_CHANGE):
                    ep.adds.append(day)
                elif abs(szi) < prev * (1 - SIZE_CHANGE):
                    ep.trims.append(day)
            ep.days.append(day)
            ep.sizes.append(szi)
            ep.end = day
    for ep in out:
        ep.open_at_end = ep.end >= last_day
    return sorted(out, key=lambda e: (e.start, e.coin))


def episode_pnl(ep: DailyEpisode, pos: dict[str, dict[int, tuple]], candles: dict[str, dict[int, tuple]]) -> dict[str, float]:
    """Mark-to-market PnL of a daily episode: Σ exposure × (close − open) per day + funding; NaN if no candles."""
    cs = candles.get(ep.coin) or {}
    mtm = fund = 0.0
    max_notional = cum = worst = 0.0
    for day in ep.days:
        _, expo, f = pos[ep.coin][day]
        fund += f
        if day not in cs:
            return {"pnl": float("nan"), "funding": fund, "max_notional": max_notional, "worst": float("nan")}
        o, _, _, c = cs[day]
        mtm += expo * (c - o)
        max_notional = max(max_notional, abs(pos[ep.coin][day][0]) * c)
        cum = mtm + fund
        worst = min(worst, cum)
    return {"pnl": mtm + fund, "funding": fund, "max_notional": max_notional, "worst": worst}


def daily_book(pos: dict[str, dict[int, tuple]], candles: dict[str, dict[int, tuple]]) -> dict[int, dict[str, float]]:
    """Per day: long / short / gross notional (avg size × close), MTM PnL, funding, number of coins held."""
    book: dict[int, dict[str, float]] = defaultdict(
        lambda: {"long": 0.0, "short": 0.0, "pnl": 0.0, "funding": 0.0, "coins": 0, "unpriced": 0}
    )
    for coin, days in pos.items():
        cs = candles.get(coin) or {}
        for day, (szi, expo, f) in days.items():
            b = book[day]
            b["funding"] += f
            b["coins"] += 1
            if day not in cs:
                b["unpriced"] += 1
                continue
            o, _, _, c = cs[day]
            b["long" if szi > 0 else "short"] += abs(szi) * c
            b["pnl"] += expo * (c - o) + f
    for b in book.values():
        b["gross"] = b["long"] + b["short"]
    return dict(sorted(book.items()))


def interp(points: list[tuple[int, float]], t: int) -> float:
    if not points:
        return float("nan")
    ts = [p[0] for p in points]
    i = bisect.bisect_left(ts, t)
    if i == 0:
        return points[0][1]
    if i >= len(points):
        return points[-1][1]
    (t0, v0), (t1, v1) = points[i - 1], points[i]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def ols(y: list[float], xs: list[list[float]]) -> dict[str, Any]:
    """OLS with intercept; returns coefficients, t-stats (plain, not HAC) and R²."""
    yv = np.asarray(y, float)
    x = np.column_stack([np.ones(len(yv))] + [np.asarray(v, float) for v in xs])
    coef, *_ = np.linalg.lstsq(x, yv, rcond=None)
    resid = yv - x @ coef
    dof = max(1, len(yv) - x.shape[1])
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.pinv(x.T @ x)
    se = np.sqrt(np.diag(cov))
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    return {
        "coef": [float(c) for c in coef],
        "t": [float(c / s) if s > 0 else float("nan") for c, s in zip(coef, se, strict=True)],
        "r2": 1 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan"),
        "n": len(yv),
    }


def day_returns(candles: dict[str, dict[int, tuple]], coins: tuple[str, ...] | list[str], day: int) -> float:
    """Equal-weight open→close return of ``coins`` on ``day`` (coins without a candle are skipped)."""
    rs = [cs[day][3] / cs[day][0] - 1 for coin in coins if (cs := candles.get(coin)) and day in cs and cs[day][0] > 0]
    return sum(rs) / len(rs) if rs else float("nan")


def ret_between(cs: dict[int, tuple], d0: int, d1: int) -> float:
    """Close-to-close return from day d0 to day d1 (NaN if a candle is missing)."""
    if d0 in cs and d1 in cs and cs[d0][3] > 0:
        return cs[d1][3] / cs[d0][3] - 1
    return float("nan")


def interval_returns(
    av: list[tuple[int, float]],
    pnl: list[tuple[int, float]],
    candles: dict[str, dict[int, tuple]],
    flows: list[tuple[int, float]] | None = None,
) -> list[tuple[int, float, float, float]]:
    """Per official portfolio interval: (start, vault return = ΔPnL / account value, BTC return, EW alt basket return).

    Prices are daily opens of the interval's first and last UTC day, so intervals inside one day are skipped.
    """
    btc = candles.get("BTC") or {}
    avd = dict(av)
    out = []
    for (t0, p0), (t1, p1) in zip(pnl, pnl[1:], strict=False):
        d0, d1, base = day_of(t0), day_of(t1), dietz_base(avd, flows, t0, t1)
        if d1 <= d0 or base <= 0 or d0 not in btc or d1 not in btc:
            continue
        alts = [cs[d1][0] / cs[d0][0] - 1 for c in ALT_BASKET if (cs := candles.get(c)) and d0 in cs and d1 in cs]
        if alts:
            out.append((t0, (p1 - p0) / base, btc[d1][0] / btc[d0][0] - 1, sum(alts) / len(alts)))
    return out


def entry_features(cs: dict[int, tuple], btc: dict[int, tuple], day: int) -> dict[str, float]:
    """Context known before ``day`` opens (uses closes up to day − 1 only) plus forward returns from day − 1's close."""
    prev = day - DAY_MS
    highs = [cs[d][1] for d in range(day - 30 * DAY_MS, day, DAY_MS) if d in cs]
    lows = [cs[d][2] for d in range(day - 30 * DAY_MS, day, DAY_MS) if d in cs]
    c = cs.get(prev, (0, 0, 0, float("nan")))[3]
    return {
        "ret3": ret_between(cs, day - 4 * DAY_MS, prev),
        "ret7": ret_between(cs, day - 8 * DAY_MS, prev),
        "ret30": ret_between(cs, day - 31 * DAY_MS, prev),
        "from_high30": c / max(highs) - 1 if highs else float("nan"),
        "pos_in_range30": (c - min(lows)) / (max(highs) - min(lows)) if highs and max(highs) > min(lows) else float("nan"),
        "btc_ret3": ret_between(btc, day - 4 * DAY_MS, prev),
        "fwd7": ret_between(cs, prev, prev + 7 * DAY_MS),
        "fwd14": ret_between(cs, prev, prev + 14 * DAY_MS),
        "fwd30": ret_between(cs, prev, prev + 30 * DAY_MS),
    }


def _q(values: list[float], pct: bool = True, qs: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)) -> str:
    """p10 / p25 / median / p75 / p90 of the non-NaN values."""
    v = sorted(x for x in values if not math.isnan(x))
    if not v:
        return "—"
    fmt = "{:+.1%}" if pct else "{:.1f}"
    return " / ".join(fmt.format(v[min(len(v) - 1, int(q * len(v)))]) for q in qs) + f"  (n={len(v)})"


def _med(values: list[float]) -> float:
    v = [x for x in values if not math.isnan(x)]
    return statistics.median(v) if v else float("nan")


def _usd(x: float) -> str:
    return f"{x / 1e6:+.2f}M" if abs(x) >= 1e5 else f"{x:+,.0f}"


# ------------------------------------------------------------------------------------------------------- report


def report(vault: str, data: dict[str, Any], candles: dict[str, dict[int, tuple]]) -> None:
    det, ledger = data["details"], data["ledger"]
    av, pnl = trim_after_close(series(det, "accountValueHistory"), series(det, "pnlHistory"))
    print(f"# {det.get('name')} {vault}  leader {det.get('leader')}  closed={det.get('isClosed')}")
    print(f"  description (untrusted): {(det.get('description') or '').strip()[:200]!r}")
    print(
        f"  commission {det.get('leaderCommission')}  leader share {float(det.get('leaderFraction') or 0):.1%}  followers listed {len(det.get('followers') or [])}"
    )

    # 1. equity, flows, drawdown
    fl = flows(ledger, det.get("leader"))
    print(f"\n## Equity and flows  {ymd(av[0][0])} … {ymd(av[-1][0])}")
    print(
        f"  account value now {_usd(av[-1][1])}, peak {_usd(max(v for _, v in av))}; official PnL {_usd(pnl[-1][1])}, peak PnL {_usd(max(v for _, v in pnl))}"
    )
    print(
        f"  deposits {_usd(fl['deposits'])} (n={fl['n_deposits']}), withdrawals {_usd(fl['withdrawals'])} (n={fl['n_withdrawals']}), leader commission paid by leavers {_usd(fl['commission'])}"
    )
    print(f"  of the withdrawals, by the leader himself: {_usd(fl['leader_withdrawals'])} (n={fl['n_leader_withdrawals']})")
    print(
        f"  check: deposits − withdrawals + PnL = {_usd(fl['deposits'] - fl['withdrawals'] + pnl[-1][1])} vs account value {_usd(av[-1][1])}"
    )
    for m, (d, w) in sorted(fl["by_month"].items()):
        print(f"    {m}: in {_usd(d)}, out {_usd(w)}")
    lf = ledger_flows(ledger)
    nav = twr_curve(av, pnl, lf)
    dd, pi, ti = max_drawdown([v for _, v in nav])
    pnl_dd = max((max(v for _, v in pnl[: i + 1]) - pnl[i][1]) for i in range(len(pnl)))
    print(
        f"  time-weighted return {nav[-1][1] - 1:+.1%} over {(nav[-1][0] - nav[0][0]) / DAY_MS:.0f} days; max drawdown {dd:.1%} ({ymd(nav[pi][0])} → {ymd(nav[ti][0])}); max PnL giveback {_usd(-pnl_dd)}"
    )
    age = (nav[-1][0] - nav[0][0]) / DAY_MS
    fr = follower_returns(det, 0.9 * age)
    print(
        f"  cross-check, followers in the vault ≥ 90 % of its life (top-100 list, leader excluded): median return {_med(fr):+.1%} (n={len(fr)}, quartiles {_q(fr, qs=(0.25, 0.75))})"
        if fr
        else "  cross-check: no follower in the vault ≥ 90 % of its life"
    )
    print("  NAV points: " + ", ".join(f"{ymd(t)[5:]} {v:.2f}" for t, v in nav[:: max(1, len(nav) // 12)]))

    # 2. daily positions from funding rows
    pos = daily_positions(data["funding"])
    if not pos:
        print("\n(no funding rows → no position history)")
        return
    last_day = max(max(d) for d in pos.values())
    book = daily_book(pos, candles)
    print(f"\n## Daily book from funding rows  {ymd(min(book))} … {ymd(last_day)}  (coins ever held: {len(pos)})")
    days = list(book)
    equity = anchored_equity(av, lf, {d: b["pnl"] for d, b in book.items()}, days)
    lev = []
    for day in days:
        eq = equity.get(day, 0.0)
        b = book[day]
        lev.append((day, b["gross"] / eq if eq > 0 else float("nan"), (b["long"] - b["short"]) / eq if eq > 0 else float("nan")))
    print(f"  gross leverage x (gross notional / equity): {_q([x[1] for x in lev], False)}  [p10/p25/med/p75/p90]")
    print(f"  net leverage x: {_q([x[2] for x in lev], False)}")
    short_days = [d for d in days if book[d]["short"] > 0.1 * book[d]["gross"]]
    print(
        f"  days with >10 % of gross short: {len(short_days)} of {len(days)}"
        + (f" ({ymd(short_days[0])} … {ymd(short_days[-1])})" if short_days else "")
    )
    print(f"  coins held per day: {_q([float(book[d]['coins']) for d in days], False)}")
    monthly: dict[str, list[float]] = defaultdict(list)
    for day, g, _net in lev:
        monthly[ymd(day)[:7]].append(g)
    print("  median gross leverage by month: " + ", ".join(f"{m[2:]} {_med(v):.1f}x" for m, v in sorted(monthly.items())))
    mtm_total = sum(b["pnl"] for b in book.values())
    fund_total = sum(b["funding"] for b in book.values())
    unpriced = sum(b["unpriced"] for b in book.values())
    print(
        f"  MTM PnL rebuilt from daily positions {_usd(mtm_total)} (funding {_usd(fund_total)}, fees not included, {unpriced} coin-days unpriced) vs official {_usd(pnl[-1][1])}"
    )

    # 3. beta: daily returns on BTC and an equal-weight alt basket
    btc = candles.get("BTC") or {}
    y, xb, xa, used, gross_btc, gross_alt = [], [], [], [], 0.0, 0.0
    for day in days:
        eq = equity.get(day, 0.0)
        rb, ra = (btc[day][3] / btc[day][0] - 1) if day in btc else float("nan"), day_returns(candles, ALT_BASKET, day)
        if eq <= 0 or math.isnan(rb) or math.isnan(ra):
            continue
        y.append(book[day]["pnl"] / eq)
        used.append(day)
        xb.append(rb)
        xa.append(ra)
        gross_btc += book[day]["gross"] * rb
        gross_alt += book[day]["gross"] * ra
    if len(y) > 10:
        r1, r2, r3 = ols(y, [xb]), ols(y, [xa]), ols(y, [xb, xa])
        print(f"\n## Beta (daily rebuilt PnL / equity, n={len(y)} days)")
        print(
            f"  on BTC:        beta {r1['coef'][1]:.2f} (t {r1['t'][1]:.1f}), alpha {r1['coef'][0]:+.3%}/day (t {r1['t'][0]:.1f}), R² {r1['r2']:.2f}"
        )
        print(
            f"  on alt basket: beta {r2['coef'][1]:.2f} (t {r2['t'][1]:.1f}), alpha {r2['coef'][0]:+.3%}/day (t {r2['t'][0]:.1f}), R² {r2['r2']:.2f}"
        )
        print(
            f"  both:          BTC {r3['coef'][1]:.2f}, alts {r3['coef'][2]:.2f}, alpha {r3['coef'][0]:+.3%}/day (t {r3['t'][0]:.1f}), R² {r3['r2']:.2f}"
        )
        print(
            f"  same gross notional held every day in BTC → {_usd(gross_btc)}; in the EW alt basket → {_usd(gross_alt)}; rebuilt PnL {_usd(mtm_total)}"
        )
        print(f"  daily return vol {statistics.pstdev(y):.2%}; best day {max(y):+.1%}, worst day {min(y):+.1%}")
        # Compounding the rebuilt daily returns is NOT reported: at ~9 %/day volatility a 10–20 % error in the
        # daily equity denominator moves the compounded result by more than 100 pp. The official intervals are used.
        lev_med = _med([g for _, g, _ in lev])
        iv = interval_returns(av, pnl, candles, lf)
        if len(iv) > 5:
            ri = ols([r[1] for r in iv], [[r[3] for r in iv]])
            span = _med([(b[0] - a[0]) / DAY_MS for a, b in zip(iv, iv[1:], strict=False)])
            print(
                f"  official intervals (n={len(iv)}, ~{span:.0f} d each): beta on alt basket {ri['coef'][1]:.2f}, "
                f"alpha {ri['coef'][0]:+.2%}/interval (t {ri['t'][0]:.1f}), R² {ri['r2']:.2f}"
            )
            his = list(np.cumprod([1 + r[1] for r in iv]))
            print(
                f"  official TWR over those intervals {his[-1] - 1:+.1%} (max DD {max_drawdown(his)[0]:.1%}) vs mechanical long, re-levered at each interval:"
            )
            for name, k_idx in (("BTC", 2), ("EW alts", 3)):
                for k in (1.0, 2.0, lev_med):
                    path = list(np.cumprod([max(0.0, 1 + k * r[k_idx]) for r in iv]))
                    print(f"    {k:.1f}x {name:8s}: TWR {path[-1] - 1:+.1%}, max DD {max_drawdown(path)[0]:.1%}")
        third = len(y) // 3
        for lo, hi in ((0, third), (third, 2 * third), (2 * third, len(y))):
            r = ols(y[lo:hi], [xb[lo:hi], xa[lo:hi]])
            print(f"  sub-period {ymd(used[lo])}…{ymd(used[hi - 1])}: alpha {r['coef'][0]:+.2%}/day (t {r['t'][0]:.1f}), R² {r['r2']:.2f}")
        levd = {d: g for d, g, _ in lev}
        lv = [levd[d] for d in used]
        nxt = [float(np.prod([1 + v for v in xa[i + 1 : i + 8]]) - 1) for i in range(len(xa) - 7)]
        past = [float(np.prod([1 + v for v in xa[i - 6 : i + 1]]) - 1) for i in range(7, len(xa))]
        dlev = [lv[i] - lv[i - 7] for i in range(7, len(lv))]
        print(
            f"  timing (exploratory): corr(leverage, alt basket next 7d) {np.corrcoef(lv[: len(nxt)], nxt)[0, 1]:+.2f}; "
            f"corr(7d change in leverage, alt basket same 7d) {np.corrcoef(dlev, past)[0, 1]:+.2f}"
        )

    # 4. daily episodes
    eps = daily_episodes(pos, last_day)
    rows = [(ep, episode_pnl(ep, pos, candles)) for ep in eps]
    closed = [(ep, r) for ep, r in rows if not ep.open_at_end and not math.isnan(r["pnl"])]
    print(f"\n## Position episodes (daily resolution): {len(eps)} total, {sum(e.open_at_end for e in eps)} open at the end of the data")
    sides = Counter("long" if e.side > 0 else "short" for e in eps)
    print(f"  sides: {dict(sides)}; coins: {len({e.coin for e in eps})}")
    dur = [len(e.days) for e in eps]
    print(f"  holding days: {_q([float(d) for d in dur], False)}")
    print(
        f"  adds per episode (days |size| +{SIZE_CHANGE:.0%}): median {_med([float(len(e.adds)) for e in eps]):.0f}, max {max(len(e.adds) for e in eps)}; trims median {_med([float(len(e.trims)) for e in eps]):.0f}"
    )
    if closed:
        pn = [r["pnl"] for _, r in closed]
        wins = [p for p in pn if p > 0]
        print(f"  closed episodes n={len(closed)}: win rate {len(wins) / len(pn):.0%}, total {_usd(sum(pn))}, median {_usd(_med(pn))}")
        lose_d = [len(e.days) for e, r in closed if r["pnl"] <= 0]
        win_d = [len(e.days) for e, r in closed if r["pnl"] > 0]
        print(f"  median holding: winners {_med([float(x) for x in win_d]):.0f} d, losers {_med([float(x) for x in lose_d]):.0f} d")
        sized = [(e, r) for e, r in closed if r["max_notional"] > 0]
        print(f"  closed losers, loss / max notional: {_q([r['pnl'] / r['max_notional'] for _, r in sized if r['pnl'] <= 0])}")
        print(f"  closed, worst open loss / max notional: {_q([r['worst'] / r['max_notional'] for _, r in sized])}")
    for s, name in ((1, "long"), (-1, "short")):
        sel = [r["pnl"] for e, r in rows if e.side == s and not math.isnan(r["pnl"])]
        if sel:
            print(f"  {name} episodes: n={len(sel)}, PnL {_usd(sum(sel))}")
    print(
        f"  PnL of episodes still open at the end {_usd(sum(r['pnl'] for e, r in rows if e.open_at_end and not math.isnan(r['pnl'])))}, of closed ones {_usd(sum(r['pnl'] for _, r in closed))}"
    )
    allp = sorted((r["pnl"] for _, r in rows if not math.isnan(r["pnl"])), reverse=True)
    if allp:
        tot = sum(allp)
        print(
            f"  all episodes incl. open: total {_usd(tot)}; top-10 = {_usd(sum(allp[:10]))} ({sum(allp[:10]) / tot:.0%} of total)"
            if tot
            else ""
        )
    print("  biggest episodes by |PnL|:")
    for ep, r in sorted(rows, key=lambda x: -abs(x[1]["pnl"]) if not math.isnan(x[1]["pnl"]) else 0)[:15]:
        print(
            f"    {ep.coin:8s} {'L' if ep.side > 0 else 'S'} {ymd(ep.start)}→{ymd(ep.end)}{'*' if ep.open_at_end else ' '} {len(ep.days):3d}d "
            f"max {_usd(r['max_notional'])} pnl {_usd(r['pnl'])} worst {_usd(r['worst'])} funding {_usd(r['funding'])} adds {len(ep.adds)} trims {len(ep.trims)}"
        )

    # 5. entry / add context vs the baseline of all coin-days in his universe
    if btc:
        events = [(ep.coin, ep.start, "new") for ep in eps if ep.start > min(book)] + [(ep.coin, d, "add") for ep in eps for d in ep.adds]
        trims = [(ep.coin, d) for ep in eps for d in ep.trims]
        universe = {e.coin for e in eps if e.coin in candles}
        base = [entry_features(candles[c], btc, d) for c in universe for d in days if d in candles[c]]
        ev = [entry_features(candles[c], btc, d) for c, d, _ in events if c in candles]
        tr = [entry_features(candles[c], btc, d) for c, d in trims if c in candles]
        print(
            f"\n## Entry / add context (exploratory; quantiles p10/p25/med/p75/p90). entries+adds n={len(ev)}, trims n={len(tr)}, baseline coin-days n={len(base)}"
        )
        for k in ("ret3", "ret7", "ret30", "from_high30", "pos_in_range30", "btc_ret3", "fwd7", "fwd14", "fwd30"):
            print(f"  {k:15s} entries {_q([e[k] for e in ev])}")
            print(f"  {'':15s} trims   {_q([e[k] for e in tr])}")
            print(f"  {'':15s} base    {_q([e[k] for e in base])}")
        per_day = Counter(d for _, d, _ in events)
        print(f"  entry/add days: {len(per_day)}; coins per such day {_q([float(v) for v in per_day.values()], False)}")
        top = per_day.most_common(8)
        print("  busiest entry days: " + ", ".join(f"{ymd(d)} ({n})" for d, n in top))
        wd = Counter(datetime.fromtimestamp(d / 1000, UTC).strftime("%a") for _, d, _ in events)
        print(f"  weekday of entries/adds: {dict(wd)}")

    # 6. fills (exact, but only the recent window the API still serves)
    fills = parse_fills(data["fills"], withdrawal_times(ledger))
    if fills:
        print(f"\n## Fills {ymd(fills[0].t)} … {ymd(fills[-1].t)} (n={len(fills)}; the API keeps only recent fills)")
        opens = [f for f in fills if abs(f.end) > abs(f.start)]
        closes = [f for f in fills if abs(f.end) < abs(f.start)]
        forced = [f for f in closes if f.forced]
        notional = lambda fs: sum(abs(f.delta) * f.px for f in fs)  # noqa: E731
        print(
            f"  opens n={len(opens)} {_usd(notional(opens))} (maker {sum(f.maker for f in opens) / max(1, len(opens)):.0%}); longs {sum(f.delta > 0 for f in opens)}, shorts {sum(f.delta < 0 for f in opens)}"
        )
        print(
            f"  closes n={len(closes)} {_usd(notional(closes))}: caused by vault withdrawals {len(forced)} / {_usd(notional(forced))}; leader closes maker {sum(f.maker for f in closes if not f.forced) / max(1, len(closes) - len(forced)):.0%}"
        )
        lead = [f for f in closes if not f.forced]
        print(
            f"  gain from avg entry at leader closes: {_q([f.close_gain for f in lead])}; at forced closes: {_q([f.close_gain for f in forced])}"
        )
        print(
            f"  realized PnL: leader closes {_usd(sum(f.closed_pnl for f in lead))}, forced closes {_usd(sum(f.closed_pnl for f in forced))}, fees {_usd(-sum(f.fee for f in fills))}"
        )
        print("  last 14 days, USD notional (opens / leader closes / forced closes):")
        for day in range(day_of(fills[-1].t) - 13 * DAY_MS, day_of(fills[-1].t) + DAY_MS, DAY_MS):
            sel = [f for f in fills if day_of(f.t) == day]
            o = notional([f for f in sel if abs(f.end) > abs(f.start)])
            c_lead = notional([f for f in sel if abs(f.end) < abs(f.start) and not f.forced])
            c_forced = notional([f for f in sel if f.forced])
            print(f"    {ymd(day)}  {_usd(o):>8s} / {_usd(c_lead):>8s} / {_usd(c_forced):>8s}")
        hours = Counter(datetime.fromtimestamp(f.t / 1000, UTC).hour for f in opens)
        print("  open fills by UTC hour: " + " ".join(f"{h}:{hours.get(h, 0)}" for h in range(24)))
        feps = build_episodes(fills)
        print(
            f"  fill episodes: {len(feps)} ({sum(e.truncated for e in feps)} started before the window, {sum(e.t_close is None for e in feps)} still open)"
        )
        dirs: Counter = Counter()
        for e in feps:
            dirs += e.add_direction()
        print(f"  adds after the first (1h clusters): below running avg entry {dirs['down']}, above {dirs['up']}")
        full = [e for e in feps if not e.truncated and e.t_close is not None]
        if full:
            pn = [e.realized - e.fees for e in full]
            hold = [(e.t_close - e.t_open) / HOUR_MS for e in full]
            print(
                f"  complete episodes n={len(full)}: win rate {sum(p > 0 for p in pn) / len(pn):.0%}, total {_usd(sum(pn))}, holding hours {_q(hold, False)}"
            )
            pn_sorted = sorted(pn, reverse=True)
            if sum(pn):
                print(f"  top-10 complete episodes = {sum(pn_sorted[:10]) / sum(pn):.0%} of their total")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vault")
    ap.add_argument("--fills", type=Path, help="use this userFillsByTime JSON instead of fetching")
    ap.add_argument("--data-dir", type=Path, help="cache directory (default: <tmp>/vault_profile/<vault>)")
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = ap.parse_args(argv)
    vault = args.vault.lower()
    data_dir = args.data_dir or Path(tempfile.gettempdir()) / "vault_profile" / vault
    data = load_data(vault, data_dir, args.fills, args.refresh)
    pos = daily_positions(data["funding"])
    coins = set(pos) | {"BTC"} | set(ALT_BASKET)
    av = series(data["details"], "accountValueHistory")
    candles = load_candles(coins, av[0][0], av[-1][0] + 31 * DAY_MS, data_dir, args.refresh)
    report(vault, data, candles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
