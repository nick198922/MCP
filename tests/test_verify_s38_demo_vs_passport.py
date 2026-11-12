"""scripts/verify_s38_demo_vs_passport.py on a synthetic migrated store (no network: the candle and turnover fetches are
replaced): groups by version tag, the orphaned -1R scenario, the $1-5M turnover row, and a smoke stage without metrics."""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.core.envelope import Builder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_s38_demo_vs_passport as mod  # noqa: E402


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


DAY = 86_400_000
T0 = _ms(2026, 10, 26, 8)  # after the v5 epoch (2026-10-25 16:15 UTC): untagged trades map to v5 by their open time
CUTOFF = _ms(2026, 11, 12, 6)
NOW = _ms(2026, 11, 12, 12)
TAGGED_R = [2.0, -1.0, -1.0, 0.05, -1.0, 2.0, -1.0, 0.05, -1.0, -1.0, 2.0, -1.0]
UNTAGGED_R = [-1.0, 2.0, 0.05]
MID = {"MID1", "MID2", "MID3"}  # coins with a $2M entry day (the $1-5M group); the rest $10M
NAMES = ["MID1", "A1", "A2", "MID2", "A3", "A4", "A5", "MID3", "A6", "A7", "A8", "A9"]  # coins of the tagged trades


def _row(i: int, base: str, status: str, r: float | None = None, version: str | None = "v5", h: int = 0) -> dict:
    sig = T0 + i * DAY + h * 3_600_000  # each group on its own 4h bars: no bar above the demo's cap of 2
    row = {
        "link_id": f"L{i}-{base}",
        "venue": "bybit-demo",
        "symbol": f"{base}/USDT:USDT",
        "side": "short",
        "setup": "S38",
        "timeframe": "4h",
        "signal_ts": sig,
        "entry": 100.0,
        "stop": 103.0,
        "target": 94.0,
        "qty": 1.0,
        "leverage": 5,
        "risk_usd": 10.0,
        "comment": "test",
        "status": status,
        "version": version,
    }
    if status in ("closed", "orphaned", "open"):
        row |= {"fill": 100.0, "opened_ts": sig + 4 * 3_600_000 + 600_000, "entry_kind": "maker"}
    if status == "closed":
        row |= {
            "exit": 100.0 - 3.0 * r,
            "closed_ts": row["opened_ts"] + DAY,
            "pnl_usd": 10.0 * r,
            "r_multiple": r,
            "exit_reason": "target" if r > 1 else ("stop" if r < 0 else "breakeven"),
            "fees_usd": 0.1,
        }
    return row


