"""scripts/dashboard.py, the «Демо» tab: the demo trader's book read from a temporary SQLite store (no network)."""

import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard as db  # noqa: E402

T0 = 1_792_700_000_000  # unix ms, 22.10.2026
DAY = 86_400_000
ICON = "<span class='info' tabindex='0' role='button'"
XSS = "<img src=x onerror=alert(1)>"


def _store(path: Path, upto: int | None = None) -> Path:
    """A store built from the real migrations (``upto`` limits them: 18 = before demo_trade / exec_audit)."""
    from tradedesk.store.schema import MIGRATIONS

    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if upto is None or v <= upto:
            for sql in MIGRATIONS[v]:
                con.execute(sql)
    con.commit()
    con.close()
    return path


def _trade(i: int, setup: str, status: str, r: float | None = None, exit_reason: str | None = None, symbol: str = "SOL/USDT:USDT"):
    closed = T0 + i * 3_600_000 + DAY if status == "closed" else None
    pnl = None if r is None else 100.0 * r
    return (
        f"td-{i}", "bybit-demo", symbol, "long" if i % 2 else "short", setup, "4h", T0 + i * 3_600_000, 100.0, 95.0, 110.0,
        1.0, 5, 100.0, "why", status, T0 + i * 3_600_000, closed, pnl, r, exit_reason,
    )  # fmt: skip


T_COLS = (
    "link_id, venue, symbol, side, setup, timeframe, signal_ts, entry, stop, target, qty, leverage, risk_usd, comment,"
    " status, opened_ts, closed_ts, pnl_usd, r_multiple, exit_reason"
)
A_COLS = "ts, venue, kind, link_id, symbol, setup, payload"

S3_R = [1.9, -1.05, 1.9, -1.05, 0.02, 1.9, -1.05, -1.05, 1.9, 0.02, -1.05, 1.9]  # 12 closed S3 trades


def _sample_trades() -> list[tuple]:
    exits = {1.9: "target", -1.05: "stop", 0.02: "breakeven"}
    rows = [_trade(i, "S3", "closed", r, exits[r]) for i, r in enumerate(S3_R)]
    rows += [
        _trade(20, "S16", "closed", -1.23, "stop"),
        _trade(21, "S3", "open"),
        _trade(22, "S14", "pending"),
        _trade(23, "S14", "missed"),
        _trade(24, "S16", "rejected"),
        _trade(25, "S3", "closed", 0.5, "time", symbol=f"{XSS}/USDT:USDT"),
    ]
    return rows


def _audit(ts: int, kind: str, violations, setup: str = "S3") -> tuple:
    payload = json.dumps({"violations": violations, "score": 1}) if violations is not None else "not json"
    return (ts, "bybit-demo", kind, f"l-{ts}", "SOL/USDT:USDT", setup, payload)


def _sample_audit(now_ms: int) -> list[tuple]:
    return [
        _audit(now_ms - 1 * DAY, "skipped", ["score 1 < 2"]),
        _audit(now_ms - 2 * DAY, "skipped", ["score 0 < 2"]),
        _audit(now_ms - 20 * DAY, "skipped", ["score 1 < 2"]),
        _audit(now_ms - 3 * DAY, "refused", ["reward/risk 1.38 after fees < 1.5"]),
        _audit(now_ms - 4 * DAY, "refused", ["reward/risk 0.20 after fees < 1.5", "stop 13.2% from the entry > 10%"]),
        _audit(now_ms - 5 * DAY, "refused", ["already in TIA/USDT:USDT"]),
        _audit(now_ms - 6 * DAY, "refused", [f"<script>alert(1)</script> {XSS}/USDT:USDT 12.5"]),
        _audit(now_ms - 7 * DAY - 1, "skipped", None),  # an unreadable payload
        _audit(now_ms - 1, "sent", None),  # not a refusal: not read
        _audit(now_ms - 1, "dry_refused", ["score 0 < 2"]),  # a dry run: not read
    ]


