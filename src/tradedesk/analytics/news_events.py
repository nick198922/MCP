"""Official-news events (exchange delistings, hacks, shutdowns): price reaction and a delayed short on the perp.

Pure functions (no I/O). Time is passed in as epoch milliseconds. Returns are decimals on notional (0.10 = 10 %);
funding rates are decimals per settlement (positive: longs pay shorts, so a short receives them).
Idea card: docs/ideas/IDEA-20261017-news-events.md (section "Попередня реєстрація").

Announcement titles are untrusted text: they are only matched against fixed patterns to extract tickers; nothing in
them is ever executed or used as an instruction.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import random
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

STABLES = frozenset(
    {"USDC", "USDT", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDS", "USDE", "USD1", "PYUSD", "AEUR", "EUR", "IDRT"}
    | {"MUSD", "USDD", "CUSD", "PAXG", "XAUT", "WBTC", "WETH", "WBETH", "STETH", "CMETH", "WEETH", "LBTC"}
)
MULTIPLIERS = (1, 1000, 10000, 1000000, 10000000)

# ------------------------------------------------------------------ title parsing (untrusted text -> tickers)

_TICK = re.compile(r"^[A-Z0-9]{1,15}$")
_SPLIT = re.compile(r"\s*(?:,|&|\band\b)\s*")


def _tickers(chunk: str) -> list[str]:
    out: list[str] = []
    for part in _SPLIT.split(chunk.strip()):
        part = part.strip().lstrip("$")
        m = re.fullmatch(r"[^()]*\(([A-Z0-9]{1,15})\)", part)  # "Metachain (MTC)" -> MTC
        if m:
            part = m.group(1)
        part = re.sub(r"\s*\(.*\)$", "", part)  # "SLX (SLIMEX)" -> SLX
        if _TICK.match(part) and part not in STABLES:
            out.append(part)
    return list(dict.fromkeys(out))


def perp_base(symbol: str) -> tuple[str, int]:
    """``1000PEPEUSDT`` -> (``PEPE``, 1000); ``BLURUSDT`` -> (``BLUR``, 1). Only the listed multiplier prefixes count,
    so ``1INCHUSDT`` stays ``1INCH``."""
    s = symbol.upper()
    for suf in ("USDT", "USDC"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    for mult in sorted(MULTIPLIERS[1:], reverse=True):
        p = str(mult)
        if s.startswith(p) and len(s) > len(p) and not s[len(p)].isdigit():
            return s[len(p) :], mult
    return s, 1


def perp_symbols(text: str) -> list[str]:
    """USDT-margined perp symbols mentioned in a text (``XYZUSDT``), in order, without duplicates."""
    return list(dict.fromkeys(re.findall(r"\b([0-9A-Z]{2,25}USDT)\b", text)))


_IGNORE = re.compile(
    r"Margin|Loan|Alpha|Leveraged|Earn|Collateral|Borrow|Grid|Dual Investment|Convert|Migration|Rebrand|swap|"
    r"Trading Pair|Spot Pair|Pairs|TradFi|stock|Inverse|USDC Perpetual|Network|withdrawal|Postponed|Process|"
    r"Cease Support|Phasing|Relisting|Deposits",
    re.I,
)


def classify_delisting(source: str, title: str) -> tuple[str, list[str]]:
    """Kind and tickers of one delisting-catalog announcement.

    Kinds: ``spot_delist`` (the exchange removes the token from spot), ``perp_delist`` (it removes the USDT perp;
    tickers are perp symbols), ``needs_body`` (tickers only in the article body), ``ignore``."""
    t = " ".join(title.split())
    if source == "binance":
        if re.match(r"^Binance Futures Will Delist", t) or re.match(r"^Delisting of USD.-M", t):
            if "Postponed" in t:
                return "ignore", []
            syms = [s for s in perp_symbols(t) if not s.endswith("BUSD")]
            if syms:
                return "perp_delist", syms
            if "Multiple" in t:
                return "needs_body", []
            return "ignore", []
        if _IGNORE.search(t):
            return "ignore", []
        m = re.search(r"\bWill Delist (.+?)(?: on \d{4}-\d{2}-\d{2}| and Support|$)", t)
        if m:
            return ("spot_delist", ticks) if (ticks := _tickers(m.group(1))) else ("ignore", [])
        return "ignore", []
    if source == "bybit":
        if re.search(r"Perpetual\s+Contract", t, re.I):
            if re.search(r"Selected|USDC|Inverse|Pre-IPO", t, re.I):
                return ("needs_body", []) if "Selected" in t else ("ignore", [])
            syms = perp_symbols(t)
            return ("perp_delist", syms) if syms else ("ignore", [])
        if re.match(r"^Bybit to Delist \d+ Token\(s\)", t) or t == "Spot Token Delisting Notice":
            return "needs_body", []
        if _IGNORE.search(t) or re.search(r"Contracts?\b|Bybit Alpha|Spot trading pair", t, re.I):
            return "ignore", []
        m = re.match(r"^(?:Notice on )?Delisting(?: of)?\s+(.+?)(?: on Bybit| on [A-Z][a-z]{2} \d{1,2}, \d{4})?$", t)
        if m:
            ticks = _tickers(m.group(1))
            return ("spot_delist", ticks) if ticks else ("ignore", [])
        return "ignore", []
    if source == "okx":
        if re.search(r"\bin EEA\b|EEA|migration", t, re.I):
            return "ignore", []
        m = re.match(r"^OKX to delist (.+?)(?: spot trading pairs?| perpetual.*)?$", t, re.I)
        if m:
            return ("spot_delist", ticks) if (ticks := _tickers(m.group(1))) else ("ignore", [])
        return "ignore", []
    return "ignore", []


def classify_listing(title: str) -> list[str]:
    """Tickers of a Binance spot listing (``Binance Will List Hyperliquid (HYPE) ...``); empty for anything else."""
    t = " ".join(title.split())
    if not re.match(r"^Binance Will List\b", t) or re.search(r"Futures|Perpetual|Margin|Pre-Market|Alpha|Earn|Options", t):
        return []
    return [x for x in dict.fromkeys(re.findall(r"\(([A-Z0-9]{2,15})\)", t)) if x not in STABLES]


# ------------------------------------------------------------------ events


@dataclass(frozen=True)
class Event:
    token: str
    ms: int  # announcement time (epoch ms, UTC)
    category: str  # spot_delist | perp_delist | hack | shutdown | listing
    source: str  # binance | bybit | okx | defillama | curated
    cluster: str  # one announcement = one cluster
    precise: bool = True  # False: only the UTC day is known (hacks)
    note: str = ""


def dedupe(events: Iterable[Event], window_ms: int = 3 * DAY_MS) -> tuple[list[Event], list[Event]]:
    """Keep the first event of a token; drop later events of the same token that start within ``window_ms`` of the
    last kept one (their windows overlap). Returns (kept, dropped); input order does not matter."""
    kept: list[Event] = []
    dropped: list[Event] = []
    last: dict[str, int] = {}
    for e in sorted(events, key=lambda e: (e.ms, e.token, e.category, e.source)):
        if e.token in last and e.ms - last[e.token] < window_ms:
            dropped.append(e)
            continue
        kept.append(e)
        last[e.token] = e.ms
    return kept, dropped


def hack_events(
    hacks: Iterable[Mapping[str, object]],
    protocols: Iterable[Mapping[str, object]],
    *,
    min_amount: float,
    start_ms: int,
    end_ms: int,
) -> list[Event]:
    """DefiLlama hacks with a known token symbol (via ``defillamaId`` -> protocol ``symbol``), amount >= min_amount.
    The time is the UTC day of the hack (``precise=False``)."""
    sym = {str(p.get("id")): str(p.get("symbol") or "") for p in protocols}
    out: list[Event] = []
    for h in hacks:
        try:
            ms = int(float(h.get("date") or 0) * 1000)  # type: ignore[arg-type]
            amt = float(h.get("amount") or 0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not (start_ms <= ms < end_ms) or amt < min_amount:
            continue
        s = sym.get(str(h.get("defillamaId") or ""), "").upper()
        if not _TICK.match(s) or s in STABLES or s == "-":
            continue
        day = ms // DAY_MS * DAY_MS
        out.append(Event(s, day, "hack", "defillama", f"hack:{day}:{s}", precise=False, note=f"{h.get('name')} ${amt / 1e6:.1f}M"))
    return out


# ------------------------------------------------------------------ stage 0b: curated closures (CSV, Claude's set)

CLOSURE_WHOLE = frozenset({"shutdown", "chain_sunset", "wind_down_distribution"})  # the whole project ends
CLOSURE_PARTIAL = frozenset({"product_sunset", "alliance_exit", "governance_negative"})  # a part of it
OFFICIAL_TIME = frozenset({"official_tweet", "contributor_tweet"})
HYPOTHESIS_SOURCES = frozenset({"BLUR", "BLAST"})  # the owner's examples the hypothesis came from


@dataclass(frozen=True)
class Closure:
    """One row of ``docs/verification/2026-10-17-news-closures.csv`` (time in epoch ms UTC)."""

    token: str
    ms: int
    type: str
    scope: str
    direction_prior: str  # down | ambiguous | up
    time_source: str  # official_tweet | contributor_tweet | secondary_upper_bound | leak
    url: str
    note: str

    @property
    def key(self) -> str:
        return f"{self.token}@{self.ms}"


def parse_closures(text: str) -> list[Closure]:
    """Rows of the closures CSV (header ``token,t0_utc,type,scope,direction_prior,time_source,url,note``; t0 as
    ``YYYY-MM-DD HH:MM:SS`` UTC). The text is untrusted: fields are only stored, never interpreted."""
    out: list[Closure] = []
    for r in csv.DictReader(io.StringIO(text)):
        ms = int(datetime.strptime(r["t0_utc"].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000)
        out.append(
            Closure(
                r["token"].strip().upper(),
                ms,
                r["type"].strip(),
                r["scope"].strip(),
                r["direction_prior"].strip(),
                r["time_source"].strip(),
                r["url"].strip(),
                r["note"].strip(),
            )
        )
    return out


TWITTER_EPOCH_MS = 1288834974657


def snowflake_ms(tweet_id: int) -> int:
    """Creation time (epoch ms UTC) encoded in an X/Twitter snowflake ID: (id >> 22) + Twitter epoch."""
    return (int(tweet_id) >> 22) + TWITTER_EPOCH_MS


def closure_exclusion(c: Closure) -> str | None:
    """Why a closure row is outside the registered primary pool of stage 0b (None: it is in the pool)."""
    if c.time_source == "leak" or c.type == "leak":
        return "leak"
    if c.type == "hack":
        return "hack (stage 0d)"
    if c.direction_prior == "up":
        return "direction prior up"
    if c.token in HYPOTHESIS_SOURCES:
        return "hypothesis source"
    return None


# ------------------------------------------------------------------ speed: how much of the 24 h move is early


def move_share_pooled(r_h: Sequence[float], r_24: Sequence[float]) -> float:
    """Share of the 24 h move that happened by horizon h, pooled: mean r(h) / mean r(24 h). NaN if mean r(24 h) = 0."""
    if not r_h or len(r_h) != len(r_24):
        return float("nan")
    den = statistics.fmean(r_24)
    return statistics.fmean(r_h) / den if den != 0 else float("nan")


def move_share_bootstrap(
    r_h: Sequence[float], r_24: Sequence[float], *, n_boot: int = 10000, seed: int = 5, alpha: float = 0.05
) -> tuple[float, float, float]:
    """(pooled share, lower, upper): percentile CI of mean r(h) / mean r(24 h), resampling events (pairs kept)."""
    est = move_share_pooled(r_h, r_24)
    n = len(r_h)
    if n < 2:
        return est, float("nan"), float("nan")
    a, z = np.asarray(r_h, dtype=float), np.asarray(r_24, dtype=float)
    idx = np.random.default_rng(seed).integers(0, n, size=(n_boot, n))
    den = z[idx].mean(axis=1)
    ok = den != 0
    ratios = a[idx].mean(axis=1)[ok] / den[ok]
    if not len(ratios):
        return est, float("nan"), float("nan")
    lo, hi = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    return est, float(lo), float(hi)


def move_share_median(r_h: Sequence[float], r_24: Sequence[float], *, min_abs_24: float = 0.05) -> tuple[float, int]:
    """(median of per-event r(h) / r(24 h), n) over events with |r(24 h)| >= ``min_abs_24`` (small moves make the
    per-event ratio meaningless)."""
    xs = [a / z for a, z in zip(r_h, r_24, strict=True) if abs(z) >= min_abs_24]
    return (statistics.median(xs) if xs else float("nan")), len(xs)


# ------------------------------------------------------------------ price paths


@dataclass(frozen=True)
class Bars:
    """OHLC bars of one instrument, sorted by open time; ``step`` is the bar length in ms."""

    t: np.ndarray  # int64 open times
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    step: int = MIN_MS
    v: np.ndarray | None = None  # base volume per bar, when the rows carry it (column 5)

    @staticmethod
    def from_rows(rows: Sequence[Sequence[float]], step: int = MIN_MS) -> Bars:
        """Rows ``[open_ms, open, high, low, close, (volume), ...]`` (any order, duplicates by open time collapse to the
        last). Volume is kept only if every row has it."""
        d = {int(r[0]): r for r in rows}
        ks = sorted(d)
        a = np.array([d[k][:5] for k in ks], dtype=float).reshape(-1, 5)
        v = np.array([float(d[k][5]) for k in ks]) if ks and all(len(d[k]) > 5 for k in ks) else None
        return Bars(np.array(ks, dtype=np.int64), a[:, 2], a[:, 3], a[:, 4], step, v)

    def __len__(self) -> int:
        return len(self.t)


def _last_closed(b: Bars, ms: int) -> int:
    """Index of the last bar that closed at or before ``ms`` (-1 if none)."""
    return int(np.searchsorted(b.t, ms - b.step, side="right")) - 1


def price_at(b: Bars, ms: int, *, max_stale_ms: int = 30 * MIN_MS) -> float | None:
    """Close of the last bar that closed at or before ``ms``; None if there is none or it closed more than
    ``max_stale_ms`` earlier (a gap or the end of the series)."""
    i = _last_closed(b, ms)
    if i < 0 or ms - (int(b.t[i]) + b.step) > max_stale_ms:
        return None
    return float(b.c[i])


def perp_live(
    b: Bars,
    t: int,
    *,
    range_window_ms: int = DAY_MS,
    vol_window_ms: int = HOUR_MS,
    max_stale_ms: int = 30 * MIN_MS,
) -> bool:
    """Was the instrument still trading at ``t``? Uses only bars that closed at or before ``t``.

    After a perp is settled (delisted) the Binance archive keeps publishing 1m bars at the constant settlement price
    with zero volume, so "a recent bar exists" is not enough. Live means: a bar closed within ``max_stale_ms``, the
    price range (max high - min low) over the bars closed in (t - range_window_ms, t] is positive, and -- when the bars
    carry volume -- the volume over the bars closed in (t - vol_window_ms, t] is positive."""
    if price_at(b, t, max_stale_ms=max_stale_ms) is None:
        return False
    i1 = _last_closed(b, t)
    i0 = int(np.searchsorted(b.t, t - range_window_ms, side="left"))  # first bar opening at/after the window start
    if i1 < i0:
        return False
    if not float(b.h[i0 : i1 + 1].max()) > float(b.lo[i0 : i1 + 1].min()):
        return False
    if b.v is not None:
        j0 = int(np.searchsorted(b.t, t - vol_window_ms, side="left"))
        if i1 < j0 or not float(b.v[j0 : i1 + 1].sum()) > 0:
            return False
    return True


def reaction(b: Bars, t0: int, dt: int, *, max_stale_ms: int = 30 * MIN_MS) -> float | None:
    """Price change from the announcement to ``t0 + dt``: p(t0 + dt) / p(t0) - 1 (p = last closed bar)."""
    p0 = price_at(b, t0, max_stale_ms=max_stale_ms)
    p1 = price_at(b, t0 + dt, max_stale_ms=max_stale_ms)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return p1 / p0 - 1.0


@dataclass(frozen=True)
class Trade:
    gross: float  # short return on notional before costs: 1 - p_exit / p_entry (or -stop)
    funding: float  # funding received by the short (sum of rates in (entry, exit])
    cost: float  # fees + slippage, both sides
    net: float
    mae: float  # max adverse excursion: max high after entry / p_entry - 1
    truncated: bool  # the series ended before the planned exit (delisting): closed at the last price
    stopped: bool
    exit_ms: int


def short_trade(
    b: Bars,
    entry_ms: int,
    exit_ms: int,
    *,
    cost_side: float,
    funding: Sequence[tuple[int, float]] = (),
    stop: float | None = None,
    max_stale_ms: int = 30 * MIN_MS,
    sign: float = 1.0,
    stop_fill: str = "level",
) -> Trade | None:
    """Short (``sign=1``) or long (``sign=-1``) the perp at ``entry_ms`` and close at ``exit_ms``.

    Entry and exit use the close of the last bar closed by that time; only bars that open at or after the entry are
    used for the stop and the excursion, so nothing after a time t changes results before t. A stop (0.25 = 25 %
    against the position) fills at the stop level on the first bar that touches it (gaps fill worse in reality);
    ``stop_fill="extreme"`` fills it at that bar's high (short) / low (long) instead -- the worst price of the bar.
    If the series ends before ``exit_ms`` the position closes at the last close (``truncated``)."""
    p0 = price_at(b, entry_ms, max_stale_ms=max_stale_ms)
    if p0 is None or p0 <= 0:
        return None
    i0 = int(np.searchsorted(b.t, entry_ms, side="left"))
    i1 = _last_closed(b, exit_ms)
    if i1 < i0:  # no bar after the entry
        return None
    seg_h, seg_l = b.h[i0 : i1 + 1], b.lo[i0 : i1 + 1]
    adverse = seg_h / p0 - 1.0 if sign > 0 else 1.0 - seg_l / p0
    stopped = False
    end_ms = exit_ms
    if stop is not None:
        hit = np.nonzero(adverse >= stop)[0]
        if len(hit):
            k = int(hit[0])
            stopped = True
            end_ms = int(b.t[i0 + k]) + b.step
            adverse = adverse[: k + 1]
    mae = float(adverse.max()) if len(adverse) else 0.0
    truncated = False
    if stopped:
        gross = -float(stop) if stop_fill == "level" else -float(adverse[-1])  # type: ignore[arg-type]
    else:
        last_end = int(b.t[i1]) + b.step
        truncated = exit_ms - last_end > max_stale_ms
        if truncated:
            end_ms = last_end
        p1 = float(b.c[i1])
        gross = (1.0 - p1 / p0) if sign > 0 else (p1 / p0 - 1.0)
    fund = sign * sum(r for ms, r in funding if entry_ms < ms <= end_ms)
    cost = 2.0 * cost_side
    return Trade(gross, fund, cost, gross + fund - cost, mae, truncated, stopped, end_ms)


def max_move(b: Bars, a_ms: int, z_ms: int, p_ref: float) -> tuple[float, float] | None:
    """(max high / p_ref - 1, min low / p_ref - 1) over bars that open in [a_ms, z_ms)."""
    i, j = int(np.searchsorted(b.t, a_ms, side="left")), int(np.searchsorted(b.t, z_ms, side="left"))
    if j <= i or p_ref <= 0:
        return None
    return float(b.h[i:j].max() / p_ref - 1.0), float(b.lo[i:j].min() / p_ref - 1.0)


# ------------------------------------------------------------------ placebo


def placebo_times(
    key: str,
    t0_ms: int,
    n: int,
    *,
    lo_days: int = 120,
    hi_days: int = 7,
    blocked: Iterable[int] = (),
    block_ms: int = 4 * DAY_MS,
    grid_ms: int = HOUR_MS,
) -> list[int]:
    """``n`` pseudo-random hour starts in [t0 - lo_days, t0 - hi_days], seeded by ``key`` (reproducible), at least
    ``block_ms`` away from every ``blocked`` time (other events of the token). Only times before the event are used,
    so the placebo window never contains the event's own reaction."""
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))
    a = (t0_ms - lo_days * DAY_MS) // grid_ms
    z = (t0_ms - hi_days * DAY_MS) // grid_ms
    bl = sorted(blocked)
    out: list[int] = []
    tries = 0
    while len(out) < n and tries < 50 * n and z > a:
        tries += 1
        t = rng.randrange(a, z) * grid_ms
        if any(abs(t - x) < block_ms for x in bl) or t in out:
            continue
        out.append(t)
    return sorted(out)


