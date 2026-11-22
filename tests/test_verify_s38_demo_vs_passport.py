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


SEEN = T0 + 6 * DAY  # tagged trades i 0..4 closed before it (i + 1 days after T0 + 4h10m), i 5..11 after; untagged all before


def test_seen_cutoff_splits_each_group_into_seen_and_new(tmp_path, migrated_db, offline):
    db = _store(migrated_db)
    assert mod.run(str(db), CUTOFF, tmp_path, "bybit", "t", now_ms=NOW, seen_ms=SEEN) == 0
    lines = (tmp_path / "t.txt").read_text(encoding="utf-8").splitlines()
    new, _ = _line(lines, "- v5 разом (тег + без тегу): нові (закриті ≥ 2026-11-01 08:00)")
    old, _ = _line(lines, "- v5 разом (тег + без тегу): бачені (закриті < 2026-11-01 08:00)")
    assert "n  7," in new and f"середнє {statistics.fmean(TAGGED_R[5:]):+.3f}" in new
    assert "n  8," in old and f"середнє {statistics.fmean(TAGGED_R[:5] + UNTAGGED_R):+.3f}" in old
    # the main rows are unchanged by the split
    assert "n 15," in _line(lines, "- v5 разом (тег + без тегу) ")[0]
    mid = statistics.fmean([r for r, b in zip(TAGGED_R, NAMES, strict=True) if b in MID])
    rest = statistics.fmean([r for r, b in zip(TAGGED_R, NAMES, strict=True) if b not in MID] + UNTAGGED_R)
    contrast = _line(lines, "- контраст $1–5M − без $1–5M, CI кластерами за днем сигналу:")[0].split(": ")[1]
    assert float(contrast.split(" [")[0]) == pytest.approx(mid - rest, abs=1.1e-3)


def test_smoke_counts_the_new_trades_without_outcome(tmp_path, migrated_db, offline, capsys):
    assert mod.smoke(str(_store(migrated_db)), CUTOFF, tmp_path, "bybit", now_ms=NOW, seen_ms=SEEN) == 0
    out = capsys.readouterr().out
    assert "closed >= seen cutoff 2026-11-01 08:00 UTC: 7 (versions [(('v5', 'column'), 7)]); signal days 7" in out
    assert "середнє" not in out and f"{statistics.fmean(TAGGED_R[5:]):+.3f}" not in out


@pytest.fixture
def s39():
    mod.configure("S39")
    yield
    mod.configure("S38")


def test_s39_runs_against_its_own_passport_without_a_bar_cap(tmp_path, migrated_db, offline, s39):
    db = _store(migrated_db)
    con = sqlite3.connect(db)
    con.execute("UPDATE demo_trade SET setup = 'S39'")
    con.commit()
    con.close()
    lines = _run(tmp_path, db)
    assert lines[0].startswith("# S39: демо проти паспорта")
    _, comp = _line(lines, "- v5 (тег)")
    assert "паспорт S39 OOS +0.002 [-0.236; +0.245]" in comp and "[-0.327; +0.346]" in comp and "[-0.837; +1.010]" in comp
    assert any(x.endswith("ліміту входів на свічку для S39 немає") for x in lines)
    assert not any("ліміт демо 2 входи" in x for x in lines)
    assert any("паспорт OOS −0.162 (n 102, рядок 57)" in x for x in lines)


@pytest.fixture
def s3():
    mod.configure("S3")
    yield
    mod.configure("S38")


def test_s3_runs_against_its_passport_with_the_30_10_splits(tmp_path, migrated_db, offline, s3):
    db = _store(migrated_db)
    con = sqlite3.connect(db)
    con.execute("UPDATE demo_trade SET setup = 'S3', version = NULL")
    con.execute("UPDATE demo_trade SET timeframe = '1d', side = 'long' WHERE symbol LIKE 'U%'")
    con.commit()
    con.close()
    assert mod.run(str(db), CUTOFF, tmp_path, "bybit", "t", now_ms=NOW, seen_ms=SEEN) == 0
    lines = (tmp_path / "t.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# S3: демо проти паспорта")
    allr, comp = _line(lines, "- усі 15")
    assert f"середнє {statistics.fmean(TAGGED_R + UNTAGGED_R):+.3f}" in allr
    assert "паспорт S3 OOS -0.076 [-0.155; +0.004]" in comp and "[-0.473; +0.361]" in comp and "[-0.719; +0.652]" in comp
    one_d, _ = _line(lines, "- 1d ")
    assert "n  3," in one_d and f"середнє {statistics.fmean(UNTAGGED_R):+.3f}" in one_d
    assert any(x.startswith("- 4h short") for x in lines) and any(x.startswith("- беззбиток ") for x in lines)
    assert any(x.startswith("лише нові угоди (закриті ≥") for x in lines)
    assert any(x.startswith("паспорт: свічки й ранг Binance, топ-100 і ≥ $5M") for x in lines)
    assert not any("ліміт демо 2 входи" in x for x in lines)


