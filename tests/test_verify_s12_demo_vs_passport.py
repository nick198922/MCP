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


def _v9(day: int, r: float, side: str = "short", kind: str = "maker") -> dict:
    sig = _ms(2026, 11, day)
    return {
        "signal_ts": sig, "closed_ts": sig + 8 * HOUR, "r_demo": r, "side": side, "entry_kind": kind,
        "turnover_group": "ge5m", "entry_edge_r": 0.1, "plan_edge_r": 0.0, "fee_diff_r": -0.01, "r_diff": 0.0,
        "demo_exit": "stop", "bt_exit": "stop",
    }  # fmt: skip


def test_new_rows_split_at_the_seen_cutoff():
    """Seen = closed before the earlier comparison's cutoff, new = the rest; means are the plain means of each part,
    and the seen row does not change when trades closed after the cutoff change."""
    seen_ms = _ms(2026, 11, 12, 6)
    v9 = [_v9(1, 1.0), _v9(2, -1.0), _v9(3, 2.0), _v9(14, -1.0, "long", "taker"), _v9(15, 0.5), _v9(16, -0.5)]
    lines = mod.new_rows(v9, seen_ms)
    assert "n  3, днів  3, середнє +0.667" in lines[0]  # (1 − 1 + 2) / 3
    assert "n  3, днів  3, середнє -0.333" in lines[1]  # (−1 + 0.5 − 0.5) / 3
    assert "n 0" in next(x for x in lines if "без топ-3" in x)
    assert "n  1, днів  1, середнє -1.000" in next(x for x in lines if "нові лонг" in x)
    assert "середнє +0.100" in next(x for x in lines if "виконання входу" in x)
    assert ") +1.000 [" in next(x for x in lines if "мейкер − тейкер" in x)  # maker (0.5 − 0.5) / 2 = 0 minus taker −1
    changed = [*v9[:3], _v9(14, 3.0), _v9(17, 3.0)]
    assert mod.new_rows(changed, seen_ms)[0] == lines[0]
    assert mod.parse_utc("2026-11-12T06:00") == seen_ms


def _row(i: int, closed: int, r: float, version: str | None = "v9") -> dict:
    """A raw closed S12 demo row whose P&L equals its price P&L (not flagged by the P&L check)."""
    return {
        "link_id": f"td-S12-{i:03d}", "setup": "S12", "status": "closed", "version": version, "symbol": f"C{i % 40}/USDT:USDT",
        "side": "long", "timeframe": "4h", "venue": "bybit-demo", "signal_ts": closed - 12 * HOUR, "opened_ts": closed - 8 * HOUR,
        "closed_ts": closed, "rest_until": None, "r_multiple": r, "risk_usd": 10.0,
        "pnl_usd": 10.0 * r, "fill": 100.0, "exit": 100.0 + 10.0 * r, "qty": 1.0, "fees_usd": 0.0, "entry_kind": "maker",
    }  # fmt: skip


def _review(rs: list[float], extra: list[dict] | None = None) -> tuple[list[dict], int, list[dict], list[str]]:
    t0 = _ms(2026, 11, 1)
    raw = [_row(i, t0 + i * HOUR, r) for i, r in enumerate(rs)] + (extra or [])
    cutoff = t0 + 400 * HOUR
    closed = mod.classify(raw, cutoff)
    sample, eff, left = mod.review_sample(closed, 100, cutoff)
    return sample, eff, left, mod.review_lines(sample)


def test_review_takes_the_first_100_and_applies_the_frozen_floor():
    t0 = _ms(2026, 11, 1)
    v7 = {
        **_row(900, t0 + 5 * HOUR, 3.0, version=None),
        "rest_until": _ms(2026, 10, 28),
    }  # untagged with a resting limit: v7, kept only for descriptive rows
    sample, eff, left, lines = _review([-0.6] * 100 + [5.0, 5.0], [v7])
    assert sum(r["passport"] == "v4" for r in sample) == 100 and any(r["ver"] == "v7" for r in sample)
    assert eff == t0 + 99 * HOUR + 1 and len(left) == 2  # the two later trades are out, whatever their R
    main = next(i for i, x in enumerate(lines) if x.startswith("- [основний] R журнал"))
    assert "n 100" in lines[main] and "середнє -0.600" in lines[main]
    assert "рішення: зняти" in lines[main + 1] and "✗ поза ковзною смугою n 100" in lines[main + 1]
    assert "правило зупинки: тримати" in lines[main + 2]  # v9 −0.600 is above −0.827
    # exactly at the floor: not below it -> keep; the sample hash does not depend on R
    s2, _, _, lines2 = _review([-0.524] * 100)
    assert "рішення: тримати" in next(x for x in lines2 if x.startswith("    рішення"))
    assert mod.sample_hash(s2) == mod.sample_hash(sample)
    # changing the trades after the 100th changes nothing in the decision rows
    _, _, _, lines3 = _review([-0.6] * 100 + [-9.0, -9.0], [v7])
    assert lines3 == lines


def test_review_stops_with_fewer_than_100_trades():
    with pytest.raises(SystemExit):
        _review([0.1] * 99)