def _store(path: Path, orphaned: int = 0) -> Path:
    rows = []
    for i, (r, base) in enumerate(zip(TAGGED_R, NAMES, strict=True)):
        rows.append(_row(i, base, "closed", r))
    for j, r in enumerate(UNTAGGED_R):
        rows.append(_row(j, f"U{j}", "closed", r, version=None, h=4))
    for k in range(orphaned):
        rows.append(_row(k, f"O{k}", "orphaned", h=8))
    rows.append(_row(14, "OPN", "open", h=12))
    rows.append(_row(13, "MIS", "missed", h=12))
    con = sqlite3.connect(path)
    for r in rows:
        cols = ", ".join(r)
        con.execute(f"INSERT INTO demo_trade ({cols}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    con.commit()
    con.close()
    return path


class _Rt:
    async def aclose(self) -> None:
        return None


@pytest.fixture
def offline(monkeypatch):
    """No network: candles are absent (the replay says no_data), the Bybit daily turnover is $2M for MID*, else $10M."""
    calls = {"fetch": 0}

    async def fetch(symbol: str, tf: str, since_ms: int, until_ms: int) -> list:
        calls["fetch"] += 1
        return []

    async def day(sid: str, start_ms: int, end_ms: int) -> list:
        turn = 2e6 if sid.removesuffix("USDT") in MID else 1e7
        return [[str(d), "1", "1", "1", "1", "1", str(turn)] for d in range(start_ms // DAY * DAY, end_ms + 1, DAY)]

    monkeypatch.setattr(mod, "open_sources", lambda venue, cache: (_Rt(), Builder(), fetch, day))
    return calls


def _run(tmp_path: Path, db: Path) -> list[str]:
    assert mod.run(str(db), CUTOFF, tmp_path, "bybit", "t", now_ms=NOW) == 0
    return (tmp_path / "t.txt").read_text(encoding="utf-8").splitlines()


def _line(lines: list[str], prefix: str) -> tuple[str, str]:
    i = next(i for i, x in enumerate(lines) if x.startswith(prefix))
    return lines[i], (lines[i + 1] if i + 1 < len(lines) else "")


def test_groups_by_version_tag_against_the_s38_passport(tmp_path, migrated_db, offline, capsys):
    lines = _run(tmp_path, _store(migrated_db))
    tagged, comp = _line(lines, "- v5 (тег)")
    m = statistics.fmean(TAGGED_R)
    assert "n 12," in tagged and f"середнє {m:+.3f}" in tagged
    assert "паспорт S38 OOS -0.032 [-0.144; +0.084]" in comp and "[-0.372; +0.335]" in comp and "[-0.535; +0.576]" in comp
    # n 12 < 30: the band of 30 trades gives no verdict here, only the neutral line
    assert "->" not in comp and not any(v in comp for v in ("✓", "≈", "✗"))
    assert lines[lines.index(comp) + 1] == "    n < 30: смуга паспорта не застосовна"
    untagged, ucomp = _line(lines, "- v5 (без тегу, за open_time)")
    assert "n  3," in untagged and "(n < 10: невимірно)" in untagged
    assert "->" not in ucomp and lines[lines.index(ucomp) + 1] == "    n < 30: смуга паспорта не застосовна"
    assert not any("✓" in x or "≈ поза" in x or "✗ поза" in x for x in lines)
    both, _ = _line(lines, "- v5 разом (тег + без тегу)")
    assert "n 15," in both and f"середнє {statistics.fmean(TAGGED_R + UNTAGGED_R):+.3f}" in both
    assert any(x.startswith("не змодельовано: ліміт демо 2 входи S38 на 4h-свічку") for x in lines)
    assert "missed 1; open 1; orphaned 0" in lines[1]
    assert _line(lines, "- v5:")[0].startswith("- v5: заповнено 16, не заповнено 1")
    doc = json.loads((tmp_path / "t_rows.json").read_text(encoding="utf-8"))
    assert len(doc["rows"]) == 15 and {r["verdict"] for r in doc["rows"]} == {"no_data"}
    assert offline["fetch"] > 0
    assert "## Пункт 6" in capsys.readouterr().out


def test_orphaned_trades_closed_at_the_stop_as_a_scenario(tmp_path, migrated_db, offline):
    lines = _run(tmp_path, _store(migrated_db, orphaned=2))
    assert "orphaned 2" in lines[1]
    orph, _ = _line(lines, "- orphaned v5 (тег): 2;")
    m = statistics.fmean([*TAGGED_R, -1.0, -1.0])
    assert "n 14," in orph and f"середнє {m:+.3f}" in orph
    assert "->" not in orph and orph.endswith("; n < 30: смуга паспорта не застосовна")
    assert _line(lines, "- orphaned v5 (без тегу, за open_time): 0")[0]
    # the sample itself does not change: the tagged row stays at 12 trades
    assert "n 12," in _line(lines, "- v5 (тег)")[0]


def test_one_to_five_million_coins_get_their_own_row(tmp_path, migrated_db, offline):
    lines = _run(tmp_path, _store(migrated_db))
    mid_r = [r for r, b in zip(TAGGED_R, NAMES, strict=True) if b in MID]
    rest = [r for r, b in zip(TAGGED_R, NAMES, strict=True) if b not in MID] + UNTAGGED_R
    mid, ref = _line(lines, "- оборот $1–5M")
    assert "n  3," in mid and f"середнє {statistics.fmean(mid_r):+.3f}" in mid
    assert "гандикап шортів до ≈ 0.35R" in ref and "−0.162" in ref
    ge5, _ = _line(lines, "- оборот ≥ $5M")
    assert "n 12," in ge5 and f"середнє {statistics.fmean(rest):+.3f}" in ge5
    no15, comp = _line(lines, "- без $1–5M")
    assert "n 12," in no15 and "паспорт S38 OOS" in comp
    assert "n 0" in _line(lines, "- оборот невідомий")[0]


def test_without_one_to_five_million_is_ge5m_lt1m_and_unknown(tmp_path, migrated_db, monkeypatch):
    """The "без $1–5M" row says what it holds and counts the < $1M and unknown-turnover trades too."""

    async def fetch(symbol: str, tf: str, since_ms: int, until_ms: int) -> list:
        return []

    async def day(sid: str, start_ms: int, end_ms: int) -> list:
        base = sid.removesuffix("USDT")
        if base.startswith("UNK"):
            return []  # no daily candle: turnover unknown
        turn = 2e6 if base in MID else (5e5 if base.startswith("LOW") else 1e7)
        return [[str(d), "1", "1", "1", "1", "1", str(turn)] for d in range(start_ms // DAY * DAY, end_ms + 1, DAY)]

    monkeypatch.setattr(mod, "open_sources", lambda venue, cache: (_Rt(), Builder(), fetch, day))
    names = ["MID1", "A1", "LOW1", "UNK1", "A2", "LOW2"]
    rs = [-1.0, 2.0, 0.05, -1.0, -1.0, 2.0]
    con = sqlite3.connect(migrated_db)
    for i, (base, r) in enumerate(zip(names, rs, strict=True)):
        row = _row(i, base, "closed", r)
        con.execute(f"INSERT INTO demo_trade ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    con.commit()
    con.close()
    lines = _run(tmp_path, migrated_db)
    assert "n  2," in _line(lines, "- оборот < $1M")[0] and "n  1," in _line(lines, "- оборот невідомий")[0]
    assert "n  2," in _line(lines, "- оборот ≥ $5M")[0] and "n  1," in _line(lines, "- оборот $1–5M")[0]
    no15, _ = _line(lines, "- без $1–5M (= ≥ $5M + < $1M + невідомий; описово)")
    rest = [r for r, b in zip(rs, names, strict=True) if b not in MID]
    assert "n  5," in no15 and f"середнє {statistics.fmean(rest):+.3f}" in no15


def test_band_verdict_only_from_thirty_trades():
    s = {"mean": -0.03, "ci": (-0.2, 0.15), "days": 10}
    small = mod.passport_lines({**s, "n": 29})
    assert "->" not in small[0] and small[1:] == ["    n < 30: смуга паспорта не застосовна"]
    full = mod.passport_lines({**s, "n": 30})
    assert len(full) == 1 and "-> ✓ всередині i.i.d.-смуги;" in full[0]
    assert "-> ✗ поза ковзною смугою" in mod.passport_lines({**s, "n": 31, "mean": -0.9})[0]


def test_run_prints_the_band_verdict_for_a_group_of_thirty(tmp_path, migrated_db, offline):
    rs = [2.0, -1.0, -1.0] * 10  # mean 0.0: inside the i.i.d. band
    con = sqlite3.connect(migrated_db)
    for i, r in enumerate(rs):
        row = _row(i // 2, f"B{i}", "closed", r, h=4 * (i % 2))
        con.execute(f"INSERT INTO demo_trade ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    con.commit()
    con.close()
    lines = _run(tmp_path, migrated_db)
    tagged, comp = _line(lines, "- v5 (тег)")
    assert "n 30," in tagged and "-> ✓ всередині i.i.d.-смуги;" in comp
    assert lines[lines.index(comp) + 1] != "    n < 30: смуга паспорта не застосовна"


def test_smoke_prints_counts_but_no_outcome_metric(tmp_path, migrated_db, offline, capsys):
    db = _store(migrated_db, orphaned=2)
    assert mod.smoke(str(db), CUTOFF, tmp_path, "bybit", now_ms=NOW) == 0
    out = capsys.readouterr().out
    assert "closed < cutoff: 15; missed 1; open 1; orphaned 2" in out
    assert "(('v5', 'column'), 12)" in out and "(('v5', 'open_time'), 3)" in out
    assert "'1to5m': 3" in out and "'ge5m': 12" in out
    assert "bars above the cap of 2: 0" in out
    for word in ("середнє", "mean", "сума", "R>0", "смуга", "паспорт", "✓", "match", "-0.075", "+0.075"):
        assert word not in out, word
    for r in {*TAGGED_R, *UNTAGGED_R, statistics.fmean(TAGGED_R), statistics.fmean(TAGGED_R + UNTAGGED_R)}:
        assert f"{r:+.3f}" not in out and f"{r:+.2f}" not in out
    assert not list(tmp_path.glob("*.txt"))  # the smoke writes no report


def test_run_needs_the_registered_cutoff(tmp_path):
    with pytest.raises(SystemExit) as e:
        mod.main(["--stage", "run", "--db", str(tmp_path / "x.sqlite"), "--cache-dir", str(tmp_path)])
    assert e.value.code == 2
