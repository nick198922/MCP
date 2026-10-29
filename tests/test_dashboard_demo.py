"""scripts/dashboard.py, the «Демо» tab: the demo trader's book read from a temporary SQLite store (no network)."""

import json
import re
import sqlite3
import statistics
import sys
from dataclasses import replace
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
    assert "замало для висновку (0 з 30)" in html  # S16, S3, S12, S38, S39, S40 and S28 have none
    s14 = html.split("S14 · середній R")[1].split("</div>")[0]
    assert "<span class='sig" not in s14 and "95 % ДІ вище 0" in s14  # no «замало для висновку» badge
    assert html.count("<span class='sig warn'>замало для висновку") == 7


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
        ("2 S12 trades at risk today = temporary S12 cap 2 (docs/QUESTIONS.md)", "тимчасовий кап S12: 2 слоти на день"),
        ("2 S38 entries on this 4h bar = S38 cap 2 per bar", "кап S38: 2 входи на 4h-свічку"),
        ("S38 entries on this 4h bar unknown (book not read) = S38 cap 2 per bar", "кап на свічку: журнал угод не прочитано"),
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
    assert "тижневе повідомлення увімкнене" in on and "файла звірки ще немає" in on
    assert "tradedesk-demo-vs-backtest-daily" in on and str(db.DEMO_VS_BT_JSON) in on
    assert "не вдалося прочитати" in db.block_demo_vs_bt([], None)
    assert "29-demo-vs-backtest-weekly-timer.sh" in db.block_demo_vs_bt([], False)
    for err, text in (("corrupt", "пошкоджений"), ("old_schema", "старого формату"), ("too_big", "завеликий")):
        assert text in db.block_demo_vs_bt([], True, None, err)
    assert "не вдалося прочитати (unreadable: PermissionError)" in db.block_demo_vs_bt([], True, None, "unreadable: PermissionError")


# ---------------------------------------------------------------- the reconciliation file (scripts/demo_vs_backtest.py --json)

SHADOW_FIX = json.loads((Path(__file__).parent / "fixtures" / "demo_shadow_live_2026-10-24.json").read_text())
REFUSED_FIX = json.loads((Path(__file__).parent / "fixtures" / "demo_shadow_refused_live_2026-10-24.json").read_text())


def _vs_bt_doc(now_ms: int) -> dict:
    """A file as scripts/demo_vs_backtest.main writes it: the replay summary and the not-executed block of the live
    samples (24.10: skipped and filled, guard refusals), built by the same functions."""
    from tradedesk.analytics import demo_shadow as ds
    from tradedesk.analytics import demo_vs_backtest as dvb

    ne = ds.not_executed(
        SHADOW_FIX["trades"],
        SHADOW_FIX["audit"] + REFUSED_FIX["audit"],
        SHADOW_FIX["plans"] + REFUSED_FIX["plans"],
        now_ms=int(SHADOW_FIX["now_ms"]),
    )
    rows = [
        dvb.Replay("a", "S/USDT:USDT", "S3", "4h", "short", 0, 0, "match", r_demo=2.2, r_demo_bt=2.1, r_bt=1.9, r_diff=0.2, entry_edge_r=0.05),
        dvb.Replay("b", "S/USDT:USDT", "S3", "4h", "long", 0, 0, "mismatch", ("entry",), r_demo=-1.0, r_demo_bt=-1.1, r_bt=-1.0, r_diff=-0.1),
        dvb.Replay("c", "S/USDT:USDT", "S16", "4h", "long", 0, 0, "no_data"),
    ]  # fmt: skip
    s = dvb.summary(rows)
    groups = {"a": ("ge5m", 7.5e6), "b": ("1to5m", 2.5e6)}  # 'c': no turnover, the default 'unknown'
    g = {k: replace(x, turnover_group=groups[k][0], turnover_usd=groups[k][1]) for x in rows for k in groups if x.link_id == k}
    by = dvb.summary_by_turnover([g.get(x.link_id, x) for x in rows])
    return {
        "schema": 2,
        "generated_ms": now_ms,
        "venue": "bybit",
        "notes": [],
        "summary": s,
        "week": s,
        "by_turnover": by,
        "rows": [],
        "not_executed": {**ne, "error": None},
    }


