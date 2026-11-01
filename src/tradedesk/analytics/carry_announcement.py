"""Strategy A exit on a delisting announcement (item 8n of IDEA-20261031-structural-edges-depth).

Announcements are parsed elsewhere (``news_events.classify_delisting``; titles are untrusted data). Here they are
matched to carry symbols and turned into causal rules for ``carry_delisting.simulate_carry_delisting``:

* ``exit_now(sym, t)`` is True once an announcement naming the symbol was published at or before ``t`` (the walk then
  closes both legs at the cycle ``t`` as a taker);
* the same predicate, negated, blocks new entries into the symbol after the announcement.

Placebo helpers build information-free early exits with a given distribution of lead times. Pure functions, no I/O;
times are epoch milliseconds.
"""

from __future__ import annotations

import bisect
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .news_events import perp_base

H_MS = 3_600_000
KINDS = ("perp_delist", "spot_delist")


def carry_symbol_ids(name: str, spot_id: str | None = None) -> dict[str, str]:
    """Perp symbol and spot base token of a carry market name: ``'1000PEPE/USDT:USDT'`` -> ``{'perp':
    '1000PEPEUSDT', 'spot_base': 'PEPE'}``. ``spot_id`` ('PEPEUSDT' or 'PEPE/USDT') overrides the spot base."""
    perp = name.split("/")[0].upper() + "USDT"
    if spot_id:
        base = spot_id.upper().replace("/", "")
        base = base[:-4] if base.endswith("USDT") else base
    else:
        base = perp_base(perp)[0]
    return {"perp": perp, "spot_base": base}


def announcement_index(rows: Iterable[Mapping[str, Any]], ids: Mapping[str, Mapping[str, str]]) -> dict[str, list[int]]:
    """``{name: sorted publication times}`` of announcements naming a carry symbol.

    ``rows``: ``{ms, kind, tickers}`` with ``kind`` 'perp_delist' (tickers are perp symbols, matched exactly to
    ``ids[name]['perp']``) or 'spot_delist' (tickers are base tokens, matched to ``ids[name]['spot_base']``); other
    kinds are ignored. ``ids``: output of ``carry_symbol_ids`` per name."""
    by_perp: dict[str, list[str]] = {}
    by_base: dict[str, list[str]] = {}
    for name, d in ids.items():
        by_perp.setdefault(d["perp"].upper(), []).append(name)
        by_base.setdefault(d["spot_base"].upper(), []).append(name)
    out: dict[str, set[int]] = {}
    for r in rows:
        if r.get("kind") not in KINDS:
            continue
        look = by_perp if r["kind"] == "perp_delist" else by_base
        for tick in r.get("tickers") or []:
            for name in look.get(str(tick).upper(), []):
                out.setdefault(name, set()).add(int(r["ms"]))
    return {k: sorted(v) for k, v in out.items()}


def announced_by(index: Mapping[str, Sequence[int]], sym: str, t: int) -> bool:
    """True when an announcement naming ``sym`` was published at or before ``t`` (data after ``t`` unused)."""
    ts = index.get(sym)
    return bool(ts) and ts[0] <= t


def first_announcement(index: Mapping[str, Sequence[int]], sym: str, upto: int | None = None) -> int | None:
    """Earliest announcement time of ``sym`` (at or before ``upto`` if given), else None."""
    ts = index.get(sym) or []
    if not ts or (upto is not None and ts[0] > upto):
        return None
    return ts[0]


def announcement_rule(index: Mapping[str, Sequence[int]]) -> tuple[Callable[[str, int], bool], Callable[[str, int], bool]]:
    """``(exit_now, entry_allowed)`` for the walk: exit once announced, never enter after the announcement."""

    def exit_now(sym: str, t: int) -> bool:
        return announced_by(index, sym, t)

    def entry_allowed(sym: str, t: int) -> bool:
        return not announced_by(index, sym, t)

    return exit_now, entry_allowed


def window_rule(windows: Mapping[str, Sequence[tuple[int, int]]]) -> tuple[Callable[[str, int], bool], Callable[[str, int], bool]]:
    """``(exit_now, entry_allowed)`` that force an exit and block entries while ``lo <= t <= hi`` for any window of
    the symbol (placebo exits without information)."""
    srt = {s: sorted(w) for s, w in windows.items()}
    los = {s: [a for a, _ in w] for s, w in srt.items()}

    def inside(sym: str, t: int) -> bool:
        w = srt.get(sym)
        if not w:
            return False
        i = bisect.bisect_right(los[sym], t) - 1
        return i >= 0 and t <= w[i][1]

    return inside, lambda sym, t: not inside(sym, t)


def placebo_windows(
    trades: Sequence[Mapping[str, Any]], leads_ms: Sequence[int], k: int, rng: random.Random, *, step_ms: int = 8 * H_MS
) -> dict[str, list[tuple[int, int]]]:
    """Pick ``k`` trades (``symbol``, ``open_ts``, ``close_ts``) without an event and give each an early exit
    ``close_ts - lead`` with a lead drawn from ``leads_ms``; trades too short for the drawn lead (exit before the
    first cycle after opening) are skipped. Returns ``{symbol: [(exit_from, close_ts)]}``."""
    pool = [t for t in trades if t["close_ts"] - t["open_ts"] > step_ms]
    rng.shuffle(pool)
    out: dict[str, list[tuple[int, int]]] = {}
    n = 0
    for tr in pool:
        if n >= k or not leads_ms:
            break
        lo = int(tr["close_ts"]) - int(rng.choice(list(leads_ms)))
        if lo <= int(tr["open_ts"]) + step_ms:
            continue
        out.setdefault(tr["symbol"], []).append((lo, int(tr["close_ts"])))
        n += 1
    return out


def paired_event_diffs(
    a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], keys: Iterable[tuple[str, int]]
) -> list[dict[str, Any]]:
    """Per-event P&L difference ``a - b`` for trades keyed by ``(symbol, open_ts)``; a missing trade counts as 0."""
    ia = {(t["symbol"], t["open_ts"]): t for t in a}
    ib = {(t["symbol"], t["open_ts"]): t for t in b}
    out = []
    for k in keys:
        pa = ia[k]["pnl"] if k in ia else 0.0
        pb = ib[k]["pnl"] if k in ib else 0.0
        out.append({"symbol": k[0], "open_ts": k[1], "pnl_a": pa, "pnl_b": pb, "diff": pa - pb})
    return out
