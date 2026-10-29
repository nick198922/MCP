"""Binance "Futures Will Launch USDⓈ-Margined ... Perpetual Contract" announcements for tokens that already trade as
perps elsewhere (IDEA-20261029-binance-perp-launch-existing-token).

Pure functions, no I/O: title classification, flattening of the CMS article body, the launch time L parsed from the
body, the venue/age eligibility rule and the placebo outcome used for the power gate. Announcement titles and bodies
are untrusted text: they are only matched against fixed patterns, nothing in them is executed or followed.
Rates and returns are decimals (0.025 = 2.5 %).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from tradedesk.analytics.news_events import DAY_MS, HOUR_MS, MIN_MS, MULTIPLIERS, perp_base

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "MIN_MS",
    "beta",
    "body_text",
    "classify_launch_title",
    "eligible",
    "hedged_short",
    "launch_from_title",
    "launch_times",
    "placebo_hours",
    "venue_symbols",
]

_SYM = re.compile(r"\b([0-9A-Z]{1,25})(USDT|USDC)\b")


def classify_launch_title(title: str) -> tuple[str, list[str]]:
    """(kind, USDT perp symbols) of a catalog-48 title.

    Kinds: ``launch`` (a new USDⓈ-M USDT perp), ``tradfi`` (equity / index / commodity perps), ``multiple``
    (symbols only in the body), ``premarket`` (pre-market perp), ``usdc`` (USDC-margined only), ``other_futures``
    (any other "Binance Futures Will Launch" title: coin-margined, quarterly, ...), ``none`` (not a launch title).
    Two title formats: "USDⓈ-Margined XUSDT and YUSDT Perpetual Contracts" (2024+) and "USDⓈ-M X and Y Perpetual
    Contracts With Up to 50x Leverage" (2023-2024; the ``USDT`` suffix is implied)."""
    t = " ".join(title.replace("Ⓢ", "S").split())
    if not re.match(r"^Binance Futures Will Launch\b", t, re.I):
        return "none", []
    if re.search(r"Pre-?Market", t, re.I):
        return "premarket", []
    if not re.search(r"Perpetual", t, re.I) or re.search(r"Coin-?Margined|COIN-M|Quarterly|Delivery|Options", t, re.I):
        return "other_futures", []
    if re.search(r"Equity|Index Perpetual|Stock|Commodit|TradFi", t, re.I):
        return "tradfi", []
    if re.search(r"\bMultiple\b", t, re.I):
        return "multiple", []
    syms = list(dict.fromkeys(a + q for a, q in _SYM.findall(t)))
    usdt = [s for s in syms if s.endswith("USDT")]
    if usdt:
        return "launch", usdt
    if any(s.endswith("USDC") for s in syms):
        return "usdc", []
    m = re.search(r"(?:USDS-M(?:argined)?|USDT-Margined)\s+(.+?)\s+Perpetual Contracts?\b", t, re.I)
    if m:
        ticks = []
        for part in re.split(r"\s*(?:,|&|\band\b)\s*", m.group(1)):
            part = part.strip()
            k = re.fullmatch(r"[^()]*\(([0-9A-Z]{1,20})\)", part)  # "Threshold (T)" -> T
            if k:
                part = k.group(1)
            if re.fullmatch(r"[0-9A-Z]{1,20}", part):
                ticks.append(part + "USDT")
        if ticks:
            return "launch", list(dict.fromkeys(ticks))
    return "other_futures", []


def body_text(body: str) -> str:
    """Plain text of a Binance CMS article body (a JSON tree of nodes with ``text`` leaves); a non-JSON body is
    returned with tags stripped and whitespace collapsed."""
    try:
        node = json.loads(body)
    except (ValueError, TypeError):
        return " ".join(re.sub(r"<[^>]+>", " ", body or "").split())
    parts: list[str] = []

    def walk(x: object) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("text"), str):
                parts.append(x["text"])
            for k, v in x.items():
                if k != "text":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and not parts and x.strip().startswith(("{", "[")):
            walk(json.loads(x))

    walk(node)
    if not parts and isinstance(node, str):
        return " ".join(re.sub(r"<[^>]+>", " ", node).split())
    return " ".join(" ".join(parts).split())


_DT = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*\(?\s*UTC\s*\)?")


_TABLE = re.compile(
    r"Perpetual Contracts?\s+((?:[0-9A-Z]{1,25}USDT\s+)+)Launch Time\s*((?:\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*\(UTC\)\s*)+)", re.I
)


