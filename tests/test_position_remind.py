from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import position_remind as pr  # noqa: E402

NOW = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
CALL, PUT = "BTC-13NOV26-83000-C-USDT", "BTC-13NOV26-78000-P-USDT"


def _state(*legs: tuple[str, str, str, str]) -> dict:
    """A positions file as scripts/bybit_options_positions.py writes it (made-up numbers)."""
    pos = [
        {"symbol": s, "side": "Buy", "size": "0.1", "avgPrice": a, "markPrice": m, "unrealisedPnl": u, "positionValue": "50",
         "delta": "0.1", "theta": "-9"}
        for s, a, m, u in legs
    ]  # fmt: skip
    tick = {s: {"markPrice": m, "markIv": "0.33", "underlyingPrice": "80500", "indexPrice": "80400"} for s, _, m, _ in legs}
    return {"schema": 1, "status": "ok", "asof_utc": "2026-11-04T06:30:00Z", "positions": pos, "tickers": tick, "errors": []}


def test_message_sums_the_legs_against_the_entry_cost():
    st = _state((CALL, "1520", "700", "-82"), (PUT, "1590", "900", "-69"))
    msg = pr.message(st, NOW, [CALL, PUT], "стренгл 13NOV26", "закрити обидві ноги", 314.21)
    assert msg.startswith("**Нагадування: стренгл 13NOV26**\nПлан: закрити обидві ноги")
    assert f"• `{CALL}`: вхід $1 520, марк $700, P&L −$82" in msg and f"• `{PUT}`: вхід $1 590, марк $900, P&L −$69" in msg
    assert "Разом відкриті ноги: −$151 (−48 % від вартості входу $314 з комісіями)" in msg
    assert "До експірації 9.1 дн, форвард $80 500." in msg and "Марки Bybit на 04.11 08:30 за Києвом." in msg


def test_message_names_a_closed_leg_and_an_unusable_file():
    msg = pr.message(_state((CALL, "1520", "2600", "108")), NOW, [CALL, PUT], "t", "p", None)
    assert f"• `{PUT}` — на Bybit уже немає (закрита?)" in msg and "Разом відкриті ноги: +$108\n" in msg
    for bad in (None, {"status": "error", "detail": "x"}, {"status": "ok", "asof_utc": "2026-10-01T00:00:00Z", "positions": []}):
        msg = pr.message(bad, NOW, [CALL], "t", "p", 1.0)
        assert "Позиції з Bybit прочитати не вдалося" in msg and "**Нагадування: t**" in msg
    assert pr.main(["--symbols", CALL, "--title", "t", "--plan", "p", "--positions", "/nonexistent/x.json"]) == 0