def test_vs_backtest_block_shows_the_file_numbers(tmp_path):
    import demo_vs_backtest as script

    path = tmp_path / "dvb" / "latest.json"
    script.write_json(str(path), _vs_bt_doc(NOW - 3_600_000))
    doc, err = db.load_demo_vs_bt(path)
    assert err is None and doc["schema"] == 2
    page = db.render_demo(*db.load_demo(_filled(tmp_path, NOW)), NOW / 1000, vs_bt_on=True, vs_bt=(doc, err))
    assert "Блок не вдалося показати" not in page
    block = page.split("<h2>Демо проти бектесту")[1]
    assert "Звірено угод" in block and "замало для висновку (2 з 30)" in block and "50 %" in block  # 1 match of 2 decided
    assert "збіг 1 · немає сигналу 0 · розбіжність 1" in block and "не вирішено 1" in block
    assert "Відібрані, але не виконані сигнали проти тіньових планів" in block
    assert "<td>Не відібрано</td><td class='num'>9</td>" in block and "<td class='num'>8 / 1 / 0</td>" in block
    assert "<td>Виконані (для порівняння)</td><td class='num'>3</td>" in block
    assert "бал сигналу нижче 2" in block and "денний ліміт угод під ризиком" in block  # reasons labelled from the examples
    assert "замало для висновку (8 з 30)" in block
    # guard refusals with a shadow plan (44 of 78), by rule; the 34 without a plan by design are only counted
    assert "<td>Відмова guard</td><td class='num'>44</td>" in block and "<td class='num'>27 / 17 / 0</td>" in block
    assert "<td>Відмова guard</td><td>R:R після комісій нижче 1.5</td><td class='num'>36</td><td class='num'>20</td>" in block
    assert "<td>Відмова guard</td><td>уже є позиція в цій монеті</td><td class='num'>8</td>" in block
    assert "замало для висновку (27 з 30)" in block
    assert "помилка рівнів): 34 (за 7 днів 34)" in block and "Відмов guard у файлі звірки ще немає" not in block
    assert "тижневе повідомлення увімкнене" in block and "старіша за 36 год" not in block
    # every new tile and the sub-heading have their help note
    tiles = re.findall(r"<div class='tile[^']*'><span class='k'>(.*?)<span class='v'>", block, re.S)
    assert len(tiles) == 3 and all(ICON in k for k in tiles)
    assert f"тіньових планів{ICON}" in block


def test_vs_backtest_file_stale_corrupt_and_untrusted(tmp_path):
    old = _vs_bt_doc(NOW - 40 * 3_600_000)
    html = db.block_demo_vs_bt([], True, old, None, NOW / 1000)
    assert "старіша за 36 год" in html
    odd = _vs_bt_doc(NOW)
    odd["not_executed"]["groups"]["skipped"]["reasons"][0]["example"] = f"<script>alert(1)</script> {XSS}"
    odd["summary"]["conclusion"] = XSS
    html = db.block_demo_vs_bt([], True, odd, None, NOW / 1000)
    assert XSS not in html and "<script>alert" not in html and "&lt;img" in html
    odd["not_executed"] = {"groups": {}, "error": "query failed: OperationalError"}
    assert "Не вдалося прочитати базу для цього блоку" in db.block_demo_vs_bt([], True, odd, None, NOW / 1000)
    assert "цього блоку немає" in db.block_demo_vs_bt([], True, {**odd, "not_executed": None}, None, NOW / 1000)
    p = tmp_path / "x.json"
    assert db.load_demo_vs_bt(p) == (None, "missing")
    p.write_text("{not json")
    assert db.load_demo_vs_bt(p) == (None, "corrupt")
    p.write_text(json.dumps({"summary": {}, "schema": 1}))
    assert db.load_demo_vs_bt(p) == (None, "old_schema")
    p.write_text(json.dumps([1, 2]))
    assert db.load_demo_vs_bt(p) == (None, "corrupt")
    broken = db.render_demo({"trades": [], "audit": []}, None, NOW / 1000, vs_bt=({"schema": 2, "summary": {"counts": "x"}}, None))
    assert broken.endswith("</body></html>")  # an odd file breaks at most its own block