def _times(text: str) -> list[tuple[int, int]]:
    out = []
    for m in _DT.finditer(text):
        y, mo, d, h, mi = (int(g) for g in m.groups())
        try:
            ms = int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)
        except ValueError:
            continue
        out.append((m.start(), ms))
    return out


def launch_times(text: str, symbols: Sequence[str], release_ms: int, *, max_lead_ms: int = 14 * DAY_MS) -> dict[str, int | None]:
    """Launch time L (epoch ms) of each symbol from the announcement body.

    Rule: L of a symbol is the first "YYYY-MM-DD HH:MM (UTC)" stamp at or after the symbol's first mention that is
    followed by no other listed symbol before the stamp; else the first stamp after the symbol's first mention; else
    the first stamp in the text. Stamps earlier than ``release_ms - 1 h`` or later than ``release_ms + max_lead_ms``
    are ignored (they are other dates in the text, e.g. pre-market conversions or trading campaigns).

    Priority: the list format "<stamp>: AUSDT Perpetual Contract" (stamp right before the symbol); then the
    specification table "Perpetual Contracts AUSDT BUSDT Launch Time <stamp A> <stamp B>" (column by column); then
    the prose rule above."""
    text = text.replace("&nbsp;", " ").replace(" ", " ")
    text = " ".join(text.split())

    def in_window(x: int) -> bool:
        return release_ms - HOUR_MS <= x <= release_ms + max_lead_ms

    res: dict[str, int | None] = {}
    for s in symbols:  # list format (2024-12+): "2024-12-20 18:30 (UTC): AIXBTUSDT Perpetual Contract"
        m = re.search(rf"(\d{{4}}-\d{{2}}-\d{{2}}\s+\d{{1,2}}:\d{{2}}\s*\(UTC\))\s*:\s*{re.escape(s)}\b", text)
        if m:
            st = _times(m.group(1))
            if st and in_window(st[0][1]):
                res[s] = st[0][1]
    for m in _TABLE.finditer(text):
        cols = m.group(1).split()
        stamps = [ms for _, ms in _times(m.group(2))]
        if len(cols) == len(stamps):
            for c, x in zip(cols, stamps, strict=True):
                if c in symbols and c not in res and in_window(x):
                    res[c] = x
    if all(s in res for s in symbols):
        return {s: res[s] for s in symbols}
    ok = [(p, ms) for p, ms in _times(text) if in_window(ms)]
    pos = {s: [m.start() for m in re.finditer(rf"\b{re.escape(s)}\b", text)] for s in symbols}
    for s in symbols:
        if s in res:
            continue
        if not ok:
            res[s] = None
            continue
        if not pos[s]:
            res[s] = ok[0][1]
            continue
        chosen = None
        for p0 in pos[s]:
            for p, ms in ok:
                if p < p0:
                    continue
                others = [q for o in symbols if o != s for q in pos[o] if p0 < q < p]
                if not others:
                    chosen = ms
                break
            if chosen is not None:
                break
        if chosen is None:
            after = [ms for p, ms in ok if p >= pos[s][0]]
            chosen = after[0] if after else ok[0][1]
        res[s] = chosen
    return res


def venue_symbols(token: str) -> dict[str, list[str]]:
    """Candidate symbols of one token on Bybit linear (``XUSDT``, ``1000XUSDT``, ...) and Hyperliquid (``X``,
    ``kX``)."""
    return {
        "bybit": [f"{m if m > 1 else ''}{token}USDT" for m in MULTIPLIERS],
        "hyperliquid": [token, f"k{token}"],
    }


def eligible(first_ms: int | None, ann_ms: int, *, min_age_ms: int = 14 * DAY_MS) -> bool:
    """The other venue's perp had its first bar at least ``min_age_ms`` before the announcement."""
    return first_ms is not None and ann_ms - first_ms >= min_age_ms


def live_at(last_ms: int | None, n14: int, ann_ms: int, *, max_gap_ms: int = 2 * DAY_MS, min_days: int = 12) -> bool:
    """The other venue's contract traded at the announcement: its last daily bar opened at most ``max_gap_ms``
    before the announcement and at least ``min_days`` of the 14 days before it had volume (sanity amendment of
    2026-10-29: H1 is undefined on a contract that is not trading)."""
    return last_ms is not None and ann_ms - last_ms <= max_gap_ms and n14 >= min_days


