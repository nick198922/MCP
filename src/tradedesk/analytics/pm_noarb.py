"""Static no-arbitrage bounds between Polymarket BTC/ETH price markets of one snapshot (pure, no I/O).

Card: docs/ideas/IDEA-20261024-polymarket-static-noarb-ladders.md, section «Попередня реєстрація». Every pair below is a
two-leg package of equal share counts that pays at least 1 per share in every scenario, so buying both legs at the ask for
less than 1 minus fees is a model-free gain. Families (K1 < K2, one asset, rules_standard = 1 on both legs):

    A        above ladder     YES(above K1) + NO(above K2), the same end_ts (12:00 ET of one date) and rules text
    U        touch-up ladder  YES(up K1) + NO(up K2), the same window (window_start, end_ts) and rules text
    D        touch-down       YES(down K2) + NO(down K1), the same window and rules text
    TA       up >= above      YES(up K) + NO(above K): the touch window holds the above settlement candle
    TB       down >= below    YES(down K) + YES(above K): the touch window holds the above settlement candle
    TA_near  secondary        YES(up Kt) + NO(above Ka), Ka = the smallest above strike > Kt (no equal strike needed)
    TB_near  secondary        YES(down Kt) + YES(above Ka), Ka = the largest above strike < Kt

Why TB pairs YES with YES: a down-touch ("dip to K", 1m Low <= K) contains "close <= K", the NO side of "above K", so the
inclusion is with "below", not with "above"; YES(down K) + NO(above K) has no bound.

Prices are decimals per share paying 1; ``*_ask_usd`` is price x size at the best level only (``pm_gap.top_of_book``);
the taker fee per share is ``pm_touch.fee_per_share`` = rate * (p (1 - p)) ** exponent, the formula checked against
Polymarket's fee docs on 2026-10-16 (``negrisk.py``); ``FEE_LOWER`` (p times that) is a descriptive sensitivity only.
Time (``ts``) is a parameter; nothing here reads the model columns or any settled outcome.

H1 counting rules (registered before any count; card p. 10-11): a violation counts toward H1 only if neither leg's own
book is crossed (its YES ask + NO ask < 1) AND (the legs were read at most ``SYNC_S`` apart OR the same pair, with no
crossed leg, is again a violation in the check snapshot = the first snapshot >= ``SURVIVE_MIN_GAP_S`` after it, when
that snapshot is at most ``SURVIVE_MAX_GAP_S`` after it). Classes: crossed, sync, survived, skew_suspect.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from tradedesk.analytics.pm_touch import et_date, fee_per_share

PRIMARY = ("A", "U", "D", "TA", "TB")  # headline H1 families: the decision is made on these
LITERAL = ("A", "TA")  # the card's literal pairs (mechanism p. 1 and 2): a secondary H1 line, descriptive
SECONDARY = ("TA_near", "TB_near")
FAMILIES = PRIMARY + SECONDARY
MARGIN = 0.005  # card: ask(A) + ask(B) <= 1 - fee - 0.005
MIN_DEPTH_USD = 50.0  # per leg, best-level USD (price x size)
MAX_BOOK_AGE_S = 120  # |ts - book_ts| of each leg
MAX_LEG_SKEW_S = 60  # |book_ts(A) - book_ts(B)| for status ok
SYNC_S = 5  # H1 rule (b): legs read at most this far apart count without persistence
SURVIVE_MIN_GAP_S = 1800  # H1 rule (b): the check snapshot is the first one at least 30 min later ...
SURVIVE_MAX_GAP_S = 5400  # ... and it must be at most 90 min later, else there is no check snapshot
CANDLE_S = 60  # the above market settles on a 1m candle that opens or closes at end_ts
STRIKE_EPS = 1e-6
STATUSES = ("no_book", "stale", "skew", "shallow", "ok")
FEE_MAIN = "main"
FEE_LOWER = "lower"
H1_CLASSES = ("crossed", "sync", "survived", "skew_suspect")
COUNTED = ("sync", "survived")


def settle_date(row: Mapping[str, Any]) -> date:
    """ET date the market settles on: 'above' the date of its 12:00 ET end, 'touch' the last day of its window."""
    end = int(row["end_ts"])
    return et_date(end) if row["kind"] == "above" else et_date(end - 1)


def covers(window_start: int | None, touch_end: int, above_end: int, candle_s: int = CANDLE_S) -> bool:
    """True when the touch window [window_start, touch_end) holds the whole above settlement candle, whichever of the two
    1m candles around ``above_end`` the rule names (the "12:00" one or the one "closing at 12:00")."""
    return window_start is not None and int(window_start) <= above_end - candle_s and above_end + candle_s <= int(touch_end)


def _std(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("rules_standard") == 1]


def _pair(family: str, a: Mapping[str, Any], sa: str, b: Mapping[str, Any], sb: str, adjacent: bool | None, unit: date) -> dict[str, Any]:
    return {
        "id": (family, str(a["market_id"]), sa, str(b["market_id"]), sb),
        "family": family,
        "asset": a["asset"],
        "a": (str(a["market_id"]), sa),
        "b": (str(b["market_id"]), sb),
        "adjacent": adjacent,
        "unit": unit.isoformat(),
    }


def match_pairs(rows: Iterable[Mapping[str, Any]], standard_only: bool = True) -> list[dict[str, Any]]:
    """All registered pairs among the rows of ONE snapshot (matching rules only: no book, no price).

    Ladders (A, U, D) are formed inside a group of equal (asset, kind, direction, end_ts, window_start, rules_hash), every
    K1 < K2 (``adjacent`` marks consecutive strikes of the group). Cross pairs (TA, TB and their ``_near`` variants) need
    the same asset and ``covers``; ``adjacent`` is None for them. Rows without ``rules_standard = 1`` are left out before
    grouping, so adjacency is among standard markets only; ``standard_only=False`` keeps them (step 0 counts those
    pairs only to show the share of standard ones, card step 0).
    """
    std = _std(rows) if standard_only else list(rows)
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for r in std:
        groups[(r["asset"], r["kind"], r["direction"], int(r["end_ts"]), r.get("window_start"), r.get("rules_hash"))].append(r)
    out: list[dict[str, Any]] = []
    for (_, kind, direction, _, _, _), g in groups.items():
        g = sorted(g, key=lambda r: float(r["strike"]))
        unit = settle_date(g[0])
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                lo, hi = g[i], g[j]
                if float(hi["strike"]) - float(lo["strike"]) <= STRIKE_EPS:
                    continue
                adj = j == i + 1
                if kind == "above":
                    out.append(_pair("A", lo, "yes", hi, "no", adj, unit))
                elif direction == "up":
                    out.append(_pair("U", lo, "yes", hi, "no", adj, unit))
                elif direction == "down":
                    out.append(_pair("D", hi, "yes", lo, "no", adj, unit))
    above: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for r in std:
        if r["kind"] == "above":
            above[(r["asset"], int(r["end_ts"]))].append(r)
    for t in std:
        if t["kind"] != "touch" or t["direction"] not in ("up", "down"):
            continue
        kt = float(t["strike"])
        for (asset, end), xs in above.items():
            if asset != t["asset"] or not covers(t.get("window_start"), int(t["end_ts"]), end):
                continue
            unit = settle_date(xs[0])
            eq = [x for x in xs if abs(float(x["strike"]) - kt) <= STRIKE_EPS]
            if t["direction"] == "up":
                for x in eq:
                    out.append(_pair("TA", t, "yes", x, "no", None, unit))
                hi = [x for x in xs if float(x["strike"]) > kt + STRIKE_EPS]
                if hi:
                    out.append(_pair("TA_near", t, "yes", min(hi, key=lambda x: float(x["strike"])), "no", None, unit))
            else:
                for x in eq:
                    out.append(_pair("TB", t, "yes", x, "yes", None, unit))
                lo = [x for x in xs if float(x["strike"]) < kt - STRIKE_EPS]
                if lo:
                    out.append(_pair("TB_near", t, "yes", max(lo, key=lambda x: float(x["strike"])), "yes", None, unit))
    return out


def leg_book(row: Mapping[str, Any], side: str, ts: int, max_age_s: int = MAX_BOOK_AGE_S) -> str:
    """'no_book' (ask missing / outside (0, 1) / no USD size), 'stale' (book_ts missing or more than ``max_age_s`` from
    ``ts``) or 'ok' for buying ``side`` ('yes' | 'no') of one market."""
    ask, usd = row.get(f"{side}_ask"), row.get(f"{side}_ask_usd")
    if ask is None or usd is None or not (math.isfinite(ask) and math.isfinite(usd)) or not 0.0 < ask < 1.0 or usd <= 0:
        return "no_book"
    bt = row.get("book_ts")
    if bt is None or abs(int(ts) - int(bt)) > max_age_s:
        return "stale"
    return "ok"


def pair_status(pair: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]], ts: int, min_depth_usd: float = MIN_DEPTH_USD) -> str:
    """First failed check of a matched pair at ``ts``: no_book, stale, skew (legs read more than ``MAX_LEG_SKEW_S``
    apart), shallow (a leg's best-level USD below ``min_depth_usd``) or ok. Uses no price beyond validity."""
    ra, rb = by_id[pair["a"][0]], by_id[pair["b"][0]]
    sa, sb = leg_book(ra, pair["a"][1], ts), leg_book(rb, pair["b"][1], ts)
    for s in ("no_book", "stale"):
        if s in (sa, sb):
            return s
    if abs(int(ra["book_ts"]) - int(rb["book_ts"])) > MAX_LEG_SKEW_S:
        return "skew"
    if min(float(ra[f"{pair['a'][1]}_ask_usd"]), float(rb[f"{pair['b'][1]}_ask_usd"])) < min_depth_usd:
        return "shallow"
    return "ok"


def snapshot_pairs(rows: Iterable[Mapping[str, Any]], ts: int, min_depth_usd: float = MIN_DEPTH_USD) -> list[dict[str, Any]]:
    """``match_pairs`` of one snapshot with each pair's ``status`` (step 0 counts these; no cost is computed)."""
    rows = list(rows)
    by_id = {str(r["market_id"]): r for r in rows}
    return [{**p, "status": pair_status(p, by_id, ts, min_depth_usd)} for p in match_pairs(rows)]


def leg_cost(row: Mapping[str, Any], side: str, fee_model: str = FEE_MAIN) -> float:
    """Ask plus the taker fee per share of buying ``side`` of one market (decimal per share paying 1).

    ``fee_model`` 'main' = rate * (p (1 - p)) ** exp (the checked formula, decides H1); 'lower' = p times that
    (descriptive sensitivity: the count of violation dates if the fee were the lower bound)."""
    if fee_model not in (FEE_MAIN, FEE_LOWER):
        raise ValueError(f"unknown fee model {fee_model!r}")
    ask = float(row[f"{side}_ask"])
    fee = fee_per_share(ask, float(row.get("fee_rate") or 0.0), float(row.get("fee_exp") or 1.0))
    return ask + (fee * ask if fee_model == FEE_LOWER else fee)


def pair_cost(pair: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]], fee_model: str = FEE_MAIN) -> float:
    """Cost per share of the package: ask(A) + fee(A) + ask(B) + fee(B)."""
    return leg_cost(by_id[pair["a"][0]], pair["a"][1], fee_model) + leg_cost(by_id[pair["b"][0]], pair["b"][1], fee_model)