def test_vs_backtest_file_written_before_the_refusals_still_shows():
    """A schema-2 file of 24.10 morning has no 'refused' group: the rest shows, the block says the refusals come next run."""
    old = _vs_bt_doc(NOW)
    del old["not_executed"]["groups"]["refused"]
    html = db.block_demo_vs_bt([], True, old, None, NOW / 1000)
    assert "<td>Не відібрано</td><td class='num'>9</td>" in html and "<td>Відмова guard</td>" not in html
    assert "Відмов guard у файлі звірки ще немає" in html
    odd = _vs_bt_doc(NOW)
    odd["not_executed"]["groups"]["refused"]["excluded"] = "x"  # an odd value: zero, not an error
    odd["not_executed"]["groups"]["refused"]["reasons"][0]["example"] = XSS
    html = db.block_demo_vs_bt([], True, odd, None, NOW / 1000)
    assert "помилка рівнів): 0 (за 7 днів 0)" in html and XSS not in html


def test_vs_backtest_block_splits_by_turnover_with_the_rule_per_group(tmp_path):
    import demo_vs_backtest as script

    path = tmp_path / "latest.json"
    script.write_json(str(path), _vs_bt_doc(NOW))
    doc, err = db.load_demo_vs_bt(path)
    assert err is None
    page = db.render_demo(*db.load_demo(_filled(tmp_path, NOW)), NOW / 1000, vs_bt_on=True, vs_bt=(doc, err))
    block = page.split("<h2>Демо проти бектесту")[1]
    assert "Блок не вдалося показати" not in page
    assert f"<h3>За оборотом монети в день входу{ICON}" in block
    sub = block.split("За оборотом монети в день входу")[1].split("Відібрані, але не виконані")[0]
    # one row per group, each with its own «замало для висновку» (decided of 30)
    assert "<td>≥ $5M</td><td class='num'>1</td><td class='num'>1</td><td class='num'>1 / 0 / 0</td><td class='num'>0</td>" in sub
    assert "<td>$1–5M</td><td class='num'>1</td><td class='num'>1</td><td class='num'>0 / 0 / 1</td>" in sub
    assert (
        "<td>Оборот невідомий</td><td class='num'>1</td><td class='num'>0</td><td class='num'>0 / 0 / 0</td><td class='num'>1</td>" in sub
    )
    assert "<td>&lt; $1M</td><td class='num'>0</td>" in sub
    assert sub.count("замало для висновку (") == 4 and "замало для висновку (1 з 30)" in sub and "замало для висновку (0 з 30)" in sub
    assert "100 %" in sub and "0 %" in sub and "+0.20 R" in sub and "−0.10 R" in sub
    # the turnover table sits between the totals and the not-executed block
    assert block.index("Звірено угод") < block.index("За оборотом монети") < block.index("Відібрані, але не виконані")


def test_vs_backtest_file_written_before_the_turnover_split_still_shows():
    """A schema-2 file of before 29.10 has no 'by_turnover': the totals show, the block says the split comes next run."""
    old = _vs_bt_doc(NOW)
    del old["by_turnover"]
    html = db.block_demo_vs_bt([], True, old, None, NOW / 1000)
    assert "Звірено угод" in html and "Розбивки за оборотом у файлі звірки ще немає" in html and "<td>≥ $5M</td>" not in html
    assert "<td>Не відібрано</td><td class='num'>9</td>" in html  # the rest of the file is untouched
    odd = _vs_bt_doc(NOW)
    odd["by_turnover"] = {"ge5m": "x", "1to5m": {"counts": "x", "decided": "y", "mean_r_diff": XSS}}
    html = db.block_demo_vs_bt([], True, odd, None, NOW / 1000)
    assert "<td>≥ $5M</td>" not in html and "<td>$1–5M</td><td class='num'>0</td>" in html and XSS not in html


