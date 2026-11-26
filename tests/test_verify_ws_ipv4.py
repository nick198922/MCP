"""8a IPv4 check (analytics/ws_ipv4_check.py, scripts/verify_ws_ipv4.py) on a synthetic journal and SQLite DB."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import ws_ipv4_check as w
from tradedesk.store.schema import MIGRATIONS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_ws_ipv4 as script  # noqa: E402

HR = w.HOUR
H = 1_790_000_000 // HR * HR  # a synthetic deploy hour
OK_S = 3560.0
STREAMS = ("binance", "bybit", "okx", *w.BOOK_DESCRIPTIVE)


def _journal(hours: int, *, fail: dict[str, int] | None = None, restarts: tuple[int, ...] = ()) -> dict:
    """``hours`` hours from H - 1 h, every stream connected OK_S; ``fail``: failures per hour of a stream;
    ``restarts``: hour offsets from H where Binance is below 3300 s."""
    j: dict[str, dict[int, tuple[float, int]]] = {s: {} for s in STREAMS}
    for k in range(-1, hours):
        h = H + k * HR
        for s in STREAMS:
            c = 3000.0 if (s == "binance" and k in restarts) else OK_S
            j[s][h] = (c, (fail or {}).get(s, 0))
    return j


def _at(hours: float) -> float:
    """The clock ``hours`` after H."""
    return H + hours * HR


def test_clean_hours_drop_restart_hour_the_next_one_missing_rows_and_unfinished_hours():
    j = _journal(10, restarts=(3,))
    del j["binance"][H + 6 * HR]  # no Binance row = a restart hour too
    clean = w.clean_hours(j, H, H + 10 * HR, _at(9) + 15 * 60)  # hour 8 complete exactly at 9:15
    assert [(x - H) // HR for x in clean] == [0, 1, 2, 5, 8]
    assert w.clean_hours(j, H + 8 * HR, H + 9 * HR, _at(9) + 15 * 60 - 1) == []


def test_rollback_b_waits_for_twelve_clean_hours_then_uses_strict_limits():
    j = _journal(20, fail={"bybit": 3, "okx": 4})
    pending = w.rollback_b(j, H, _at(13))  # clean hours 2..11 complete: 10
    assert not pending["ready"] and len(pending["hours"]) == 10 and not pending["triggered"]
    ready = w.rollback_b(j, H, _at(14) + 15 * 60)
    assert ready["ready"] and [(x - H) // HR for x in ready["hours"]] == list(range(2, 14))
    assert not ready["triggered"]
    j["bybit"][H + 5 * HR] = (OK_S, 3 + 11)  # 47 failures / 12 h = 3.92 > 3.9
    hit = w.rollback_b(j, H, _at(14) + 15 * 60)
    assert hit["triggered"] and hit["over"] == ["bybit"]
    j["bybit"][H + 5 * HR] = (OK_S, 3 + 10)  # 46 / 12 h = 3.83: not above 3.9
    assert not w.rollback_b(j, H, _at(14) + 15 * 60)["triggered"]


def test_rollback_b_is_no_rollback_when_binance_fails_too():
    j = _journal(20, fail={"bybit": 5, "binance": 1})
    b = w.rollback_b(j, H, _at(15))
    assert b["over"] == ["bybit"] and b["shared_incident"] and not b["triggered"]


def test_rollback_a_flags_a_low_ratio_and_a_missing_book_row():
    j = _journal(6)
    assert not w.rollback_a_hours(j, w.clean_hours(j, H, H + 6 * HR, _at(7)))["triggered"]
    j["okx"][H + 2 * HR] = (0.89 * OK_S, 0)
    del j["coinbase:book"][H + 3 * HR]
    a = w.rollback_a_hours(j, w.clean_hours(j, H, H + 6 * HR, _at(7)))
    assert a["triggered"] and {(x - H, s) for x, s, _ in a["low"]} == {(2 * HR, "okx"), (3 * HR, "coinbase:book")}
    j = _journal(6)
    j["bybit"][H + 2 * HR] = (3204.0, 0)  # 3204 / 3560 = 0.9 exactly: not below
    assert not w.rollback_a_hours(j, [H + 2 * HR])["low"]


def test_rollback_a_binance_exemption_holds_hour_by_hour_not_pooled():
    j = _journal(6)
    j["bybit"][H + 2 * HR] = (0.5 * OK_S, 0)
    j["bybit"][H + 3 * HR] = (0.5 * OK_S, 0)
    j["binance"][H + 3 * HR] = (OK_S, 1)  # pooled: 1 failure / 2 h = 0.5 > 0.2 would have exempted both hours
    a = w.rollback_a_hours(j, [H + 2 * HR, H + 3 * HR])
    assert a["exempt_hours"] == [H + 3 * HR] and a["triggered"]
    j["binance"][H + 2 * HR] = (OK_S, 1)  # Binance failed in every flagged hour: a shared incident, no rollback
    a = w.rollback_a_hours(j, [H + 2 * HR, H + 3 * HR])
    assert a["shared_incident"] and not a["triggered"]


def test_health_parse_consecutive_failures_and_ipv6_peer():
    data = {
        "collector": [
            {"name": "liquidations:bybit", "stream": {"consecutive_failures": 5, "peer": "18.245.199.83"}},
            {"name": "liquidations:okx", "stream": {"consecutive_failures": 0, "peer": "2606:4700::6812"}},
            {"name": "book_walls:coinbase", "stream": {"book": {"consecutive_failures": 1, "peer": "172.64.152.241"}}},
        ]
    }
    s = w.health_streams(data)
    assert s["book_walls:coinbase"] == {"consecutive_failures": 1, "peer": "172.64.152.241"}
    r = w.rollback_a_health(s, 0.0)
    assert r["triggered"] and r["consecutive_hits"] == ["liquidations:bybit"]
    assert r["not_ipv4"] == ["liquidations:okx"] and not r["deploy_confirmed"]
    assert w.rollback_a_health(s, 0.5)["shared_incident"]
    gone = w.health_streams({"collector": []})
    assert w.rollback_a_health(gone, None)["absent"] == sorted(n for n, _ in w.HEALTH_STREAMS)


def test_verdict_is_not_computed_before_h_plus_50h15m():
    j = _journal(60)
    early = w.verdict(j, H, _at(50) + 15 * 60 - 1)
    assert early["state"] == "wait" and "per" not in early["window"] and early["window"]["clean"] == 47
    on_time = w.verdict(j, H, _at(50) + 15 * 60)
    assert on_time["state"] == "confirmed" and on_time["window"]["clean"] == 48


def test_verdict_window_extends_until_36_clean_hours():
    restarts = tuple(range(2, 50, 3))  # 16 restart hours: each and the next one out
    j = _journal(80, restarts=restarts)
    v = w.verdict(j, H, _at(50) + 15 * 60)
    assert v["state"] == "wait" and v["window"]["clean"] < 36
    v = w.verdict(j, H, _at(80))
    assert v["state"] == "confirmed" and v["window"]["clean"] == 36 and v["window"]["end"] > H + 50 * HR


def test_verdict_refuted_between_partial_and_invalid():
    assert w.verdict(_journal(60, fail={"bybit": 2, "okx": 2}), H, _at(51))["state"] == "refuted"
    between = _journal(200, fail={"bybit": 1, "okx": 0})
    assert w.verdict(between, H, _at(51))["state"] == "extend to 168 h"
    assert w.verdict(between, H, _at(171))["state"] == "partial effect"
    inv = _journal(120)
    for k in range(10, 20):
        inv["binance"][H + k * HR] = (OK_S, 1)
    v = w.verdict(inv, H, _at(51))
    assert v["state"] == "wait" and v["invalid"][0]["new_start"] == H + 21 * HR
    assert w.verdict(inv, H, _at(21 + 48) + 15 * 60)["state"] == "confirmed"


def _db(path: Path, journal: dict) -> None:
    con = sqlite3.connect(path)
    for stmts in MIGRATIONS.values():
        for stmt in stmts:
            if "CREATE TABLE" in stmt and ("liquidation_stream_hour" in stmt or "book_stream_hour" in stmt):
                con.execute(stmt)
    for s, hours in journal.items():
        table, col = ("book_stream_hour", "stream") if ":" in s else ("liquidation_stream_hour", "exchange")
        con.executemany(
            f"INSERT INTO {table} ({col}, hour_ts, connected_s, failures) VALUES (?, ?, ?, ?)",
            [(s, h, c, f) for h, (c, f) in hours.items()],
        )
    con.commit()
    con.close()


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat()


def test_script_prints_only_rollback_checks_before_the_verdict_time(tmp_path, capsys):
    db = tmp_path / "t.sqlite"
    _db(db, _journal(30))
    out = tmp_path / "o" / "r.md"
    assert script.main(["--db", str(db), "--h", _iso(H), "--now", _iso(_at(20)), "--no-health", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert out.read_text() == text and json.loads(out.with_suffix(".json").read_text())["rollback"] is False
    assert "ROLLBACK: not triggered" in text and "PENDING" not in text
    assert "not yet:" in text and "VERDICT" not in text and "(descriptive)" not in text


def test_script_verdict_after_the_registered_time(tmp_path, capsys):
    db = tmp_path / "t.sqlite"
    _db(db, _journal(60, fail={"bybit": 2, "okx": 2}))
    script.main(["--db", str(db), "--h", _iso(H), "--now", _iso(_at(51)), "--no-health"])
    text = capsys.readouterr().out
    assert "ROLLBACK: not triggered" in text  # (b): 2/h is under the limits
    assert "VERDICT: refuted" in text and "coinbase:book (descriptive)" in text


def test_script_issues_no_verdict_after_a_journal_rollback(tmp_path, capsys):
    j = _journal(60)
    j["okx"][H + 51 * HR] = (0.5 * OK_S, 0)  # one clean hour after the verdict time with a low ratio: rollback (a)
    db = tmp_path / "t.sqlite"
    _db(db, j)
    out = tmp_path / "r.md"
    script.main(["--db", str(db), "--h", _iso(H), "--now", _iso(_at(53)), "--no-health", "--out", str(out)])
    text = capsys.readouterr().out
    summary = json.loads(out.with_suffix(".json").read_text())
    assert "ROLLBACK: TRIGGERED ((a) journal)" in text and "VERDICT: not issued" in text
    assert "VERDICT: confirmed" not in text and "clean hours:" not in text and "(descriptive)" not in text
    assert summary["verdict_state"] == "rollback" and "verdict" not in summary


def _health(consecutive: int, store: str = "/db") -> dict:
    s = {"consecutive_failures": consecutive, "peer": "18.245.199.83"}
    return {
        "store": {"path": store},
        "collector": [
            {"name": "liquidations:bybit", "stream": dict(s)},
            {"name": "liquidations:okx", "stream": {"consecutive_failures": 0, "peer": "172.64.144.82"}},
            {"name": "book_walls:coinbase", "stream": {"book": {"consecutive_failures": 0, "peer": "104.18.35.15"}}},
        ],
    }


def test_render_withholds_the_verdict_on_a_health_only_or_b_only_rollback():
    j = _journal(60)
    text, summary = script.render(j, H, _at(53), (_health(5), "test"))
    assert "ROLLBACK: TRIGGERED ((a) system_health)" in text and "VERDICT: not issued" in text
    assert summary["verdict_state"] == "rollback"
    text, _ = script.render(j, H, _at(53), (_health(0), "test"))
    assert "ROLLBACK: not triggered\n" in text and "VERDICT: confirmed\n" in text
    jb = _journal(60, fail={"bybit": 4})  # 4/h > 3.9 in the first 12 clean hours: rollback (b)
    text, summary = script.render(jb, H, _at(53), (_health(0), "test"))
    assert "ROLLBACK: TRIGGERED ((b))" in text and "VERDICT: not issued" in text and summary["rollback_parts"] == ["(b)"]


def test_unread_or_foreign_health_is_reported_as_not_checked(tmp_path):
    j = _journal(60)
    text, summary = script.render(j, H, _at(53), (None, "no answer"))
    assert "ROLLBACK: not triggered (system_health not read: (a) health part not checked)" in text
    assert "VERDICT: confirmed (system_health not read" in text and summary["health_checked"] is False
    db = str(tmp_path / "t.sqlite")
    assert script.check_store((_health(0, store=db), "ok"), db)[0] is not None
    other = script.check_store((_health(5, store="/other/clone.sqlite"), "ok"), db)
    assert other[0] is None and "other store" in other[1]
    assert script.check_store((_health(0, store=None), "ok"), db)[0] is None


def test_script_opens_the_db_read_only(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        script.load_journal(str(tmp_path / "missing.sqlite"), H)
    assert not (tmp_path / "missing.sqlite").exists()


def test_now_never_moves_past_the_clock():
    assert script.resolve_now(None, 100.0) == 100.0
    assert script.resolve_now("2100-01-01T00:00:00", 1_800_000_000.0) == 1_800_000_000.0
    assert script.resolve_now("2026-11-25T06:00:00", 1_800_000_000.0) == 1795586400.0