# ------------------------------------------------------------------ statistics


def cluster_bootstrap(
    values: Sequence[float], clusters: Sequence[object], *, n_boot: int = 10000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float, float]:
    """(mean, lower, upper): percentile CI of the mean, resampling whole clusters (announcements)."""
    if not values:
        return float("nan"), float("nan"), float("nan")
    g: dict[object, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        g.setdefault(c, []).append(float(v))
    sums = np.array([sum(v) for v in g.values()])
    cnts = np.array([len(v) for v in g.values()], dtype=float)
    k = len(sums)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n_boot, k))
    means = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(sum(values) / len(values)), float(lo), float(hi)


def paired_diff_bootstrap(
    event: Sequence[float], placebo: Sequence[float], clusters: Sequence[object], *, n_boot: int = 10000, seed: int = 11
) -> tuple[float, float, float]:
    """Mean of (event - placebo mean of the same event) with a cluster-bootstrap CI; inputs are aligned per event."""
    d = [e - p for e, p in zip(event, placebo, strict=True)]
    return cluster_bootstrap(d, clusters, n_boot=n_boot, seed=seed)


def cluster_sd(values: Sequence[float], clusters: Sequence[object]) -> tuple[float, int]:
    """(SD of cluster means, number of clusters): the effective unit for the power gate."""
    g: dict[object, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        g.setdefault(c, []).append(float(v))
    means = [statistics.fmean(v) for v in g.values()]
    return (statistics.stdev(means) if len(means) > 1 else float("nan")), len(means)


def mde_clusters(sd_cluster_means: float, k: int, *, z: float = 2.8) -> float:
    """Minimum detectable mean effect (5 % two-sided, 80 % power) with k independent clusters."""
    return z * sd_cluster_means / math.sqrt(k) if k > 0 else float("inf")


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> tuple[float, int, float, float]:
    """(Q, df, I^2, p) for heterogeneity of group means given their standard errors (chi-square, df = k - 1)."""
    w = [1.0 / (s * s) for s in ses if s > 0]
    m = [x for x, s in zip(means, ses, strict=True) if s > 0]
    k = len(w)
    if k < 2:
        return float("nan"), 0, float("nan"), float("nan")
    mu = sum(wi * mi for wi, mi in zip(w, m, strict=True)) / sum(w)
    q = sum(wi * (mi - mu) ** 2 for wi, mi in zip(w, m, strict=True))
    df = k - 1
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    return q, df, i2, _chi2_sf(q, df)


def _chi2_sf(x: float, k: int) -> float:
    """Upper tail of the chi-square distribution (regularised gamma, series / continued fraction)."""
    if x <= 0:
        return 1.0
    a, z = k / 2.0, x / 2.0
    if z < a + 1:
        term = s = 1.0 / a
        n = a
        for _ in range(500):
            n += 1
            term *= z / n
            s += term
            if abs(term) < abs(s) * 1e-14:
                break
        return max(0.0, 1.0 - s * math.exp(-z + a * math.log(z) - math.lgamma(a)))
    b = z + 1 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1) < 1e-14:
            break
    return min(1.0, math.exp(-z + a * math.log(z) - math.lgamma(a)) * h)
