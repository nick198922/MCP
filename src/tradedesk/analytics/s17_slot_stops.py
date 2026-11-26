"""S17 signals stopped only by the demo's daily setup slots or its open-positions cap (accounting, not an edge test).

IDEA-20261030-demo-setups-depth, item «S17, сигнали, зупинені слотом старої лімітки або лімітом позицій». Pure
functions, no I/O; times are unix ms passed in. Steps:

- ``pair_first_cycle``: the candidates of one ``link_id`` (all S17 "siblings" of one breakout candle share it) from
  its ``signal`` audit rows and its ``refused`` / ``skipped`` rows of the same demo cycle, paired in order (both loops
  of ``demo_trader._select_and_enter`` walk the same candidate list);
- ``classify_key``: the key's class from the candidate the demo rule would rank first (score, then R:R);
- ``share_vectors``: the registered shares s, s′ and the placed share over all eligible keys;
- ``prev_day_pending`` / ``released_keys``: the earlier-day ``pending`` limits counted in today's S17 slots and the
  keys a cycle would have admitted without them (one step, no cascade);
- ``first_touch_glued`` / ``touch_status`` / ``touch_share``: whether price reached the level within a horizon on
  1m bars for the first hour after the decision and 1h bars after it, with censoring at the data cut-off (a share
  only over keys whose horizon ended by then);
- ``shadow_trade``: the passport's trade on 1h bars (``setup_passport.limit_trade``).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .course_s14_ema import DAY_MS, Bars
from .pending_slots import Interval
from .sample_pace import reason_category
from .setup_passport import limit_trade
from .stop_management import Fees, StopRule

OWN_SLOTS = 2  # demo_trader.OWN_SLOTS (read-only copy); S17 is in NO_RESERVE, so 2 is its whole daily allowance
MIN_SCORE = 2  # demo_trader.MIN_SCORE for S17
CYCLE_GAP_MS = 30 * 60_000  # signal rows of one link_id more than this apart belong to different demo cycles
SLOT_CATS = frozenset({"setup_slots", "positions_max"})
STRUCT_CATS = frozenset({"stop_wide", "rr_low", "levels", "at_level", "score_low"})
STOPPED = ("slots_only", "limit_only", "slots_and_limit")  # (a): the numerator of s
STOPPED_WIDE = (*STOPPED, "slots_limit_plus_other")  # the numerator of the sensitivity s′
# every eligible class: the denominator of s, s′ and the placed share (slots_unread: a slot text without N, i.e. the
# demo did not read its book; eligible but in neither numerator)
ELIGIBLE = ("placed", *STOPPED, "slots_limit_plus_other", "slots_unread", "other")
RULE_A = StopRule("A")  # no break-even
RULE_B = StopRule("B", be_frac=0.5)  # the passport's rule: break-even after half the way to the target
_SLOTS_RE = re.compile(r"^(\d+) \S+ trades at risk today: its \d+ slots")

__all__ = [
    "OWN_SLOTS",
    "RULE_A",
    "RULE_B",
    "STOPPED",
    "STOPPED_WIDE",
    "ELIGIBLE",
    "Candidate",
    "KeyClass",
    "pair_first_cycle",
    "classify_key",
    "share_vectors",
    "slots_used",
    "prev_day_pending",
    "released_keys",
    "first_touch",
    "first_touch_glued",
    "touch_status",
    "touch_share",
    "shadow_trade",
    "km_touch",
]


@dataclass(frozen=True)
class Candidate:
    """One evaluated S17 signal (a sibling of its key): decision time, demo score, R:R after fees, levels, violations."""

    link_id: str
    ts: int
    score: int
    rr: float
    side: str
    entry: float
    stop: float
    target: float
    rest_until: int
    violations: tuple[str, ...]


@dataclass(frozen=True)
class KeyClass:
    """``cls``: placed | ineligible | slots_only | limit_only | slots_and_limit | slots_limit_plus_other | other |
    unmatched. ``best``: the eligible candidate ranked first by the demo rule (None if none)."""

    link_id: str
    cls: str
    best: Candidate | None
    cats: frozenset[str]


def _cycles(rows: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    out: list[list[Mapping[str, Any]]] = []
    for r in sorted(rows, key=lambda r: (int(r["ts"]), int(r.get("id") or 0))):
        if out and int(r["ts"]) - int(out[-1][-1]["ts"]) <= CYCLE_GAP_MS:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def _candidate(sig: Mapping[str, Any], dec: Mapping[str, Any]) -> Candidate | None:
    p = sig.get("payload") or {}
    s, prop = p.get("signal") or {}, p.get("proposal") or {}
    d = dec.get("payload") or {}

    def lvl(k: str) -> float:  # the tick-rounded level the demo would send (proposal), else the raw signal level
        return float(prop[k] if prop.get(k) is not None else s[k])

    try:
        rest = int((s.get("indicators") or {}).get("rest_until_ms") or 0)
        return Candidate(
            link_id=str(sig["link_id"]),
            ts=int(dec["ts"]),
            score=int(d["score"] if d.get("score") is not None else p["score"]),
            rr=float(prop.get("rr") or 0.0),
            side=str(s["side"]),
            entry=lvl("entry"),
            stop=lvl("stop"),
            target=lvl("target"),
            rest_until=rest,
            violations=tuple(str(v) for v in d.get("violations") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None


def pair_first_cycle(signals: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]]) -> list[Candidate] | None:
    """Candidates of one ``link_id`` in its first demo cycle that has decision rows.

    ``signals`` / ``decisions``: audit rows (``id``, ``ts`` ms, ``link_id``, ``payload`` dict) of kind ``signal`` and of
    kinds ``refused`` / ``skipped``. A cycle = signal rows at most ``CYCLE_GAP_MS`` apart; its decisions are those from
    its first signal up to the next cycle. The i-th decision pairs with the i-th signal. None: no cycle has decisions,
    or the first one with decisions has a different number of them (or an unreadable row) — the key is unmatched."""
    cyc = _cycles(signals)
    decs = sorted(decisions, key=lambda r: (int(r["ts"]), int(r.get("id") or 0)))
    for i, c in enumerate(cyc):
        t0 = int(c[0]["ts"])
        t1 = int(cyc[i + 1][0]["ts"]) if i + 1 < len(cyc) else None
        mine = [d for d in decs if int(d["ts"]) >= t0 and (t1 is None or int(d["ts"]) < t1)]
        if not mine:
            continue
        if len(mine) != len(c):
            return None
        out = [_candidate(s, d) for s, d in zip(c, mine, strict=True)]
        return None if any(x is None for x in out) else [x for x in out if x is not None]
    return None


def _cats(c: Candidate) -> frozenset[str]:
    return frozenset(reason_category(v) for v in c.violations) - {"decided"}


def classify_key(link_id: str, cands: Sequence[Candidate] | None, placed: bool) -> KeyClass:
    """Class of one key. ``placed`` (an entry order was sent for the link_id) wins; then ``unmatched`` (``cands`` None).
    Eligible candidates: score >= ``MIN_SCORE`` and no structural violation; the demo's rank (score desc, R:R desc,
    stable) picks ``best``, whose non-structural violations decide: a slot text without N («… unknown (book not
    read)») -> slots_unread (eligible, never counted as stopped by slots); only ``setup_slots`` -> slots_only, only
    ``positions_max`` -> limit_only, both -> slots_and_limit, one of them with any other account cap ->
    slots_limit_plus_other, neither -> other. No eligible candidate: at_level if a sibling with enough score had no
    structural flaw but «price already at the resting level» (a limit of the passport, lost to the demo's post-only
    execution; still not eligible), else ineligible."""
    if placed:
        return KeyClass(link_id, "placed", None, frozenset())
    if cands is None:
        return KeyClass(link_id, "unmatched", None, frozenset())
    ok = [c for c in cands if c.score >= MIN_SCORE and not (_cats(c) & STRUCT_CATS)]
    if not ok:
        lvl = any(c.score >= MIN_SCORE and _cats(c) & STRUCT_CATS == {"at_level"} for c in cands)
        return KeyClass(link_id, "at_level" if lvl else "ineligible", None, frozenset())
    best = sorted(ok, key=lambda c: (-c.score, -c.rr))[0]
    cats = _cats(best)
    hit = cats & SLOT_CATS
    if "setup_slots" in cats and slots_used(best.violations) is None:
        cls = "slots_unread"
    elif not hit:
        cls = "other"
    elif cats - SLOT_CATS:
        cls = "slots_limit_plus_other"
    elif hit == {"setup_slots"}:
        cls = "slots_only"
    elif hit == {"positions_max"}:
        cls = "limit_only"
    else:
        cls = "slots_and_limit"
    return KeyClass(link_id, cls, best, cats)


def share_vectors(keys: Sequence[tuple[str, int]]) -> dict[str, list[float] | list[int]]:
    """Registered shares over every eligible key (``ELIGIBLE``: placed, the three stopped classes,
    slots_limit_plus_other, slots_unread, other). ``keys``: (class, cluster id, e.g. the decision day). Returns the
    0/1 vectors ``s`` ((a) = ``STOPPED``), ``s_prime`` (``STOPPED_WIDE``), ``placed`` and the ``days`` of the same
    eligible keys, for a cluster bootstrap; ineligible, at_level and unmatched keys are left out."""
    el = [(c, d) for c, d in keys if c in ELIGIBLE]
    return {
        "s": [1.0 if c in STOPPED else 0.0 for c, _ in el],
        "s_prime": [1.0 if c in STOPPED_WIDE else 0.0 for c, _ in el],
        "placed": [1.0 if c == "placed" else 0.0 for c, _ in el],
        "days": [d for _, d in el],
    }


def slots_used(violations: Sequence[str]) -> int | None:
    """N of «N S17 trades at risk today: its 2 slots are taken …» (``demo_trader._setup_cap``), None if absent."""
    for v in violations:
        m = _SLOTS_RE.match(str(v))
        if m:
            return int(m.group(1))
    return None


def prev_day_pending(intervals: Sequence[Interval], t: int) -> int:
    """Limits resting (unfilled) at ``t`` that were placed before the UTC day of ``t``: the ones ``demo_trader._today``
    counts in today's slots only because their status is ``pending``. ``intervals``: [placement, fill or end)."""
    day0 = t // DAY_MS * DAY_MS
    return sum(1 for iv in intervals if iv.start < day0 and iv.start <= t < iv.end)