def test_open_and_other_are_classified_at_the_cutoff():
    """opened_ts < cutoff <= closed_ts -> open at the cutoff, whatever the status is today; filled after it -> other."""
    c = CUTOFF
    rows = {
        "closed_after": {"status": "closed", "signal_ts": c - 9, "opened_ts": c - 5, "closed_ts": c + 1},
        "closed_at": {"status": "closed", "signal_ts": c - 8, "opened_ts": c - 5, "closed_ts": c},
        "still_open": {"status": "open", "signal_ts": c - 7, "opened_ts": c - 5},
        "open_no_time": {"status": "open", "signal_ts": c - 6, "opened_ts": None},
        "open_late": {"status": "open", "signal_ts": c - 4, "opened_ts": c},
        "closed_late": {"status": "closed", "signal_ts": c - 3, "opened_ts": c + 1, "closed_ts": c + 2},
        "cancelled": {"status": "cancelled", "signal_ts": c - 2},
        # closed without a close time: it may have closed before the cutoff, so not "open"; stays "other" as load_s12 put it
        "closed_no_time": {"status": "closed", "signal_ts": c - 1, "opened_ts": c - 5, "closed_ts": None},
    }
    rows = {k: {**v, "link_id": k} for k, v in rows.items()}
    g = {"closed": [], "missed": [{"link_id": "m"}], "orphaned": [], "open": [], "other": []}
    for k in ("still_open", "open_no_time", "open_late"):
        g["open"].append(rows[k])
    for k in ("closed_after", "closed_at", "closed_late", "cancelled", "closed_no_time"):
        g["other"].append(rows[k])
    out = mod.at_cutoff(g, c)
    assert [r["link_id"] for r in out["open"]] == ["closed_after", "closed_at", "still_open", "open_no_time"]
    assert [r["link_id"] for r in out["other"]] == ["open_late", "closed_late", "cancelled", "closed_no_time"]
    assert out["missed"] == g["missed"] and out["closed"] == [] and out["orphaned"] == []
    assert len(g["open"]) == 3  # the input groups are not changed


def test_run_counts_a_trade_closed_after_the_cutoff_as_open_and_filled(tmp_path, migrated_db, offline):
    """A repeat after the open trade closed gives the same open / other and filled counts as the run at the cutoff."""
    db = _store(migrated_db)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE demo_trade SET status = 'closed', exit = 100.0, closed_ts = ?, pnl_usd = 0.0, r_multiple = 0.0, "
        "exit_reason = 'breakeven' WHERE symbol LIKE 'OPN/%'",
        (CUTOFF + 600_000,),
    )
    con.commit()
    con.close()
    lines = _run(tmp_path, db)
    assert "missed 1; open 1; orphaned 0; other 0" in lines[1] and lines[1].startswith("закритих 15;")
    assert _line(lines, "- v5:")[0].startswith("- v5: заповнено 16, не заповнено 1")


def _shadow(con: sqlite3.Connection, pid: int, base: str, decided: int, resolved: int | None) -> None:
    """A skipped S38 signal decided at ``decided`` and its shadow plan, resolved at ``resolved`` (a short at the target,
    unix ms) or still open (None)."""
    link = f"td-S38-s-4h-{base}-{decided // 1000}"
    con.execute(
        "INSERT INTO exec_audit (ts, venue, kind, link_id, symbol, setup, payload) VALUES (?, 'bybit-demo', 'skipped', ?, ?, 'S38', ?)",
        (decided, link, f"{base}/USDT:USDT", json.dumps({"violations": ["score 1 < 2"]})),
    )
    done = {"status": "resolved", "outcome": "target", "fill": 100.0, "exit": 94.0, "r_result": 1.9}
    if resolved is not None:
        done["resolved_ts"] = resolved // 1000
    plan = {
        "id": pid, "created_ts": decided // 1000, "exchange": "bybit", "symbol": f"{base}/USDT:USDT", "instrument": "perp",
        "side": "short", "entry_type": "market", "entry": 100.0, "stop": 103.0, "target": 94.0,
        "expires_ts": decided // 1000 + 7 * 86_400, "verdict": "enter",
        "tags": json.dumps(["src:demo-shadow", "setup:S38", "tf:4h", "demo:skipped"]),
        **(done if resolved is not None else {"status": "open"}),
    }  # fmt: skip
    con.execute(f"INSERT INTO trade_plan ({', '.join(plan)}) VALUES ({', '.join('?' * len(plan))})", tuple(plan.values()))