def collision_ratio(
    bybit_1h: Sequence[Sequence[float]],
    binance_1h: Sequence[Sequence[float]],
    launch_ms: int,
    *,
    bybit_mult: int = 1,
    binance_mult: int = 1,
) -> float | None:
    """Per-token price level of the first Binance USD-M trade (open of the first 1h bar opening at or after the hour
    of ``launch_ms``) over the close of the last Bybit 1h bar that closed at or before that hour, i.e. both venues at
    (about) the same moment, just before / at L. A ratio far from 1 means a different asset under the same ticker;
    a pump between the announcement and L moves both venues and leaves the ratio near 1. Bars ``[open_ms, o, h, l,
    c, ...]`` sorted by time; reads no Bybit bar that closes after the hour of L. None if either price is missing."""
    h = launch_ms // HOUR_MS * HOUR_MS
    first = next((float(x[1]) for x in binance_1h if int(x[0]) >= h), None)
    pre = _close_at(bybit_1h, h - HOUR_MS)
    if not first or not pre:
        return None
    return (first / binance_mult) / (pre / bybit_mult)


# ------------------------------------------------------------------ placebo outcome (power gate; no event windows)


def _close_at(bars: Sequence[Sequence[float]], ms: int) -> float | None:
    """Close of the 1h bar that opened at ``ms`` (bars ``[open_ms, o, h, l, c, ...]``), else None."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] < ms:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(bars) and int(bars[lo][0]) == ms:
        return float(bars[lo][4])
    return None


def beta(alt: Sequence[Sequence[float]], btc: Sequence[Sequence[float]], t_ms: int, *, days: int = 60) -> float | None:
    """OLS beta of daily alt log returns on BTC over the ``days`` full days before ``t_ms`` (1h bars; the daily close is
    the close of the bar opening at 23:00 UTC). Uses only bars that closed before ``t_ms``. None if < 30 days."""
    d_end = t_ms // DAY_MS * DAY_MS
    xs: list[float] = []
    ys: list[float] = []
    prev_a = prev_b = None
    for k in range(days, -1, -1):
        h = d_end - k * DAY_MS - HOUR_MS
        if h + HOUR_MS > t_ms:
            break
        a, b = _close_at(alt, h), _close_at(btc, h)
        if a and b and prev_a and prev_b:
            ys.append(math.log(a / prev_a))
            xs.append(math.log(b / prev_b))
        prev_a, prev_b = a, b
    if len(xs) < 30:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / vx


def hedged_short(
    alt: Sequence[Sequence[float]],
    btc: Sequence[Sequence[float]],
    t_ms: int,
    b: float,
    *,
    hold_ms: int = 24 * HOUR_MS,
    cost: float = 0.0025,
) -> float | None:
    """Short the alt from the close of the 1h bar opening at ``t_ms`` for ``hold_ms``, minus beta times the BTC
    simple return over the same window, minus the round-trip ``cost``. Funding not included (placebo / SD only)."""
    a0, a1 = _close_at(alt, t_ms), _close_at(alt, t_ms + hold_ms)
    b0, b1 = _close_at(btc, t_ms), _close_at(btc, t_ms + hold_ms)
    if not (a0 and a1 and b0 and b1):
        return None
    return -(a1 / a0 - 1.0) + b * (b1 / b0 - 1.0) - cost


def placebo_hours(
    key: str, month_start_ms: int, month_end_ms: int, n: int, *, blocked: Iterable[int] = (), block_ms: int = 4 * DAY_MS
) -> list[int]:
    """``n`` reproducible pseudo-random hour starts in [month_start, month_end - 1 day), at least ``block_ms`` away
    from each ``blocked`` time (the token's announcements), so no placebo window holds an event reaction."""
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))
    a, z = month_start_ms // HOUR_MS, (month_end_ms - DAY_MS) // HOUR_MS
    bl = list(blocked)
    out: list[int] = []
    tries = 0
    while len(out) < n and tries < 50 * n and z > a:
        tries += 1
        t = rng.randrange(a, z) * HOUR_MS
        if t in out or any(abs(t - x) < block_ms for x in bl):
            continue
        out.append(t)
    return sorted(out)


def launch_from_title(symbol: str) -> tuple[str, int]:
    """(token, multiplier) of a title symbol."""
    return perp_base(symbol)
