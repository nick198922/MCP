"""Setup versions of the demo trader (BACKLOG 0.5, owner 23.10, item b): a change of the rules is a new version tag,
the statistics are kept per (setup, version), and no conclusion is drawn before ~30 closed trades of one version.

Pure functions, no I/O, time is a parameter (``now_ms``: epochs that start later and trades closed later are ignored).

A version is a rule epoch from ``EPOCHS``: the rules that decide how a taken futures trade is entered, managed and
exited (signal detector, levels, entry and target orders, break-even, exits). The trade's version is the last epoch
that applies to its setup and starts at or before its open time (``opened_ts``, the fill). ``demo_trade`` has no
version column and the demo trader (``execution/``, under the owner's Claude review) does not tag one, so the version
is derived from the open time; tagging at open time is a follow-up in BACKLOG. A trade without an open time, or opened
before the first epoch, gets no version (``None``) and is counted apart, not guessed.

Epochs (each from the commit that changed the live rules; the demo trader runs from the checkout those commits were
made in, so a change is assumed live from the commit minute, the deploy time is not recorded):

- ``v1`` 2026-10-21 18:39:17 UTC, de3540f: the rules the first trade (21.10 20:02 UTC) ran under: market entry with
  the stop and target attached on the venue, break-even at +30 % ROE at the trade's leverage
  (``docs/changes/2026-10-21-demo-trader.md``; 88de068 / 14ed303 / f7dd99e / de3540f are all earlier the same evening);
- ``v2`` 2026-10-22 06:13:19 UTC, 8ec1f37: maker limit entry and target, the market only when the situation asks
  (``docs/changes/2026-10-22-demo-maker-orders.md``, ADR 0007 p. 10a);
- ``v3`` 2026-10-22 07:12:39 UTC, 8d82847: break-even half the way to the target instead of +30 % ROE
  (``docs/changes/2026-10-22-demo-breakeven-path.md``; the same minute as ``demo_vs_backtest.BE_SWITCH_MS``).

Not versioned, because they change which signals are taken, not how a taken trade runs: eb3af15 (22.10 07:45 UTC, the
daily cap counts trades at risk, ``docs/changes/2026-10-22-demo-risk-cap.md``), f9f7cb5 (23.10 16:14 UTC, the option
guard; "futures messages unchanged") and cab422e (23.10 16:49 UTC, the option leg shares the signal's slot and the
risk caps; ``docs/changes/2026-10-23-demo-option-leg.md``). The detectors (``analytics/setup_signals.py``) last
changed in 14ed303, before the first trade. A new rule change = a new row here (a test pins the list).

Caveats: the live monitor applies new code to the trades still open at a switch (``changed_inside`` counts the closed
trades of a version that were open when a later epoch of their setup began), so their result mixes two rule sets.
R is the demo's own (venue P&L with fees / risk at the stop with both taker fees); a win is R > 0, so a break-even exit
with fees covered counts as one. Below ``min_n`` closed trades with R a version is 'замало для висновку'.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .demo_vs_backtest import BE_SWITCH_MS, MIN_TRADES


def _ms(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()) * 1000


@dataclass(frozen=True)
class Epoch:
    tag: str  # version tag, e.g. 'v3'
    from_ms: int  # unix ms (UTC) the rules apply from
    what: str  # what changed, Ukrainian (shown to the owner)
    source: str  # commit and change note
    setups: tuple[str, ...] = ()  # the setups it applies to; () = every setup


EPOCHS: tuple[Epoch, ...] = (
    Epoch(
        "v1",
        _ms(2026, 10, 21, 18, 39, 17),
        "запуск: ринковий вхід зі стопом і ціллю на біржі, беззбиток за +30 % ROE на плечі угоди",
        "de3540f, docs/changes/2026-10-21-demo-trader.md",
    ),
    Epoch(
        "v2",
        _ms(2026, 10, 22, 6, 13, 19),
        "мейкерські лімітні вхід і ціль, ринок лише за потреби (беззбиток ще за +30 % ROE)",
        "8ec1f37, docs/changes/2026-10-22-demo-maker-orders.md",
    ),
    Epoch(
        "v3",
        BE_SWITCH_MS,
        "беззбиток на половині шляху до цілі замість +30 % ROE",
        "8d82847, docs/changes/2026-10-22-demo-breakeven-path.md",
    ),
)


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _setup(x: object) -> str | None:
    s = "" if x is None else str(x).strip()
    return s or None


def applies(e: Epoch, setup: str | None) -> bool:
    return not e.setups or setup in e.setups


def version_of(setup: str | None, open_ms: float | None, *, now_ms: int, epochs: Iterable[Epoch] = EPOCHS) -> Epoch | None:
    """The epoch a trade of ``setup`` opened at ``open_ms`` ran under: the latest one that applies to the setup and
    starts at or before both ``open_ms`` and ``now_ms`` (a boundary trade opened at the exact epoch minute is in the new
    version). None without an open time or before the first epoch."""
    if open_ms is None:
        return None
    best: Epoch | None = None
    for e in epochs:
        if applies(e, setup) and e.from_ms <= open_ms and e.from_ms <= now_ms and (best is None or e.from_ms >= best.from_ms):
            best = e
    return best


def _verdict(n_r: int, min_n: int) -> str:
    if n_r < min_n:
        return f"замало для висновку ({n_r} з {min_n})"
    return "достатньо угод для висновку"


def version_stats(
    trades: Iterable[Mapping[str, Any]], *, now_ms: int, epochs: Iterable[Epoch] = EPOCHS, min_n: int = MIN_TRADES
) -> dict[str, Any]:
    """Per (setup, version) statistics of the closed demo trades (``demo_trade`` rows: setup, status, opened_ts,
    closed_ts, r_multiple, pnl_usd). Rows: setup (None = no setup recorded), version (None = no version), from_ms,
    closed, n_r (closed with R), wins (R > 0), win_share, mean_r, pnl_usd (sum, USD), changed_inside, enough
    (n_r >= min_n) and verdict ('замало для висновку (n з 30)' below it). Sorted by setup (None last), then by the
    version's start (None last). ``epochs``: the epochs started by ``now_ms``."""
    eps = sorted((e for e in epochs if e.from_ms <= now_ms), key=lambda e: e.from_ms)
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for t in trades:
        if t.get("status") != "closed":
            continue
        closed_ms = _num(t.get("closed_ts"))
        if closed_ms is not None and closed_ms > now_ms:
            continue
        setup = _setup(t.get("setup"))
        e = version_of(setup, _num(t.get("opened_ts")), now_ms=now_ms, epochs=eps)
        tag = None if e is None else e.tag
        g = groups.setdefault(
            (setup, tag),
            {"setup": setup, "version": tag, "from_ms": None if e is None else e.from_ms, "rs": [], "closed": 0, "pnl": 0.0, "inside": 0},
        )
        g["closed"] += 1
        g["pnl"] += _num(t.get("pnl_usd")) or 0.0
        r = _num(t.get("r_multiple"))
        if r is not None:
            g["rs"].append(r)
        if e is not None and closed_ms is not None:
            later = [x.from_ms for x in eps if applies(x, setup) and x.from_ms > e.from_ms]
            if later and closed_ms > min(later):
                g["inside"] += 1
    rows = []
    for g in groups.values():
        rs = g["rs"]
        wins = sum(1 for v in rs if v > 0)
        rows.append(
            {
                "setup": g["setup"],
                "version": g["version"],
                "from_ms": g["from_ms"],
                "closed": g["closed"],
                "n_r": len(rs),
                "wins": wins,
                "win_share": wins / len(rs) if rs else None,
                "mean_r": statistics.fmean(rs) if rs else None,
                "pnl_usd": g["pnl"],
                "changed_inside": g["inside"],
                "enough": len(rs) >= min_n,
                "verdict": _verdict(len(rs), min_n),
            }
        )
    rows.sort(key=lambda x: (x["setup"] is None, x["setup"] or "", x["from_ms"] is None, x["from_ms"] or 0))
    return {
        "rows": rows,
        "epochs": [{"tag": e.tag, "from_ms": e.from_ms, "what": e.what, "source": e.source, "setups": list(e.setups)} for e in eps],
        "unversioned": sum(x["closed"] for x in rows if x["version"] is None),
        "min_n": min_n,
    }