def crossed(row: Mapping[str, Any]) -> bool:
    """True when a market's own book is crossed: both asks present and YES ask + NO ask < 1 (a suspect book; H1 rule a)."""
    ya, na = row.get("yes_ask"), row.get("no_ask")
    if ya is None or na is None or not (math.isfinite(ya) and math.isfinite(na)):
        return False
    return float(ya) + float(na) < 1.0


def check_ts(ts: int, all_ts: Sequence[int]) -> int | None:
    """The check snapshot of a violation at ``ts`` (H1 rule b): the first ts of the sorted sample >= ts +
    ``SURVIVE_MIN_GAP_S``, if it is <= ts + ``SURVIVE_MAX_GAP_S``; otherwise None (no persistence check possible)."""
    i = bisect.bisect_left(all_ts, int(ts) + SURVIVE_MIN_GAP_S)
    if i == len(all_ts) or all_ts[i] > int(ts) + SURVIVE_MAX_GAP_S:
        return None
    return int(all_ts[i])


def classify_violations(viols: Iterable[Mapping[str, Any]], all_ts: Sequence[int]) -> list[dict[str, Any]]:
    """Registered H1 classes of violation records (one fee model at a time), in input order.

    Each record needs ``id`` (pair id), ``ts``, ``book_ts`` (leg A, leg B) and ``crossed`` (either leg's book crossed).
    Class, first rule that applies: 'crossed' (rule a, never counted); 'sync' (|book_ts A - book_ts B| <= ``SYNC_S``);
    'survived' (the same pair is a non-crossed violation at ``check_ts``); else 'skew_suspect'. Only ``COUNTED``
    classes enter the H1 decision. Adds ``h1_class`` and ``check_ts`` to a copy of each record.
    """
    viols = list(viols)
    clean = {(v["id"], int(v["ts"])) for v in viols if not v["crossed"]}
    out = []
    for v in viols:
        ct = check_ts(int(v["ts"]), all_ts)
        bta, btb = v["book_ts"]
        if v["crossed"]:
            cls = "crossed"
        elif abs(int(bta) - int(btb)) <= SYNC_S:
            cls = "sync"
        elif ct is not None and (v["id"], ct) in clean:
            cls = "survived"
        else:
            cls = "skew_suspect"
        out.append({**v, "h1_class": cls, "check_ts": ct})
    return out


