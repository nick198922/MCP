"""Rehearsal of scripts/checkpoint_report.py on the review date 2026-11-30 (item 242 of IDEA-20261102-standing-improvements).

A migrated store with one or two rows in every table ``load_inputs`` reads, the repository's real ``docs/ideas/`` cards,
``main`` run at 2026-11-30 00:00 UTC and at the end of 2026-12-01 with the wall clock moved there. The rules and numbers
are covered by tests/test_checkpoint_report.py; here only that the text of the review itself is whole: exit code 0, no
traceback, no ``nan`` / ``None`` in a cell, no forward without an explanation, every forward card named.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from tradedesk.analytics import checkpoint as cp
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import checkpoint_report as script  # noqa: E402

H = 3_600_000
DAY = 86_400_000
REVIEW = cp._ms(2026, 11, 30)  # 2026-11-30 00:00 UTC


def _s(y: int, m: int, d: int, h: int = 0) -> int:
    return cp._ms(y, m, d, h) // 1000


def _ins(con: sqlite3.Connection, table: str, **row) -> None:
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _demo(con, link: str, setup: str, *, tf: str = "4h", status: str = "closed", opened: int, r: float | None = None, **kw) -> None:
    base = {
        "link_id": link, "venue": "bybit-demo", "symbol": f"{setup}/USDT:USDT", "side": "long", "setup": setup, "timeframe": tf,
        "signal_ts": opened - 4 * H, "entry": 100.0, "stop": 95.0, "target": 110.0, "qty": 1.0, "leverage": 5, "risk_usd": 5.2,
        "comment": "c", "status": status,
    }  # fmt: skip
    if status in ("open", "closed"):
        base |= {"fill": 100.0, "opened_ts": opened}
    if status == "closed":  # consistent P&L: (99 - 100) * 1 - 0.1
        base |= {"exit": 99.0, "closed_ts": opened + 2 * H, "pnl_usd": -1.1, "fees_usd": 0.1, "r_multiple": r, "exit_reason": "stop"}
    _ins(con, "demo_trade", **(base | kw))


def _leg(con, day: str, role: str, opt: str, strike: float, *, ts: int, expiry: int) -> None:
    _ins(
        con, "option_leg_snapshot", day=day, currency="BTC", role=role, ts=ts, expiry_ts=expiry, tau_days=(expiry - ts) / 86400,
        spot=100_000.0, forward=100_050.0, opt=opt, instrument=f"BTC-{day}-{int(strike)}-{opt}", target=strike, strike=strike,
        dist=0.0, floor=0 if role in ("lp", "lc") else None, src="ticker", bid=0.0040, ask=0.0045, mark=0.00425, mark_iv=0.45,
        bid_iv=0.44, ask_iv=0.46, bid_size=5.0, ask_size=5.0, delta=0.1, quote_ts_ms=ts * 1000, open_interest=100.0, volume=10.0,
    )  # fmt: skip


def _seed(tmp_path: Path) -> str:
    db = str(tmp_path / "rehearsal.sqlite")

    async def go() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    asyncio.run(go())
    con = sqlite3.connect(db)
    # demo: closed S3 / S12 / S17, a missed S28 limit, an S40 still open
    _demo(con, "s3-1", "S3", opened=cp._ms(2026, 11, 10, 4), r=-0.2)
    _demo(con, "s3-2", "S3", tf="1d", opened=cp._ms(2026, 11, 18), r=0.4)
    _demo(con, "s12-1", "S12", opened=cp._ms(2026, 11, 12, 8), r=0.3, version="v9")
    _demo(con, "s17-1", "S17", tf="1d", opened=cp._ms(2026, 11, 14), r=-0.1, rest_until=cp._ms(2026, 11, 15))
    _demo(con, "s28-1", "S28", status="missed", opened=cp._ms(2026, 11, 16), rest_until=cp._ms(2026, 11, 17))
    _demo(con, "s40-1", "S40", status="open", opened=cp._ms(2026, 11, 20))
    # carry A and B: a closed and an open position, their journal and cycles
    t = cp._ms(2026, 11, 8)
    for strat, tid, closed in (("A", "A:X", t + 10 * DAY), ("A", "A:Y", None), ("B", "B:Z", t + 6 * DAY)):
        state = {"entry_apr": 0.3, "spot_e": 1.0, "fees": 0.003, "funding": 0.002}
        trade = None
        if closed:
            trade = {"open_ts": t, "close_ts": closed, "entry_apr": 0.3, "funding": 0.002, "pnl_usd": 4.0, "max_runup": 0.1}
            trade |= {"entry_spread": 0.01} if strat == "B" else {}
        _ins(
            con, "cp_position", trade_id=tid, strategy=strat, symbol=f"{tid[2:]}/USDT:USDT", status="closed" if closed else "open",
            opened_ts=t, closed_ts=closed, slot_notional=2000.0, state=json.dumps(state), trade=json.dumps(trade) if trade else None,
            exit_reason="signal" if closed else None,
        )  # fmt: skip
    logs = (
        ("A", "side", "A:X", {"actual_cost": 0.002, "model_cost": 0.0015, "spread_bps": 3.0}),
        ("A", "funding", "A:Y", {"payment_ts": t + 8 * H}),
        ("A", "mark", "A:Y", {"pnl_usd": 1.0, "spot": 1.0, "funding_usd": 1.0, "max_runup": 0.1}),
        ("A", "close", "A:X", {"reason": "signal"}),
        ("B", "event", "B:Z", {"type": "funding_gap_confirmed", "symbol": "Z"}),
    )
    for strat, kind, tid, payload in logs:
        _ins(con, "cp_log", strategy=strat, cycle_ts=t + 8 * H, kind=kind, trade_id=tid, payload=json.dumps(payload))
    for strat in ("A", "B"):
        for k in range(2):
            c = t + k * 8 * H
            payload = json.dumps({"on_time": True, "complete": True})
            _ins(con, "cp_cycle", strategy=strat, cycle_ts=c, status="done", finished_ms=c + 60_000, payload=payload)
    # assistant_paper: one method trade (its P&L column is never read)
    _ins(
        con, "rec_paper", opened_ts=_s(2026, 11, 20, 9), currency="BTC", structure="long_straddle", expiry_ts=_s(2026, 12, 25, 8),
        spot=1, forward=1, t_years=0.1, legs="[]", premium_per_btc=1, entry_fees_per_btc=0, max_loss_per_btc=1,
        risk_basis="max_loss", size_btc=0.1, deposit_usd=1e4, risk_pct=1, risk_usd=100, within_risk=1, status="open",
        source="method", trigger="chain", pnl_usd=12345.678,
    )  # fmt: skip
    # a plan tagged with a real forward card's tag
    _ins(
        con, "trade_plan", created_ts=_s(2026, 11, 25), exchange="bybit", symbol="X/USDT:USDT", instrument="perp", side="short",
        entry_type="market", entry=1, stop=1.1, target=0.9, expires_ts=_s(2026, 11, 26), verdict="enter", tags='["idea:hl-oi-cap"]',
        status="open",
    )  # fmt: skip
    # Polymarket: two snapshots of one market after both T0s, and its outcome row
    end = _s(2026, 11, 22, 16)
    for ts in (_s(2026, 11, 21, 12), _s(2026, 11, 22, 12)):
        _ins(
            con, "pm_gap_snapshot", ts=ts, market_id="m1", asset="BTC", kind="above", direction="above", strike=100_000.0,
            end_ts=end, hours_to_end=(end - ts) / 3600, yes_bid=0.40, yes_ask=0.42, yes_bid_usd=800.0, yes_ask_usd=900.0,
            no_bid=0.57, no_ask=0.59, no_bid_usd=800.0, no_ask_usd=900.0, volume_usd=5e4, fee_rate=0.0, fee_exp=1.0,
            rules_hash="h", rules_standard=1, binance_last=100_100.0, model_prob=0.55, model_note="ok", model_iv=0.45,
            model_expiry="2026-11-23T08:00:00Z", expiry_gap_hours=16.0, model_covered=1, spot=100_050.0, dvol=0.45,
        )  # fmt: skip
    _ins(con, "pm_gap_outcome", market_id="m1", asset="BTC", kind="above", strike=100_000.0, end_ts=end, outcome_yes=1, attempts=1,
         checked_ts=end + 3600, resolved_ts=end + 3600)  # fmt: skip
    # condor: full BTC H1 entries on the Fridays 06.11 and 13.11 (T0 = 06.11), nothing on 20.11 and 27.11
    for day in ("2026-11-06", "2026-11-13"):
        y, m, d = map(int, day.split("-"))
        ts, expiry = _s(y, m, d, 9), _s(y, m, d, 8) + 7 * 86400
        for role, opt, k in (("lp", "P", 85_000.0), ("sp10", "P", 92_000.0), ("sc10", "C", 108_000.0), ("lc", "C", 115_000.0)):
            _leg(con, day, role, opt, k, ts=ts, expiry=expiry)
    con.commit()
    con.close()
    (tmp_path / "assistant").mkdir()
    state = {"date": "2026-11-29", "asof_utc": "2026-11-29T09:05:00Z", "state": "actual", "action": "hold"}
    (tmp_path / "assistant" / "paper_state.json").write_text(json.dumps(state), encoding="utf-8")
    return db


def _run(tmp_path: Path, monkeypatch, capsys, db: str, date: str, wall_ms: int) -> str:
    monkeypatch.setattr(script, "time", type("Clock", (), {"time": staticmethod(lambda: wall_ms / 1000)}))
    out = tmp_path / "rep" / f"checkpoint-{date}.md"
    assert script.main(["--db", db, "--date", date, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert out.read_text(encoding="utf-8") == text
    return text


def test_rehearsal_seeds_every_table_the_report_reads(tmp_path):
    db = _seed(tmp_path)
    con = sqlite3.connect(db)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in script.FORWARD_TABLES}
    con.close()
    assert len(counts) == 9 and all(counts.values()), counts
    assert {(s, len(c["positions"]), len(c["cycles"])) for s, c in script.load_inputs(db, REVIEW)["carry"].items()} == {
        ("A", 2, 2),
        ("B", 1, 2),
    }


@pytest.mark.parametrize(
    ("date", "wall_ms", "stamp"),
    [
        ("2026-11-30", REVIEW, "2026-11-30 00:00"),  # the wall clock at 00:00 caps the end of the day
        ("2026-12-01", cp._ms(2026, 12, 3), "2026-12-01 23:59"),  # the end of the UTC day
    ],
)
def test_report_on_the_review_date_is_whole(tmp_path, monkeypatch, capsys, date, wall_ms, stamp):
    db = _seed(tmp_path)
    before = Path(db).read_bytes()
    text = _run(tmp_path, monkeypatch, capsys, db, date, wall_ms)
    assert Path(db).read_bytes() == before  # read-only
    assert f"стан форвардів на {stamp} UTC" in text
    assert "Traceback" not in text and not re.search(r"\bnan\b", text, re.IGNORECASE) and "None" not in text
    assert "запит не вдався" not in text and "таблиці немає" not in text and "не відкрито" not in text
    lines = text.splitlines()
    rows = [ln for ln in lines if ln.startswith("| ") and not ln.startswith(("| Форвард", "|---"))]
    report = cp.build(script.load_inputs(db, script.at_date(date, wall_ms)), script.at_date(date, wall_ms))
    assert len(rows) == len(report["forwards"]) >= 20
    for ln in rows:  # name | n | metric | band | verdict: every cell filled, a number or a dash
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert len(cells) == 5 and all(cells), ln
        assert re.fullmatch(r"\d+|—", cells[1]), ln
    # every forward of the table is explained below it: a header with at least one line, never an empty section
    detail = lines.index("## Подробиці")
    headers = [i for i, ln in enumerate(lines) if i > detail and ln.startswith("**")]
    for i in headers:
        assert lines[i + 1].startswith("- "), lines[i]
    named = {lines[i][2:].split("**", 1)[0] for i in headers}
    assert named == {r["name"] for r in report["forwards"]}
    # every forward card of the repository (the loader itself: test_script_lists_every_forward_card_of_the_repo)
    cards = script.load_idea_cards()
    assert cards and all(f"- {c['slug']}: планів" in text for c in cards)
    assert "- hl-oi-cap-state-forward: планів 1, «входити» 1, оцінено 0" in text
    # date-dependent branches: condor interim after T0, embargoes closed, assistant_paper state from the day before
    assert "- п'ятниць з 8, що минули: 4" in text and "- валідних входів BTC: 2" in text
    assert "рано: рішення після 2026-12-31 10:00 UTC" in text
    assert text.count("| закрито |") == 3  # Polymarket v1, v2-g, condor P&L
    assert "12345" not in text  # no P&L of assistant_paper
    assert "завислі демо-угоди (open довше за строк утримання + 1 доба): 1 — S40" in text
    # the count form agrees with the number (item 243: it used to print «1 угод»)
    assert "проксі примусового закриття: 1 угода за" in text and "1 угод " not in text
