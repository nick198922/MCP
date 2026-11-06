"""Stuck demo trades: ``demo_trade`` rows still ``open`` well past their hold (pure: no I/O, time passed in).

Item 133 of docs/ideas/IDEA-20261102-standing-improvements.md. The demo trader closes a filled trade by time once
``now - opened_ts > hold_ms(setup, timeframe)`` (execution/demo_trader.py, ``_sync_trade``). A row that stays ``open``
past that hold plus :data:`GRACE_MS` (one day) has escaped the time exit (NEAR / HYPE sat ``open`` for 10 days in the
03.11 audit); such rows drop out of the closed samples the 30.11 checkpoint counts, so they are named in the morning
brief (scripts/collector_health.py) and in scripts/checkpoint_report.py. Nothing here closes a row: that stays a
session task for Claude with the owner (BACKLOG).

The hold rules are copied from execution/demo_trader.py (``SETUP_HOLD_MS``, ``MAX_HOLD_MS``, the 7-day default of
``hold_ms``): ``analytics/`` may not import ``execution/`` (tests/test_architecture_imports.py), and
tests/test_stuck_demo.py pins the copies equal to the trader's.

Rows without ``opened_ts`` (the trader always writes it with ``status = 'open'``, so only an older or hand-edited row)
are aged from ``signal_ts`` instead, an earlier moment, and marked ``from_signal``: the trader's own time exit never
fires for them (it reads a missing ``opened_ts`` as "now"). A row without either time is skipped. A missing
``timeframe`` takes the trader's default hold (7 days) unless the setup has its own.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
# Copies of execution/demo_trader.py (SETUP_HOLD_MS, MAX_HOLD_MS, hold_ms default); pinned equal in tests/test_stuck_demo.py.
SETUP_HOLD_MS = {"S17": 20 * DAY_MS}
MAX_HOLD_MS = {"4h": 7 * DAY_MS, "1d": 30 * DAY_MS}
DEFAULT_HOLD_MS = 7 * DAY_MS
GRACE_MS = DAY_MS  # past the hold by more than this = stuck
MAX_BRIEF_ITEMS = 4  # trades named in the brief's line; the rest is counted


def hold_ms(setup: str | None, timeframe: str | None) -> int:
    """How long the demo trader holds a trade of ``setup`` on ``timeframe``, ms (same rule as execution's ``hold_ms``)."""
    return SETUP_HOLD_MS.get(setup or "") or MAX_HOLD_MS.get(timeframe or "", DEFAULT_HOLD_MS)


def _int(x: object) -> int | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return int(x)


def stuck_trades(rows: Iterable[Mapping[str, Any]], now_ms: int, grace_ms: int = GRACE_MS) -> list[dict[str, Any]]:
    """The ``open`` rows whose age at ``now_ms`` (UTC ms) exceeds their hold plus ``grace_ms``, oldest first. Each:
    ``id`` (link_id), ``setup``, ``symbol``, ``age_h`` (hours since the fill, or since the signal bar when
    ``from_signal``), ``hold_h`` (the hold that applies, hours), ``from_signal``."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") != "open":
            continue
        opened = _int(r.get("opened_ts"))
        start = opened if opened is not None else _int(r.get("signal_ts"))
        if start is None:
            continue
        hold = hold_ms(r.get("setup"), r.get("timeframe"))
        age = now_ms - start
        if age > hold + grace_ms:
            out.append(
                {
                    "id": r.get("link_id"), "setup": r.get("setup"), "symbol": r.get("symbol"), "age_h": age / HOUR_MS,
                    "hold_h": hold / HOUR_MS, "from_signal": opened is None,
                }
            )  # fmt: skip
    return sorted(out, key=lambda t: -t["age_h"])


def describe(t: Mapping[str, Any]) -> str:
    """One stuck trade in Ukrainian: 'S3 NEAR/USDT:USDT 240 год (строк 168 год)'."""
    since = " від сигналу, без часу входу" if t.get("from_signal") else ""
    return f"{t.get('setup') or '?'} {t.get('symbol') or t.get('id')} {t['age_h']:.0f} год{since} (строк {t['hold_h']:.0f} год)"


def brief_line(stuck: list[Mapping[str, Any]]) -> str | None:
    """The morning brief's line (plain text, Ukrainian) naming at most :data:`MAX_BRIEF_ITEMS` stuck trades, the oldest
    first; None when there is none."""
    if not stuck:
        return None
    named = "; ".join(describe(t) for t in stuck[:MAX_BRIEF_ITEMS])
    more = f"; і ще {len(stuck) - MAX_BRIEF_ITEMS}" if len(stuck) > MAX_BRIEF_ITEMS else ""
    return f"Завислі демо-угоди: {len(stuck)} ({named}{more})."
