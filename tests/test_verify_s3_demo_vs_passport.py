"""scripts/verify_s3_demo_vs_passport.py, the shadow-plan section (no network: a JSON of demo_vs_backtest and a synthetic
migrated store): with --cutoff it is read from the store as of the cutoff, so two runs at different times repeat."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_s3_demo_vs_passport as mod  # noqa: E402


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


DAY = 86_400_000
HOUR = 3_600_000
CUTOFF = _ms(2026, 11, 12, 6)
CUTOFF_ARG = "2026-11-12T06:00"
NOW = _ms(2026, 11, 12, 12)


def _shadow(con: sqlite3.Connection, pid: int, base: str, decided: int, resolved: int | None) -> None:
    """A skipped S3 signal decided at ``decided`` and its shadow plan, resolved at ``resolved`` (a short at the target,
    unix ms) or still open (None)."""
    link = f"td-S3-s-4h-{base}-{decided // 1000}"
    con.execute(
        "INSERT INTO exec_audit (ts, venue, kind, link_id, symbol, setup, payload) VALUES (?, 'bybit-demo', 'skipped', ?, ?, 'S3', ?)",
        (decided, link, f"{base}/USDT:USDT", json.dumps({"violations": ["score 1 < 2"]})),
    )
    done = {"status": "resolved", "outcome": "target", "fill": 100.0, "exit": 94.0, "r_result": 1.9}
    if resolved is not None:
        done["resolved_ts"] = resolved // 1000
    plan = {
        "id": pid, "created_ts": decided // 1000, "exchange": "bybit", "symbol": f"{base}/USDT:USDT", "instrument": "perp",
        "side": "short", "entry_type": "market", "entry": 100.0, "stop": 103.0, "target": 94.0,
        "expires_ts": decided // 1000 + 7 * 86_400, "verdict": "enter",
        "tags": json.dumps(["src:demo-shadow", "setup:S3", "tf:4h", "demo:skipped"]),
        **(done if resolved is not None else {"status": "open"}),
    }  # fmt: skip
    con.execute(f"INSERT INTO trade_plan ({', '.join(plan)}) VALUES ({', '.join('?' * len(plan))})", tuple(plan.values()))


def _dvb_json(path: Path, generated_ms: int, ne_rows: list[dict]) -> Path:
    """A minimal demo_vs_backtest --json output: no closed trades, the not-executed block as of ``generated_ms``."""
    doc = {
        "schema": 2,
        "generated_ms": generated_ms,
        "venue": "bybit",
        "rows": [],
        "not_executed": {"now_ms": generated_ms, "rows": ne_rows},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _ne_row(plan_id: int, status: str, r: float | None) -> dict:
    return {
        "group": "skipped", "reason": "score N < 2", "example": "score 1 < 2", "link_id": None, "symbol": "X/USDT:USDT",
        "setup": "S3", "timeframe": "4h", "side": "short", "decided_ts": CUTOFF - DAY, "plan_id": plan_id,
        "status": status, "outcome": "target" if r is not None else None, "r_plan": None, "r": r,
    }  # fmt: skip


def _section(out: Path) -> list[str]:
    lines = out.read_text(encoding="utf-8").splitlines()
    i = next(k for k, x in enumerate(lines) if x.startswith("## Невиконані сигнали"))
    return lines[i : next(k for k, x in enumerate(lines) if x.startswith("BE_SWITCH_MS"))]


def _run(tmp_path: Path, tag: str, js: Path, *extra: str) -> list[str]:
    out = tmp_path / f"{tag}.txt"
    assert mod.main(["--json", str(js), "--out", str(out), *extra]) == 0
    return _section(out)


def test_shadow_section_repeats_at_the_cutoff_whatever_the_run_time(tmp_path, migrated_db):
    """The same cutoff, two JSONs generated at different times (their own shadow rows differ) and the store later
    holding a plan resolved after the cutoff and a decision after it: the same section. Before 20.11 the section took
    ``now_ms`` of the JSON, so a later JSON counted that plan resolved."""
    con = sqlite3.connect(migrated_db)
    _shadow(con, 1, "SH1", CUTOFF - 3 * DAY, CUTOFF - DAY)  # resolved before the cutoff: in both runs
    _shadow(con, 2, "SH2", CUTOFF - DAY, None)  # open at the cutoff
    con.commit()
    con.close()
    j1 = _dvb_json(tmp_path / "a.json", NOW, [_ne_row(1, "resolved", 1.9), _ne_row(2, "open", None)])
    a = _run(tmp_path, "a", j1, "--cutoff", CUTOFF_ARG, "--db", str(migrated_db))

    con = sqlite3.connect(migrated_db)
    con.execute(
        "UPDATE trade_plan SET status = 'resolved', outcome = 'target', fill = 100.0, exit = 94.0, resolved_ts = ? WHERE id = 2",
        ((CUTOFF + HOUR) // 1000,),
    )
    _shadow(con, 3, "SH3", CUTOFF + 3 * HOUR, CUTOFF + 4 * HOUR)  # decided after the cutoff: left out
    con.commit()
    con.close()
    later = [_ne_row(1, "resolved", 1.9), _ne_row(2, "resolved", 1.9), _ne_row(3, "resolved", 1.9)]
    j2 = _dvb_json(tmp_path / "b.json", NOW + 5 * DAY, later)
    b = _run(tmp_path, "b", j2, "--cutoff", CUTOFF_ARG, "--db", str(migrated_db))

    assert a == b
    sk = next(x for x in a if x.startswith("- skipped:"))
    assert "n 2, з R 1, відкрито 1" in sk  # SH2 open at the cutoff in both runs, SH3 not counted
    # without --cutoff the section is the later JSON's, as before: all three resolved
    assert "n 3, з R 3, відкрито 0" in next(x for x in _run(tmp_path, "c", j2) if x.startswith("- skipped:"))
    # a cutoff past SH2's resolution (still before SH3's decision) counts it resolved
    d = _run(tmp_path, "d", j2, "--cutoff", "2026-11-12T07:30", "--db", str(migrated_db))
    assert "n 2, з R 2, відкрито 0" in next(x for x in d if x.startswith("- skipped:"))


def test_shadow_section_without_a_store_says_so(tmp_path):
    js = _dvb_json(tmp_path / "a.json", NOW, [_ne_row(1, "resolved", 1.9)])
    sec = _run(tmp_path, "a", js, "--cutoff", CUTOFF_ARG, "--db", str(tmp_path / "absent.sqlite"))
    assert sec[0].endswith(f"— no database at {tmp_path / 'absent.sqlite'}")
    assert sec[1] == "рядків немає"