def violation_dates(viols: Iterable[Mapping[str, Any]], families: Sequence[str] = PRIMARY, classes: Sequence[str] = COUNTED) -> list[str]:
    """Distinct settlement ET dates (pooled over assets and the given families) of classified violations in ``classes``."""
    return sorted({str(v["unit"]) for v in viols if v["family"] in families and v["h1_class"] in classes})


def is_violation(cost: float, margin: float = MARGIN) -> bool:
    """The card's trigger: ask(A) + ask(B) <= 1 - fee - margin, i.e. cost per share (fees included) <= 1 - margin."""
    return cost <= 1.0 - margin + 1e-12


def package(pair: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """Equal-share package at the best levels: shares n = min(usd / ask) of the two legs, notional n * (ask A + ask B)
    (USD), and the payout floor n * (1 - cost) guaranteed at settlement (USD, fees in, capital locked to the end)."""
    (ma, sa), (mb, sb) = pair["a"], pair["b"]
    ra, rb = by_id[ma], by_id[mb]
    aa, ab = float(ra[f"{sa}_ask"]), float(rb[f"{sb}_ask"])
    n = min(float(ra[f"{sa}_ask_usd"]) / aa, float(rb[f"{sb}_ask_usd"]) / ab)
    return {"shares": n, "notional_usd": n * (aa + ab), "floor_gain_usd": n * (1.0 - pair_cost(pair, by_id))}


class EpisodeTracker:
    """Runs of consecutive snapshots in which one pair is a violation (streaming, memory = open runs only).

    ``update(ts, flags)`` takes one snapshot's {pair id: violation?} for the pairs with status ok; a pair missing from
    ``flags`` was absent or not ok. A run ends as 'held' (next snapshot ok, no violation), 'absent' (next snapshot has the
    pair missing / not ok) or 'cutoff' (it reaches the last snapshot given). Descriptive only: whether a violation
    "survived the next snapshot" is decided by ``classify_violations`` / ``check_ts`` (>= 30 min later), not by the
    run length, since a run can step through a snapshot taken 42 s later.
    """

    def __init__(self) -> None:
        self.open: dict[Any, dict[str, Any]] = {}
        self.done: list[dict[str, Any]] = []
        self.last_ts: int | None = None

    def update(self, ts: int, flags: Mapping[Any, bool]) -> None:
        for pid in list(self.open):
            if not flags.get(pid, False):
                ep = self.open.pop(pid)
                ep["ended_by"] = "held" if pid in flags else "absent"
                ep["next_ts"] = ts
                self.done.append(ep)
        for pid, v in flags.items():
            if not v:
                continue
            if pid in self.open:
                self.open[pid]["last_ts"] = ts
                self.open[pid]["n_snapshots"] += 1
            else:
                self.open[pid] = {"id": pid, "first_ts": ts, "last_ts": ts, "n_snapshots": 1}
        self.last_ts = ts

    def close(self) -> list[dict[str, Any]]:
        for ep in self.open.values():
            ep["ended_by"] = "cutoff"
            ep["next_ts"] = None
            self.done.append(ep)
        self.open = {}
        return sorted(self.done, key=lambda e: (e["first_ts"], str(e["id"])))


def h1_verdict(
    n_violation_dates: int, n_obs_days: int, n_ok_pairs: int, *, min_dates: int = 5, min_days: int = 30, min_pairs: int = 300
) -> tuple[str, str]:
    """Registered step-1 decision: ('confirmed' | 'rejected' | 'inconclusive', reason). Four disjoint branches:

    n >= 5 -> confirmed (any sample); n == 0 and adequate -> rejected, kill; 1 <= n <= 4 and adequate -> rejected,
    H1 not met (not a kill); n <= 4 and not adequate -> inconclusive.

    ``n_violation_dates`` (decision input) = ``len(violation_dates(classified main-fee violations, PRIMARY, COUNTED))``;
    ``n_obs_days`` / ``n_ok_pairs`` = distinct ET days of ``ts`` / snapshot-pairs with status ok (ladders adjacent only,
    TA / TB) that measure whether the sample is big enough for a "no violations" verdict.
    """
    if n_violation_dates < 0:
        raise ValueError("n_violation_dates must be >= 0")
    if n_violation_dates >= min_dates:
        return "confirmed", f"{n_violation_dates} independent dates with a counted violation >= {min_dates}"
    adequate = n_obs_days >= min_days and n_ok_pairs >= min_pairs
    if not adequate:
        return "inconclusive", f"sample below {min_days} days / {min_pairs} ok pairs and {n_violation_dates} dates < {min_dates}"
    if n_violation_dates == 0:
        return "rejected", "kill: zero counted violations over the adequate sample"
    return "rejected", f"H1 not met: {n_violation_dates} dates < {min_dates} (rare violations, not a kill)"