def released_keys(keys: Sequence[KeyClass], intervals: Sequence[Interval]) -> set[str]:
    """Keys a cycle would have admitted if earlier-day ``pending`` limits held no daily slot (one step, no cascade).

    Per decision day: N = the slots in use from the guard text (max over the day's keys), P = ``prev_day_pending`` at
    the first decision; free = max(0, ``OWN_SLOTS`` - (N - P)) slots go to the day's ``slots_only`` keys in demo rank
    order (score desc, R:R desc). Keys also stopped by the position cap or another cap are never admitted.

    Not a bound in either direction: the cascade (an admitted limit holding later days' slots and a place under the
    position cap) would lower the count, while the guard texts (and N) describe the state at the end of the cycle, so
    a key whose cap became binding only after its turn reads as slots plus a cap and is never admitted (raises it)."""
    by_day: dict[int, list[KeyClass]] = defaultdict(list)
    for k in keys:
        if k.best is not None and "setup_slots" in k.cats:
            by_day[k.best.ts // DAY_MS].append(k)
    out: set[str] = set()
    for day in sorted(by_day):
        ks = by_day[day]
        ns = [n for k in ks if k.best is not None and (n := slots_used(k.best.violations)) is not None]
        if not ns:
            continue
        t0 = min(k.best.ts for k in ks if k.best is not None)
        free = max(0, OWN_SLOTS - (max(ns) - prev_day_pending(intervals, t0)))
        ranked = sorted((k for k in ks if k.cls == "slots_only" and k.best is not None), key=lambda k: (-k.best.score, -k.best.rr))  # type: ignore[union-attr]
        out.update(k.link_id for k in ranked[:free])
    return out


def first_touch(path: Bars, side: str, level: float, t0: int, t1: int, through: bool = False) -> int | None:
    """Open time of the first bar (any length) opening in [t0, t1) whose low reaches ``level`` (long; the high for a short);
    ``through``: strictly beyond it. None if none. Bars outside [t0, t1) are never read."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    if side == "long":
        x = path.low[i0:i1]
        hit = np.flatnonzero(x < level if through else x <= level)
    else:
        x = path.h[i0:i1]
        hit = np.flatnonzero(x > level if through else x >= level)
    return int(path.t[i0 + int(hit[0])]) if hit.size else None


def first_touch_glued(
    fine: Bars, coarse: Bars, side: str, level: float, t0: int, t_switch: int, t1: int, through: bool = False
) -> int | None:
    """First touch on the glued series: ``fine`` bars (e.g. 1m) opening in [t0, t_switch), then ``coarse`` bars (1h)
    opening in [t_switch, t1). ``t_switch`` must be a coarse bar boundary at or after t0 (the first full 1h bar after
    the decision), so a coarse bar never overlaps the decision. Bars outside the two windows are never read."""
    a = first_touch(fine, side, level, t0, min(t_switch, t1), through)
    if a is not None or t_switch >= t1:
        return a
    return first_touch(coarse, side, level, t_switch, t1, through)


def touch_status(touch_ms: int | None, t0: int, horizon_ms: int, until_ms: int) -> str:
    """For a horizon that ended by ``until_ms`` (t0 + horizon <= until_ms): touched (a touching bar opened before
    t0 + horizon) | not. For a horizon still open at the data cut-off: early_touch (already touched; descriptive
    only, never in a share: counting it would bias the share up) | censored (no touch yet)."""
    done = t0 + horizon_ms <= until_ms
    hit = touch_ms is not None and touch_ms < t0 + horizon_ms
    if done:
        return "touched" if hit else "not"
    return "early_touch" if hit else "censored"


def touch_share(statuses: Sequence[str]) -> tuple[int, int]:
    """(touched, n) over the keys whose horizon ended by the cut-off (``touch_status`` touched | not); early_touch
    and censored keys are not in n."""
    k = sum(1 for s in statuses if s == "touched")
    return k, k + sum(1 for s in statuses if s == "not")


def km_touch(times: Sequence[float | None], follow: Sequence[float], at: Sequence[float]) -> list[float]:
    """Kaplan-Meier share touched by each age in ``at`` (days): ``times`` the age at touch (None = no touch), ``follow``
    the age at the data cut-off (censoring). Ages beyond the longest follow-up return the last estimate."""
    ev = sorted((float(t), 1) if t is not None else (float(f), 0) for t, f in zip(times, follow, strict=True))
    surv, out, j, n = 1.0, [], 0, len(ev)
    for a in at:
        while j < n and ev[j][0] <= a:
            tj = ev[j][0]
            d = sum(1 for e in ev[j:] if e[0] == tj and e[1] == 1)
            m = sum(1 for e in ev[j:] if e[0] == tj)
            if d:
                surv *= 1 - d / (n - j)
            j += m
        out.append(1 - surv)
    return out


def shadow_trade(
    path: Bars,
    side: str,
    entry: float,
    stop: float,
    target: float,
    t0: int,
    rest_until: int,
    until_ms: int,
    hold_ms: int,
    rule: StopRule = RULE_B,
    fees: Fees = Fees(),
    through: bool = True,
) -> dict[str, Any]:
    """The passport's limit trade of one key on 1h bars cut at ``until_ms`` (only bars closed by then may be passed).

    status: not_filled (no fill before ``rest_until`` and that deadline passed) | pending (no fill yet, deadline not
    passed) | open (filled, exit not reached by the cut-off: ``r_mtm`` = R at the last close, net of the entry fee and a
    taker exit) | resolved (``r`` = net R) | no_path. Shorts run on mirrored prices."""
    b = path if side == "long" else path.mirrored()
    e, s, g = (entry, stop, target) if side == "long" else (-entry, -stop, -target)
    o, tk = limit_trade(b, t0, min(rest_until, until_ms), e, s, g, hold_ms, rule, fees, through)
    if tk < 0:
        return {"status": "not_filled" if rest_until <= until_ms else "pending", "r": None, "r_mtm": None, "fill_ms": None}
    if o is None:
        return {"status": "no_path", "r": None, "r_mtm": None, "fill_ms": tk}
    if o.exit_kind == "data_end":
        return {"status": "open", "r": None, "r_mtm": o.r_net, "fill_ms": tk, "exit_kind": o.exit_kind}
    return {"status": "resolved", "r": o.r_net, "r_mtm": o.r_net, "fill_ms": tk, "exit_kind": o.exit_kind}
