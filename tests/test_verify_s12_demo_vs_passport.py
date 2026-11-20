"""scripts/verify_s12_demo_vs_passport.py, the shadow-plan section (no network: the runtime, the candle and turnover
fetches and the clock are replaced; a synthetic migrated store): with --cutoff it is read as of the cutoff, so two runs
at different times repeat."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_s12_demo_vs_passport as mod  # noqa: E402


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


DAY = 86_400_000
HOUR = 3_600_000
CUTOFF = _ms(2026, 11, 12, 6)
NOW = _ms(2026, 11, 12, 12)


def _shadow(con: sqlite3.Connection, pid: int, base: str, decided: int, resolved: int | None) -> None:
    """A skipped S12 signal decided at ``decided`` and its shadow plan, resolved at ``resolved`` (a short at the target,
    unix ms) or still open (None)."""
    link = f"td-S12-s-1h-{base}-{decided // 1000}"
    con.execute(
        "INSERT INTO exec_audit (ts, venue, kind, link_id, symbol, setup, payload) VALUES (?, 'bybit-demo', 'skipped', ?, ?, 'S12', ?)",
        (decided, link, f"{base}/USDT:USDT", json.dumps({"violations": ["score 1 < 2"]})),
    )
    done = {"status": "resolved", "outcome": "target", "fill": 100.0, "exit": 94.0, "r_result": 1.9}
    if resolved is not None:
        done["resolved_ts"] = resolved // 1000
    plan = {
        "id": pid, "created_ts": decided // 1000, "exchange": "bybit", "symbol": f"{base}/USDT:USDT", "instrument": "perp",
        "side": "short", "entry_type": "market", "entry": 100.0, "stop": 103.0, "target": 94.0,
        "expires_ts": decided // 1000 + 7 * 86_400, "verdict": "enter",
        "tags": json.dumps(["src:demo-shadow", "setup:S12", "tf:1h", "demo:skipped"]),
        **(done if resolved is not None else {"status": "open"}),
    }  # fmt: skip
    con.execute(f"INSERT INTO trade_plan ({', '.join(plan)}) VALUES ({', '.join('?' * len(plan))})", tuple(plan.values()))


class _Rt:
    async def aclose(self) -> None:
        return None


@pytest.fixture
def offline(monkeypatch):
    """No network and a settable clock: ``clock["now"]`` is the script's run time (unix ms); fetches are counted."""
    clock = {"now": NOW, "fetch": 0}

    def venue_fetch(rt, b, venue):
        async def fetch(symbol: str, tf: str, since_ms: int, until_ms: int) -> list:
            clock["fetch"] += 1
            return []

        return fetch

    def day_fetch(rt, b):
        async def fetch(sym: str, start_ms: int, end_ms: int) -> list:
            clock["fetch"] += 1
            return []

        return fetch

    monkeypatch.setattr(mod, "Runtime", lambda settings: _Rt())
    monkeypatch.setattr(mod.dvb, "venue_fetch", venue_fetch)
    monkeypatch.setattr(mod.dvb, "bybit_day_fetch", day_fetch)
    monkeypatch.setattr(mod, "time", SimpleNamespace(time=lambda: clock["now"] / 1000, monotonic=time.monotonic))
    return clock


def _run(out: Path, db: Path, tag: str, cutoff: int) -> list[str]:
    """The report lines without the run-time header and the seed / RSS line."""
    assert mod.run(str(db), cutoff, out, "bybit", tag) == 0
    lines = (out / f"{tag}.txt").read_text(encoding="utf-8").splitlines()
    return [x for x in lines[1:] if not x.startswith("seed ")]


def _section(lines: list[str]) -> list[str]:
    i = next(k for k, x in enumerate(lines) if x.startswith("## Невиконані сигнали S12"))
    return lines[i : next(k for k, x in enumerate(lines) if x.startswith("## По угодах"))]


def test_shadow_plans_repeat_at_the_cutoff_whatever_the_run_time(tmp_path, migrated_db, offline):
    """Two runs on the same cutoff, the second later and after a shadow plan resolved past the cutoff and a decision
    after it: the same section and report. With ``now_ms`` of the run instead of the cutoff the second run counted the
    plan resolved."""
    con = sqlite3.connect(migrated_db)
    _shadow(con, 1, "SH1", CUTOFF - 3 * DAY, CUTOFF - DAY)  # resolved before the cutoff: in both runs
    _shadow(con, 2, "SH2", CUTOFF - DAY, None)  # open at the cutoff
    con.commit()
    con.close()
    out = tmp_path / "out"
    out.mkdir()
    a = _run(out, migrated_db, "a", CUTOFF)

    con = sqlite3.connect(migrated_db)
    con.execute(
        "UPDATE trade_plan SET status = 'resolved', outcome = 'target', fill = 100.0, exit = 94.0, resolved_ts = ? WHERE id = 2",
        ((CUTOFF + HOUR) // 1000,),
    )
    _shadow(con, 3, "SH3", CUTOFF + 3 * HOUR, CUTOFF + 4 * HOUR)  # decided after the cutoff: left out
    con.commit()
    con.close()
    offline["now"] = NOW + 5 * DAY
    b = _run(out, migrated_db, "b", CUTOFF)

    assert _section(a) == _section(b) and a == b
    sk = next(x for x in _section(a) if x.startswith("- skipped:"))
    assert "n 2, з R 1, відкрито 1" in sk  # SH2 open at the cutoff in both runs, SH3 not counted
    # a cutoff past SH2's resolution (still before SH3's decision) counts it resolved
    c = _run(out, migrated_db, "c", CUTOFF + 2 * HOUR)
    assert "n 2, з R 2, відкрито 0" in next(x for x in _section(c) if x.startswith("- skipped:"))
    assert offline["fetch"] == 0  # no S12 demo trades: nothing to replay