def test_vs_backtest_file_constants_follow_the_script_and_the_unit():
    import demo_vs_backtest as script

    from tradedesk.analytics.demo_shadow import GROUPS
    from tradedesk.analytics.demo_vs_backtest import TURNOVER_GROUPS

    assert db.DEMO_VS_BT_SCHEMA == script.JSON_SCHEMA
    assert tuple(k for k, _ in db.DEMO_NE_GROUPS) == GROUPS
    assert tuple(k for k, _ in db.DEMO_TURNOVER_GROUPS) == TURNOVER_GROUPS
    unit = (Path(__file__).resolve().parent.parent / "deploy" / "user-units" / "tradedesk-demo-vs-backtest-daily.service").read_text()
    assert (
        f"--json {db.DATA_DIR / 'demo-vs-backtest' / 'latest.json'}" in unit
        or "--json /var/lib/tradedesk-mcp/demo-vs-backtest/latest.json" in unit
    )
    assert db.DEMO_VS_BT_JSON.parts[-2:] == ("demo-vs-backtest", "latest.json")


@pytest.mark.parametrize(
    ("msg", "label"),
    [  # why a maker entry was cancelled (execution/orders.py entry_step, demo_trader)
        ("price at the stop before the fill: the idea is invalidated", "ціна дійшла до стопу до заповнення"),
        ("price ran 0.31R from the planned entry (> 0.25R)", "ціна втекла від плану далі 0.25R"),
        ("reward/risk at the market 1.20 < 1.5", "R:R за ринком нижче 1.5"),
        ("the venue has no such order", "біржа не знає ордера"),
        ("not filled in time; the price is still near the plan; no position after the fill", "після заповнення позиції немає"),
    ],
)
def test_missed_entry_reason_labels(msg, label):
    assert db.demo_reason(msg) == label


def test_demo_page_reads_the_store_and_is_in_the_shared_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEMO_DB", _filled(tmp_path, NOW))
    flag = tmp_path / "demo-vs-backtest-telegram.on"
    flag.write_text("")
    monkeypatch.setattr(db, "DEMO_VS_BT_FLAG", flag)
    monkeypatch.setattr(db, "DEMO_VS_BT_JSON", tmp_path / "dvb" / "latest.json")
    page = db.demo_page(now=NOW / 1000)
    assert "querySelectorAll('.info')" in page and page.endswith("</body></html>")
    assert "href='/demo' aria-current=page" in page and "тижневе повідомлення увімкнене" in page
    assert "файла звірки ще немає" in page
    import demo_vs_backtest as script

    script.write_json(str(tmp_path / "dvb" / "latest.json"), _vs_bt_doc(NOW))
    assert "Звірено угод" in db.demo_page(now=NOW / 1000)  # the page reads the file the daily job writes
    assert "href='/demo'" in db.render_polymarket(None, "missing", NOW / 1000)


def test_constants_follow_the_demo_trader_and_the_reconciliation():
    from tradedesk.analytics.demo_vs_backtest import MIN_TRADES
    from tradedesk.execution.demo_trader import SETUPS
    from tradedesk.execution.orders import ENTRY_WAIT_MS

    assert db.DEMO_SETUPS == SETUPS and db.DEMO_MIN_N == MIN_TRADES
    assert db.DEMO_ENTRY_WAIT_MS == ENTRY_WAIT_MS
    assert f"до {ENTRY_WAIT_MS // 60_000} хв" in db.HELP["demo_open"][2] and "2 год" not in db.HELP["demo_open"][2]


# ---------------------------------------------------------------- setup versions (stdlib copy of analytics.setup_versions)

LIVE_TRADES = json.loads((Path(__file__).parent / "fixtures" / "demo_trades_live_2026-10-24.json").read_text())


def _same(mine, ref) -> None:
    """Exact on everything but floats (approx): the copies do the same arithmetic in the same order."""
    if isinstance(ref, dict):
        assert isinstance(mine, dict) and mine.keys() == ref.keys()
        for k in ref:
            _same(mine[k], ref[k])
    elif isinstance(ref, list):
        assert isinstance(mine, list) and len(mine) == len(ref)
        for a, b in zip(mine, ref, strict=True):
            _same(a, b)
    elif isinstance(ref, float):
        assert mine == pytest.approx(ref, abs=1e-9)
    else:
        assert mine == ref