def test_shadow_plans_repeat_at_the_cutoff_whatever_the_run_time(tmp_path, migrated_db, offline):
    """Two runs on the same cutoff, the second later and after a shadow plan resolved past the cutoff: the same report
    (only the run-time header and the RSS line differ). Before 20.11 the second run counted that plan resolved."""
    db = _store(migrated_db)
    con = sqlite3.connect(db)
    _shadow(con, 1, "SH1", CUTOFF - 3 * DAY, CUTOFF - DAY)  # resolved before the cutoff: in both runs
    _shadow(con, 2, "SH2", CUTOFF - DAY, None)  # open at the cutoff
    con.commit()
    con.close()
    out = tmp_path / "out"
    out.mkdir()
    assert mod.run(str(db), CUTOFF, out, "bybit", "a", now_ms=NOW) == 0
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE trade_plan SET status = 'resolved', outcome = 'target', fill = 100.0, exit = 94.0, resolved_ts = ? WHERE id = 2",
        ((CUTOFF + 3_600_000) // 1000,),
    )
    con.commit()
    con.close()
    assert mod.run(str(db), CUTOFF, out, "bybit", "b", now_ms=NOW + 5 * DAY) == 0

    def body(tag: str) -> list[str]:
        lines = (out / f"{tag}.txt").read_text(encoding="utf-8").splitlines()
        return [x for x in lines[1:] if not x.startswith("seed ")]

    a, b = body("a"), body("b")
    assert a == b
    sk, _ = _line(a, "- skipped:")
    assert "n 2, з R 1, відкрито 1" in sk  # SH2 open at the cutoff in both runs
    # the plan does count as resolved once the cutoff is past its resolution
    assert mod.run(str(db), CUTOFF + 2 * 3_600_000, out, "bybit", "c", now_ms=NOW + 5 * DAY) == 0
    assert "n 2, з R 2, відкрито 0" in _line(body("c"), "- skipped:")[0]


def test_run_prints_the_n_ref_band_from_the_setup_config(tmp_path, migrated_db, offline, s3):
    db = _store(migrated_db)
    con = sqlite3.connect(db)
    con.execute("UPDATE demo_trade SET setup = 'S3'")
    con.commit()
    con.close()
    lines = _run(tmp_path, db)
    band = "смуга n=50 (docs/CHECKPOINT-2026-11-30.md:112): i.i.d. [-0.388; +0.265], ковзна [-0.545; +0.494]"
    assert any(x.startswith(band) for x in lines)
    assert mod.ref_band_line().startswith(band)
    mod.configure("S38")
    assert mod.ref_band_line().startswith("смуга n=50 (docs/CHECKPOINT-2026-11-30.md:118): i.i.d. [-0.296; +0.245]")


def test_main_selects_the_setup(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(mod, "run", lambda *a, **k: seen.update(setup=mod.SETUP, seen=k.get("seen_ms")) or 0)
    try:
        assert (
            mod.main(
                [
                    "--stage",
                    "run",
                    "--cutoff",
                    "2026-11-19T00:00",
                    "--setup",
                    "S40",
                    "--seen-cutoff",
                    "2026-11-12T10:00",
                    "--cache-dir",
                    str(tmp_path),
                ]
            )
            == 0
        )
    finally:
        mod.configure("S38")
    assert seen == {"setup": "S40", "seen": _ms(2026, 11, 12, 10)}


def test_run_needs_the_registered_cutoff(tmp_path):
    with pytest.raises(SystemExit) as e:
        mod.main(["--stage", "run", "--db", str(tmp_path / "x.sqlite"), "--cache-dir", str(tmp_path)])
    assert e.value.code == 2