def _filled(tmp_path: Path, now_ms: int) -> Path:
    p = _store(tmp_path / "demo.sqlite")
    con = sqlite3.connect(p)
    con.executemany(f"INSERT INTO demo_trade ({T_COLS}) VALUES ({','.join('?' * 20)})", _sample_trades())
    con.executemany(f"INSERT INTO exec_audit ({A_COLS}) VALUES ({','.join('?' * 7)})", _sample_audit(now_ms))
    con.commit()
    con.close()
    return p


NOW = T0 + 30 * DAY


def test_demo_tab_happy_path_tiles_setups_refusals(tmp_path):
    data, err = db.load_demo(_filled(tmp_path, NOW))
    assert err is None and len(data["trades"]) == 18 and len(data["audit"]) == 8  # 'sent' and 'dry_*' are not read
    page = db.render_demo(data, err, NOW / 1000, vs_bt_on=False)
    assert "Блок не вдалося показати" not in page and "Демо не вдалося показати" not in page
    for title in ("Демо-угоди", "R за сетапом", "Останні демо-угоди", "Відмови guard за причинами", "Демо проти бектесту"):
        assert f"<h2>{title}{ICON}" in page, title
    # every heading and tile has its help note (HELP key exists)
    for h in re.findall(r"<h2>(.*?)</h2>", page, re.S):
        assert ICON in h, h[:80]
    tiles = re.findall(r"<div class='tile[^']*'><span class='k'>(.*?)<span class='v'>", page, re.S)
    assert len(tiles) >= 10 and all(ICON in k for k in tiles)
    # counts: 14 closed (12 S3 + S16 + the XSS row), open 1 + pending 1, missed 1 + rejected 1
    assert "<span class='v'>14</span>" in page and "з них чекають входу 1" in page
    assert "вхід не заповнився 1 · біржа відхилила 1" in page
    assert "ціль 5 · стоп 6 · беззбиток 2 · за часом 1" in page
    # every demo setup gets a tile, all under 30 trades: «замало для висновку»
    for name, n in (("S14", 0), ("S16", 1), ("S3", 13)):
        assert f"{name} · середній R" in page and f"замало для висновку ({n} з 30)" in page, name
    assert "Відмов guard" in page and "Не відібрано" in page
    assert "бал сигналу нижче 2" in page and "R:R після комісій нижче 1.5" in page and "стоп далі 10% від входу" in page
    assert "уже є позиція в цій монеті" in page and "причину не записано" in page
    assert "тижневе повідомлення поки вимкнене" in page and "scripts/demo_vs_backtest.py" in page
    assert "Закритих демо-угод для звірки: 14" in page


def test_demo_setup_stats_match_an_independent_t_interval():
    from tradedesk.analytics.smart_money import t975

    trades = [dict(zip(T_COLS.replace(" ", "").split(","), t, strict=True)) for t in _sample_trades()]
    s = db.demo_setup_stats(trades)
    assert list(s)[:3] == ["S14", "S16", "S3"]
    rs = [*S3_R, 0.5]
    h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
    m = sum(rs) / len(rs)
    assert s["S3"]["closed"] == 13 and s["S3"]["n"] == 13
    assert s["S3"]["mean_r"] == pytest.approx(m)
    assert s["S3"]["ci95"] == pytest.approx([m - h, m + h])
    assert s["S3"]["win_rate"] == pytest.approx(8 / 13)
    assert s["S3"]["pnl"] == pytest.approx(100.0 * sum(rs))
    assert s["S16"]["n"] == 1 and s["S16"]["ci95"] is None
    assert (s["S14"]["closed"], s["S14"]["n"], s["S14"]["mean_r"]) == (0, 0, None)
    assert db._demo_vs0(s["S3"]["ci95"], 13) == ("95 % ДІ містить 0: переваги не видно", "")
    assert db._demo_vs0([0.1, 0.4], 40)[1] == "ok" and db._demo_vs0([-0.4, -0.1], 40)[1] == "bad"


def test_enough_trades_drop_the_small_sample_badge():
    rs = [1.0 if i % 2 else 0.5 for i in range(30)]
    trades = [{"setup": "S14", "status": "closed", "r_multiple": r, "pnl_usd": 100 * r} for r in rs]
    html = db.block_demo_setups(trades)
    assert "замало для висновку (0 з 30)" in html  # S16 and S3 have none
    s14 = html.split("S14 · середній R")[1].split("</div>")[0]
    assert "<span class='sig" not in s14 and "95 % ДІ вище 0" in s14  # no «замало для висновку» badge
    assert html.count("<span class='sig warn'>замало для висновку") == 2