def test_setup_versions_copy_matches_the_analytics_reference():
    from tradedesk.analytics import setup_versions as sv

    assert db.DEMO_EPOCHS == tuple((e.tag, e.from_ms, e.what, e.source, e.setups) for e in sv.EPOCHS)
    now = int(LIVE_TRADES["now_ms"])
    v3 = sv.EPOCHS[2].from_ms
    extra = [  # the boundary, no setup, no open time, no R, an odd value, closed after now
        {"setup": "S14", "status": "closed", "opened_ts": v3, "closed_ts": v3 + 1, "r_multiple": 0.5, "pnl_usd": 50.0},
        {"setup": "S14", "status": "closed", "opened_ts": v3 - 1, "closed_ts": v3 + 1, "r_multiple": -1.0, "pnl_usd": -100.0},
        {"setup": None, "status": "closed", "opened_ts": v3 + 5, "closed_ts": v3 + 9, "r_multiple": 1.5, "pnl_usd": 150.0},
        {"setup": " ", "status": "closed", "opened_ts": None, "closed_ts": v3 + 9, "r_multiple": None, "pnl_usd": 3.0},
        {"setup": "S3", "status": "closed", "opened_ts": v3 + 5, "closed_ts": v3 + 9, "r_multiple": "x", "pnl_usd": float("nan")},
        {"setup": "S3", "status": "closed", "opened_ts": v3 + 5, "closed_ts": now + 1, "r_multiple": 9.0, "pnl_usd": 900.0},
    ]
    trades = LIVE_TRADES["trades"] + extra
    for t_now in (now, sv.EPOCHS[1].from_ms + 1, sv.EPOCHS[0].from_ms - 1):
        _same(db.demo_version_stats(trades, t_now), sv.version_stats(trades, now_ms=t_now))
    for open_ms in (None, v3 - 1, v3, sv.EPOCHS[0].from_ms - 1):
        ref = sv.version_of("S3", open_ms, now_ms=now)
        mine = db.demo_version_of("S3", open_ms, now)
        assert (None if ref is None else ref.tag) == (None if mine is None else mine[0])


def test_setup_versions_block_on_the_live_sample():
    now = LIVE_TRADES["now_ms"] / 1000
    page = db.render_demo({"trades": LIVE_TRADES["trades"], "audit": [], "option_legs": {}}, None, now)
    assert f"<h2>Версії сетапів{ICON}" in page and "Блок не вдалося показати" not in page
    block = page.split("<h2>Версії сетапів")[1].split("<h2>")[0]
    assert "<td>S3</td><td>v1</td>" in block and "<td>S3</td><td>v3</td>" in block and "<td>S16</td><td>v3</td>" in block
    assert "замало для висновку (4 з 30)" in block and "замало для висновку (8 з 30)" in block
    assert "<td class='num'>100 %</td>" in block and "<td class='num'>25 %</td>" in block
    assert "4 закритих угод були відкриті, коли почалася наступна версія" in block
    assert "Без версії" not in block
    for tag in ("v1", "v2", "v3"):  # the epochs table lists every rule change, v2 too (no trades in it)
        assert f"<tr><td>{tag}</td>" in block
    assert "8d82847" in block and "за часом відкриття угоди" in block


def test_setup_versions_block_empty_unversioned_and_escaped(tmp_path):
    empty = db.render_demo(*db.load_demo(_store(tmp_path / "e.sqlite")), NOW / 1000)
    block = empty.split("<h2>Версії сетапів")[1].split("<h2>")[0]
    assert "Закритих демо-угод ще немає: статистика за версіями" in block and "<tr><td>v3</td>" in block
    odd = [
        {"setup": XSS, "status": "closed", "opened_ts": T0, "closed_ts": T0 + 1, "r_multiple": 1.0, "pnl_usd": 100.0},
        {"setup": "S3", "status": "closed", "opened_ts": None, "closed_ts": T0 + 1, "r_multiple": 1.0, "pnl_usd": 100.0},
    ]
    html = db.block_demo_versions(odd, NOW / 1000)
    assert XSS not in html and "&lt;img src=x " in html
    assert "<td>без версії</td>" in html and "Без версії: 1 закритих угод" in html
    no_setup = db.block_demo_versions([{**odd[1], "setup": None, "opened_ts": T0}], NOW / 1000)
    assert "<td>без сетапу</td><td>v3</td>" in no_setup


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
