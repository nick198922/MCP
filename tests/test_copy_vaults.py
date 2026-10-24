"""The paper copy of HL vaults in scripts/copy_vaults.py (fake APIs, in-memory SQLite, no network)."""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import copy_vaults as cv  # noqa: E402

from tradedesk.execution.guard import TAKER_FEE  # noqa: E402


def state(av, **sizes):
    return {
        "marginSummary": {"accountValue": str(av)},
        "assetPositions": [{"position": {"coin": c, "szi": str(q)}} for c, q in sizes.items()],
    }


def db():
    con = sqlite3.connect(":memory:")
    con.executescript(cv.SCHEMA)
    return con


QUOTES = {"BTCUSDT": (99.0, 101.0), "ETHUSDT": (9.9, 10.1)}


def no_funding(sym):
    return []


def test_fee_matches_the_demo_traders_guard():
    assert cv.TAKER_FEE == TAKER_FEE


def test_symbols_and_vault_book():
    assert cv.bybit_symbol("BTC") == "BTCUSDT" and cv.bybit_symbol("kPEPE") == "1000PEPEUSDT"
    av, sizes = cv.vault_book(state(1000, BTC=-2, kPEPE=5, ETH=0))
    assert av == 1000 and sizes == {"BTCUSDT": -2.0, "1000PEPEUSDT": 5.0}


def test_plan_trades_out_of_the_band_and_always_closes():
    mid = {"BTCUSDT": 100.0, "ETHUSDT": 10.0}
    assert cv.plan({}, {"BTCUSDT": 1.0}, mid, 10_000) == []  # $100 < 2 % of $10k
    assert cv.plan({}, {"BTCUSDT": 3.0}, mid, 10_000) == [("BTCUSDT", 3.0, "open")]
    assert cv.plan({"ETHUSDT": 0.5}, {}, mid, 10_000) == [("ETHUSDT", -0.5, "close")]
    assert cv.plan({"XUSDT": 1.0}, {}, mid, 10_000) == []  # no quote: nothing to trade at


def test_step_mirrors_the_vault_scaled_to_the_book_at_bid_ask_with_fees():
    con = db()
    # vault $100k: short 100 BTC ($10k = 0.1 of it), long 2 000 ETH ($20k): the book copies 0.1x and 0.2x of $10k
    lines = cv.step(con, "V", state(100_000, BTC=-100, ETH=2000), QUOTES, no_funding, now=1)
    assert len(lines) == 2
    trades = dict((s, (q, px, fee)) for s, q, px, fee in con.execute("SELECT sym, dqty, px, fee FROM trade"))
    assert trades["BTCUSDT"][:2] == (-10.0, 99.0)  # a sell fills at the bid
    assert trades["ETHUSDT"][:2] == (200.0, 10.1)  # a buy at the ask
    cash = con.execute("SELECT cash FROM book").fetchone()[0]
    fees = 10 * 99 * TAKER_FEE + 200 * 10.1 * TAKER_FEE
    assert cash == pytest.approx(10_000 + 990 - 2020 - fees)
    eq = con.execute("SELECT equity FROM mark").fetchone()[0]
    assert eq == pytest.approx(10_000 - 10 * 1 - 200 * 0.1 - fees)  # half the spread on each side, and the fees
    # the vault closes ETH: the copy closes it whatever the band
    lines = cv.step(con, "V", state(100_000, BTC=-100), QUOTES, no_funding, now=2)
    assert lines == ["ETHUSDT -200 @ 9.9 (close)"]
    assert dict(con.execute("SELECT sym, qty FROM pos")) == {"BTCUSDT": -10.0}


def test_withdrawals_cancel_out_in_the_target():
    con = db()
    cv.step(con, "V", state(100_000, BTC=-100), QUOTES, no_funding, now=1)
    # followers withdrew half: the vault's size and account value halve together, nothing to trade
    assert cv.step(con, "V", state(50_000, BTC=-50), QUOTES, no_funding, now=2) == []


def test_funding_is_charged_once_per_settlement_with_its_sign():
    con = db()
    cv.step(con, "V", state(100_000, ETH=2000), QUOTES, no_funding, now=1000)
    rows = [{"fundingRateTimestamp": "500", "fundingRate": "0.001"}, {"fundingRateTimestamp": "2000", "fundingRate": "0.001"}]
    cv.step(con, "V", state(100_000, ETH=2000), QUOTES, lambda s: rows, now=3000)
    cv.step(con, "V", state(100_000, ETH=2000), QUOTES, lambda s: rows, now=4000)
    paid = con.execute("SELECT ts, amount FROM funding").fetchall()
    assert paid == [(2000, pytest.approx(-0.001 * 200 * 10.0))]  # a long pays a positive rate, once


def test_a_held_coin_without_a_quote_skips_the_run_and_keeps_the_book():
    con = db()
    with con:
        cv.step(con, "V", state(100_000, BTC=-100), QUOTES, no_funding, now=1)
    before = con.execute("SELECT cash FROM book").fetchone()
    with pytest.raises(RuntimeError, match="no Bybit quote"):
        with con:
            cv.step(con, "V", state(100_000, BTC=-100, ETH=2000), {"ETHUSDT": (9.9, 10.1)}, no_funding, now=2)
    assert con.execute("SELECT cash FROM book").fetchone() == before
    assert con.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 1


def test_report_escapes_and_shows_the_gap():
    rows = [
        {"vault": "<V>", "start_ms": 0, "copy_ret": 0.05, "gross": 2.0, "vault_ret": 0.08, "trades": 4, "fees": 12.0, "funding": -3.0},
        {"vault": "W", "error": "ще немає даних"},
    ]
    text = cv.report_text(rows, datetime.fromtimestamp(7 * 86_400, UTC))
    assert "&lt;V&gt;" in text and "7 дн." in text and "різниця -3.00 %" in text and "ще немає даних" in text


def test_run_is_off_without_the_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cv, "FLAG", tmp_path / "copy-vaults.on")
    assert cv.main(["run"]) == 0 and "off" in capsys.readouterr().out