def test_demo_refusals_group_by_rule_and_count_the_week():
    now = NOW
    rows = [dict(zip(A_COLS.replace(" ", "").split(","), a, strict=True)) for a in _sample_audit(now)]
    rows = [r for r in rows if r["kind"] in ("refused", "skipped")]
    g = {x["reason"]: x for x in db.demo_refusals(rows, now)}
    assert g["бал сигналу нижче 2"]["skipped"] == 3 and g["бал сигналу нижче 2"]["week"] == 2
    assert g["R:R після комісій нижче 1.5"]["refused"] == 2
    assert g["стоп далі 10% від входу"]["refused"] == 1
    assert g["причину не записано"]["skipped"] == 1 and g["причину не записано"]["week"] == 0
    assert db.demo_refusals(rows, now)[0]["reason"] == "бал сигналу нижче 2"  # the most frequent first


@pytest.mark.parametrize(
    ("msg", "label"),
    [  # messages as execution/guard.py and demo_trader write them (live exec_audit, 24.10)
        ("score 1 < 2", "бал сигналу нижче 2"),
        ("reward/risk 1.38 after fees < 1.5", "R:R після комісій нижче 1.5"),
        ("stop 161.0% from the entry > 10%", "стоп далі 10% від входу"),
        ("already in TIA/USDT:USDT", "уже є позиція в цій монеті"),
        ("levels: stop and target must be on opposite sides of the entry", "стоп і ціль не по різні боки входу"),
        ("FOO/USDT:USDT is not in today's universe", "монети немає в сьогоднішньому списку"),
        ("lower rank", "обрано сильніший сигнал"),
        ("3 trades at risk today = max", "денний ліміт угод під ризиком"),
        (
            "maintenance + loss at all stops 4,100 USD > 40% of equity (cross margin)",
            "крос-маржа: підтримка + збиток на стопах понад ліміт",
        ),
        ("margin 2,500 USD > 50% of equity", "застава понад частку капіталу"),
        ("notional 30,000 USD > 25,000", "розмір позиції понад ліміт"),
        ("risk 101.00 USD > 100", "ризик угоди понад ліміт"),
        ("", "причину не записано"),
        (None, "причину не записано"),
        ("network timeout on BTC/USDT:USDT after 12.5 s", "network timeout on <символ> after N s"),
    ],
)
def test_demo_reason_labels(msg, label):
    assert db.demo_reason(msg) == label


def test_untrusted_symbols_and_reasons_are_escaped(tmp_path):
    page = db.render_demo(*db.load_demo(_filled(tmp_path, NOW)), NOW / 1000)
    assert XSS not in page and "<script>alert" not in page
    assert "<td>&lt;img src=x onerror=alert</td>" in page  # the symbol cell: cut to 24 characters and escaped
    assert "&lt;script&gt;alert(N)&lt;/script&gt;" in page  # the unknown reason: numbers masked, escaped


def test_empty_store_old_store_and_missing_file_explain_instead_of_failing(tmp_path):
    empty = db.render_demo(*db.load_demo(_store(tmp_path / "e.sqlite")), NOW / 1000)
    assert "Демо-угод ще немає" in empty and "Відмов і пропусків ще не було" in empty
    assert "Закритих демо-угод для звірки: 0" in empty and "Блок не вдалося показати" not in empty
    old = db.render_demo(*db.load_demo(_store(tmp_path / "o.sqlite", upto=18)), NOW / 1000)
    assert "Таблиці demo_trade у базі ще немає" in old and "Таблиці exec_audit у базі ще немає" in old
    missing = tmp_path / "none.sqlite"
    data, err = db.load_demo(missing)
    assert (data, err) == (None, "missing") and not missing.exists()  # read-only: nothing created
    assert "Бази ще немає" in db.render_demo(data, err, NOW / 1000, missing)
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database at all, just bytes" * 10)
    data, err = db.load_demo(bad)
    assert data is None and err.startswith("unreadable")
    assert "Базу не вдалося прочитати" in db.render_demo(data, err, NOW / 1000)


def test_an_odd_row_does_not_take_the_page_down():
    data = {"trades": [{"status": "closed", "setup": "S3", "r_multiple": "x", "pnl_usd": object()}], "audit": [{"kind": "refused"}]}
    page = db.render_demo(data, None, NOW / 1000)
    assert page.endswith("</body></html>") and "<h2>Відмови guard за причинами" in page
    assert "Блок не вдалося показати" not in page and "причину не записано" in page  # odd values are skipped
    broken = db.render_demo({"trades": ["not a row"], "audit": None}, None, NOW / 1000)
    assert "Блок не вдалося показати" in broken and broken.endswith("</body></html>")  # one block fails, the page renders


def test_vs_backtest_block_says_where_the_data_is():
    on = db.block_demo_vs_bt([], True)
    assert "тижневе повідомлення увімкнене" in on and "Дані — тижневе повідомлення в Telegram" in on
    assert "не вдалося прочитати" in db.block_demo_vs_bt([], None)
    assert "29-demo-vs-backtest-weekly-timer.sh" in db.block_demo_vs_bt([], False)


def test_demo_page_reads_the_store_and_is_in_the_shared_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEMO_DB", _filled(tmp_path, NOW))
    flag = tmp_path / "demo-vs-backtest-telegram.on"
    flag.write_text("")
    monkeypatch.setattr(db, "DEMO_VS_BT_FLAG", flag)
    page = db.demo_page(now=NOW / 1000)
    assert "querySelectorAll('.info')" in page and page.endswith("</body></html>")
    assert "href='/demo' aria-current=page" in page and "тижневе повідомлення увімкнене" in page
    assert "href='/demo'" in db.render_polymarket(None, "missing", NOW / 1000)


def test_constants_follow_the_demo_trader_and_the_reconciliation():
    from tradedesk.analytics.demo_vs_backtest import MIN_TRADES
    from tradedesk.execution.demo_trader import SETUPS
    from tradedesk.execution.orders import ENTRY_WAIT_MS

    assert db.DEMO_SETUPS == SETUPS and db.DEMO_MIN_N == MIN_TRADES
    assert db.DEMO_ENTRY_WAIT_MS == ENTRY_WAIT_MS
    assert f"до {ENTRY_WAIT_MS // 60_000} хв" in db.HELP["demo_open"][2] and "2 год" not in db.HELP["demo_open"][2]


def test_option_legs_are_flagged_as_left_out_with_their_count(tmp_path):
    p = _filled(tmp_path, NOW)
    page = db.render_demo(*db.load_demo(p), NOW / 1000)
    assert "опціонні ноги демо-трейдера (demo_option_leg) не враховано" in page and "Опціонних ніг у базі ще немає" in page
    con = sqlite3.connect(p)
    for i, status in enumerate(("closed", "closed", "open")):
        con.execute(
            "INSERT INTO demo_option_leg (link_id, pair_id, venue, symbol, underlying, side, is_call, strike, expiry_ms, tick,"
            " lot_step, min_qty, setup, timeframe, signal_ts, target, comment, status, placed_ts, qty, risk_usd)"
            " VALUES (?, 'td-1', 'bybit-demo', 'BTC-13NOV26-84000-C-USDT', 'BTC/USDT:USDT', 'long', 1, 84000, ?, 5, 0.01,"
            " 0.01, 'S3', '4h', ?, 90000, 'paired test', ?, ?, 0.01, 100)",
            (f"tdo-{i}", T0 + 20 * DAY, T0, status, T0),
        )
    con.commit()
    con.close()
    data, err = db.load_demo(p)
    assert data["option_legs"] == {"closed": 2, "open": 1}
    page = db.render_demo(data, err, NOW / 1000)
    assert "У базі опціонних ніг: 3 (закрито 2)" in page
    assert "<span class='v'>14</span>" in page  # the futures count is unchanged: legs are not mixed in
    old = db.render_demo(*db.load_demo(_store(tmp_path / "o.sqlite", upto=27)), NOW / 1000)
    assert "Таблиці demo_option_leg у базі ще немає" in old
