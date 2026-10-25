"""The pre-trade rules check from the owner's Telegram /check command (scripts/rules_pretrade_tg.py); temp DB, no network."""

import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_trade_tg as jt  # noqa: E402
import rules_pretrade_tg as rp  # noqa: E402

from tradedesk.store.db import Store  # noqa: E402

NOW = datetime(2026, 10, 25, 10, tzinfo=UTC).timestamp()  # Sunday 13:00 Kyiv
H = 3600.0
RULE_RE = re.compile(r"^Правило (\d) · [^:]*: (✓ виконано|✗ порушено|\? невідомо|— не стосується)", re.M)


def statuses(reply):
    return dict(RULE_RE.findall(reply))


def _row(i, symbol, side, opened, closed=None, pnl=None, tags='["hyperliquid", "import", "perp"]', **kw):
    return {
        "id": i,
        "symbol": symbol,
        "side": side,
        "opened_ts": int(opened),
        "closed_ts": None if closed is None else int(closed),
        "status": "open" if closed is None else "closed",
        "pnl": pnl,
        "size": kw.get("size", 1.0),
        "entry": kw.get("entry", 1.0),
        "stop": kw.get("stop"),
        "target": kw.get("target"),
        "thesis": None,
        "tags": tags,
    }


@pytest.fixture
def journal_db(tmp_path):
    path = tmp_path / "tradedesk.sqlite"

    async def make():
        s = Store(path)
        await s.init()
        await s.aclose()

    asyncio.run(make())
    return path


def test_an_option_purchase_in_the_one_to_three_day_zone_is_never_ok_without_the_live_checks(journal_db):
    out = rp.handle("/check BTC call premium 80 expiry 36h target 95000 thesis пробій діапазону", journal_db, NOW)
    lines = out.split("\n")
    assert len(lines) <= 8 and lines[-1].startswith("<i>Статистика дотримання правил, не порада")
    assert "жодних ордерів" in lines[-1] and "нічого не записано" in lines[-1]
    assert statuses(out) == {"1": "✓ виконано", "2": "✓ виконано", "4": "✓ виконано", "5": "— не стосується", "8": "? невідомо"}
    assert lines[0] == "<b>Перевірка перед угодою: ? невідомо: 8</b>"  # unknown is not ok
    assert "BTC кол · купівля опціону · премія $80 · до експірації 1.5 дн. (36 год) · ціль 95 000 · теза є" in out
    assert "разом з цією $80 при межі $300 (відкритих у журналі 0)" in out


@pytest.mark.parametrize(
    ("text", "rule8", "phrase"),
    [
        ("/check BTC put premium 80 expiry 12h", "✗ порушено", "менше 24 год до експірації"),
        ("/check BTC call premium 80 expiry 2d", "✗ порушено", "немає тези й цілі — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 24h thesis x", "✗ порушено", "немає цілі — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 2d target 95000", "✗ порушено", "немає тези — умова (а) не виконана"),
        ("/check BTC call premium 80 expiry 2d target 95000 thesis x", "? невідомо", "теза й ціль є"),
        ("/check BTC call premium 150 expiry 5d", "✗ порушено", "завелика"),
        ("/check BTC call premium 80 expiry 5d", "✓ виконано", "понад 3 доби — лише ліміт премії"),
        ("/check BTC-27OCT26-90000-C premium 80 target 95000 thesis так", "? невідомо", "1–3 доби"),  # 46 h from the symbol
    ],
)
def test_rule_8_zones(journal_db, text, rule8, phrase):
    out = rp.handle(text, journal_db, NOW)
    assert statuses(out)["8"] == rule8 and phrase in out


def test_a_linear_trade_rules_1_and_5():
    out = rp.build_reply(rp.parse_check_command("/check SOL long 180 stop 170 target 200 risk 90", NOW)["check"], NOW, [])
    assert statuses(out) == {"1": "✓ виконано", "2": "✓ виконано", "4": "✓ виконано", "5": "✓ виконано", "8": "— не стосується"}
    assert out.startswith("<b>Перевірка перед угодою: ✓ виконано: 1, 2, 4, 5</b>")
    out = rp.build_reply(rp.parse_check_command("/check BTC long 84000", NOW)["check"], NOW, [])
    assert statuses(out)["1"] == statuses(out)["5"] == "? невідомо" and "додайте stop" in out
    out = rp.build_reply(rp.parse_check_command("/check BTC long 84000 stop 82000 target 85000 size 0.1", NOW)["check"], NOW, [])
    assert statuses(out)["1"] == statuses(out)["5"] == "✗ порушено" and "✗ порушено: 1, 5" in out


def test_rules_2_and_4_come_from_the_journal():
    alts = [_row(i, s, "long", NOW - 5 * H, stop=0.5) for i, s in enumerate(("SOL", "AVAX", "LINK"), 1)]
    check = rp.parse_check_command("/check DOGE long 0.2 stop 0.19 risk 50", NOW)["check"]
    out = rp.build_reply(check, NOW, alts)
    assert statuses(out)["2"] == "✗ порушено" and "лонгів в альтах з цією 4 — новий альт понад межу" in out
    assert "разом з цією $51.5 при межі $300 (відкритих у журналі 3)" in out
    losses = [_row(10 + i, "BTC", "long", NOW - (5 - i) * H, NOW - (4 - i) * H, -20.0) for i in range(2)]
    out = rp.build_reply(rp.parse_check_command("/check BTC call premium 50 expiry 5d", NOW)["check"], NOW, losses)
    assert statuses(out)["4"] == "✗ порушено" and "сьогодні спрацював о 07:00 UTC (2 збиткові поспіль)" in out
    # paper and airdrop rows are not the owner's trades
    paper = [{**r, "tags": '["carry-paper"]'} for r in alts]
    assert statuses(rp.build_reply(check, NOW, paper))["2"] == "✓ виконано"


def test_an_unreadable_journal_makes_rules_2_and_4_unknown(tmp_path):
    out = rp.handle("/check SOL long 180 stop 170 target 200 risk 90", tmp_path / "missing.sqlite", NOW)
    assert statuses(out)["2"] == statuses(out)["4"] == "? невідомо" and out.count("журнал не прочитано") == 2
    assert statuses(out)["1"] == "✓ виконано" and "? невідомо: 2, 4" in out.split("\n")[0]
    assert not (tmp_path / "missing.sqlite").exists()  # never created


def test_the_check_matches_the_trade_answer_on_the_same_line(journal_db, tmp_path):
    jt.handle("/trade AVAX long 30 size 2 stop 28", journal_db, NOW - H)  # one open trade in both journals
    for i, line in enumerate(
        (
            "BTC long 84000 size 0.1 stop 82000 target 90000 thesis x",
            "SOL/USDT:USDT short 182.5 size 3",
            "BTC-27OCT26-90000-C long 1200 size 0.01 target 95000 thesis чому",
            "BTC-26OCT26-90000-P long 150 size 1",
        )
    ):
        other = tmp_path / f"trade{i}.sqlite"  # /trade writes its row: a fresh copy per line
        other.write_bytes(journal_db.read_bytes())
        assert statuses(rp.handle("/check " + line, journal_db, NOW)) == statuses(jt.handle("/trade " + line, other, NOW)), line


def test_the_check_matches_the_trade_answer_at_a_fractional_second_on_the_24_hour_edge(journal_db):
    now = datetime(2026, 10, 25, 8, tzinfo=UTC).timestamp() + 0.6  # BTC-26OCT26 expires 24 h after the whole second
    line = "BTC-26OCT26-90000-C long 150 size 0.5 target 95000 thesis x"
    out = rp.handle("/check " + line, journal_db, now)
    assert statuses(out) == statuses(jt.handle("/trade " + line, journal_db, now))
    assert statuses(out)["8"] == "? невідомо" and "1–3 доби" in out  # the 1-3 day zone, as /trade, not the ban


def test_a_premium_in_coin_is_refused_and_never_shown_as_kept(journal_db):
    for text in ("/check BTC-31OCT26-90000-C premium 0.012", "/check BTC call premium 0.015 expiry 5d"):
        out = rp.handle(text, journal_db, NOW)
        assert out.startswith("❌ Перевірку не виконано: схоже на премію в BTC/ETH") and "✓" not in out
    out = rp.handle("/check BTC-31OCT26-90000-C put premium 80", journal_db, NOW)
    assert out.startswith("❌ Перевірку не виконано: call / put суперечить символу опціону")


def test_nothing_is_written_and_user_text_is_escaped(journal_db):
    jt.handle("/trade BTC long 84000 size 0.1 stop 82000", journal_db, NOW)
    before = hashlib.sha256(journal_db.read_bytes()).hexdigest()
    out = rp.handle("/check xyz:<b>X long 1", journal_db, NOW)
    assert out.startswith("❌ Перевірку не виконано: символ") and "<b>X" not in out
    out = rp.handle("/check BTC call premium 80 expiry 4d thesis <script>‮", journal_db, NOW)
    assert "<script>" not in out and "‮" not in out
    assert hashlib.sha256(journal_db.read_bytes()).hexdigest() == before


def test_a_bad_command_gives_the_format_and_a_failure_never_raises(journal_db, monkeypatch):
    for text in ("/check", "/check BTC call premium 80", "/check BTC long 84000 risk 90", "/check BTC-27OCT26-90000-C short premium 1"):
        out = rp.handle(text, journal_db, NOW)
        assert out.startswith("❌ Перевірку не виконано:") and "Формат:" in out
    for code in rp.ERRORS_UA:
        assert rp.error_reply({"error": code}).startswith("❌ Перевірку не виконано")
    monkeypatch.setattr(rp.rc, "trade_checks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = rp.handle("/check BTC call premium 80 expiry 4d", journal_db, NOW)
    assert out == "❌ Перевірку не вдалося зібрати (RuntimeError). Нічого не записано."


# ---------------------------------------------------------------- rule 8 (b) / (c) from the assistant snapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _near_snap() -> tuple[dict, float]:
    """The dashboard tests' input: the 20.10 snapshot plus the real chain_near of 23.10 (Deribit 24OCT26), stamped
    23.10 00:11:30 UTC; ``now`` 5 min later (31.7 h to the delivery: the 1-3 day zone)."""
    snap = json.loads((FIXTURES / "assistant_snapshot_2026-10-20.json").read_text(encoding="utf-8"))
    near = json.loads((FIXTURES / "assistant_chain_near_2026-10-23.json").read_text(encoding="utf-8"))
    snap["sections"]["chain_near"] = near["section"]
    snap["expiries"]["near"] = near["near"]
    snap["asof_utc"] = near["section"]["asof_utc"]
    return snap, rp.db.parse_utc(snap["asof_utc"]).timestamp() + 300


def _write(tmp_path, snap) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path


def _rule8_line(out: str) -> str:
    return next(x for x in out.split("\n") if x.startswith("Правило 8"))


def test_rule_8_b_and_c_come_from_the_real_snapshot(journal_db, tmp_path):
    snap, now = _near_snap()
    path = _write(tmp_path, snap)
    # 80000 put at $300 per coin: breakeven 79 700, 1.75 % from the forward 81 123 <= 1 sigma 1.9 %; IV 32.8 % <= 38.1 %
    out = rp.handle("/check BTC-24OCT26-80000-P long 300 size 0.1 target 78000 thesis пробій вниз", journal_db, now, path)
    assert statuses(out) == {"1": "✓ виконано", "2": "✓ виконано", "4": "✓ виконано", "5": "— не стосується", "8": "✓ виконано"}
    line = _rule8_line(out)
    assert "(б) ✓ до беззбитку 1.8 % від форварду 81 123, межа 1.9 % (1σ до експірації)" in line
    assert "(в) ✓ IV 32.8 %, межа 38.1 % (1.2 × RV30)" in line and "RV30 31.8 %, знімок 00:11 UTC" in line
    assert "премія $30 при межі $100" in line and "марка" not in line  # entry + size: the owner's per-coin price
    # a far call by its total premium: per coin at the snapshot's mark ($9.74), breakeven 6 % away, IV 43.3 % > 38.1 %
    out = rp.handle("/check BTC-24OCT26-86000-C premium 20 target 88000 thesis x", journal_db, now, path)
    assert statuses(out)["8"] == "✗ порушено" and out.startswith("<b>Перевірка перед угодою: ✗ порушено: 8</b>")
    line = _rule8_line(out)
    assert "(б) ✗ до беззбитку 6 %" in line and "(в) ✗ IV 43.3 %" in line and "премія за монету — марка $9.74" in line
    # (b) and (c) hold but (a) does not: still violated
    out = rp.handle("/check BTC-24OCT26-80000-P long 300 size 0.1", journal_db, now, path)
    assert statuses(out)["8"] == "✗ порушено" and "немає тези й цілі — умова (а) не виконана" in out and "(б) ✓" in out
    # a premium over the limit is violated whatever (b) and (c) say
    out = rp.handle("/check BTC-24OCT26-80000-P long 1200 size 0.1 target 78000 thesis x", journal_db, now, path)
    assert statuses(out)["8"] == "✗ порушено" and "завелика" in out


def test_check_and_the_dashboard_copy_agree_on_8b_8c():
    from tradedesk.analytics import rules_check as rc

    snap, now = _near_snap()
    ch = rp.db.option_chain(snap, "near")
    rv = snap["sections"]["vrp"]["data"]["current"]["rv_30d"]
    exp = datetime(2026, 10, 24, 8, tzinfo=UTC).timestamp()
    seen = set()
    for k, right, how in (
        (80000, "P", "long 300 size 0.1"),
        (82500, "C", "premium 50"),
        (86000, "C", "premium 20"),
        (78000, "P", "premium 40"),
        (81000, "C", "long 900 size 0.05"),
        (90000, "C", "premium 5"),
    ):
        check = rp.parse_check_command(f"/check BTC-24OCT26-{k}-{right} {how} target 95000 thesis x", now)["check"]
        ev = rp.evaluate(check, now, [], rp.market_inputs(check, snap, None, now))
        cond = ev["conditions"]
        assert cond is not None, k
        row = next(r for r in ch["rows"] if r["k"] == k)["c" if right == "C" else "p"]
        per = check["entry"] if check["entry"] is not None else row["mark"]
        size = check["size"] if check["entry"] is not None else check["premium"] / row["mark"]
        leg = {"kind": check["right"], "side": "long", "size": size, "strike": float(k), "entry_premium": per, "iv": row["iv"]}
        dash = {c["key"]: c for c in rp.db.rules_pre_trade([leg], int(now), exp, 10_000.0, ch["F"], rv, "x", 95000.0)}
        ref = {
            c["key"]: c for c in rc.pre_trade_option([leg], now_ts=int(now), expiry_ts=exp, spot=ch["F"], rv=rv, thesis="x", target=95000.0)
        }
        for key in ("breakeven_move", "iv_vs_rv"):
            assert cond[key]["status"] == dash[key]["status"] == ref[key]["status"], (k, key)
            assert cond[key]["value"] == pytest.approx(dash[key]["value"], rel=1e-9), (k, key)
            seen.add(cond[key]["status"])
        assert ev["statuses"][8] == rc.conditions_status(dash["premium"]["status"], dash["days_to_expiry"]["status"])
    assert seen == {"ok", "violated"}  # the inputs cover both outcomes


@pytest.mark.parametrize(
    ("text", "edit", "phrase"),
    [
        ("/check BTC call premium 50 expiry 36h target 95000 thesis x", None, "немає страйку: вкажіть символ опціону з датою"),
        ("/check BTC-25OCT26-80000-P premium 30 target 78000 thesis x", None, "у знімку найближча експірація 24OCT26, а не дата символу"),
        ("/check ETH-24OCT26-4000-C premium 30 target 4200 thesis x", None, "ланцюжок знімка — BTC, а не ETH"),
        ("/check BTC-24OCT26-80250-P premium 30 target 78000 thesis x", None, "страйка 80 250 (пут) у знімку немає"),
        ("/check BTC-24OCT26-80000-P premium 30 target 78000 thesis x", "no_chain", "у знімку немає ланцюжка найближчої експірації"),
        ("/check BTC-24OCT26-80000-P premium 30 target 78000 thesis x", "no_rv", "у знімку немає RV30"),
        ("/check BTC-24OCT26-80000-P premium 30 target 78000 thesis x", "no_asof", "час знімка невідомий"),
        ("/check BTC-24OCT26-80000-P premium 30 target 78000 thesis x", "odd_vrp", "у знімку немає RV30"),
    ],
)
def test_rule_8_b_and_c_stay_unknown_without_snapshot_inputs(journal_db, tmp_path, text, edit, phrase):
    snap, now = _near_snap()
    if edit == "no_chain":
        snap["sections"]["chain_near"] = {"ok": False, "data": None, "missing": [{"detail": "no suitable expiry"}], "warnings": []}
    elif edit == "no_rv":
        del snap["sections"]["vrp"]["data"]["current"]["rv_30d"]
    elif edit == "no_asof":
        snap["asof_utc"] = "вчора"
    elif edit == "odd_vrp":
        snap["sections"]["vrp"]["data"]["current"] = [0.3]
    out = rp.handle(text, journal_db, now, _write(tmp_path, snap))
    line = _rule8_line(out)
    assert statuses(out)["8"] == "? невідомо" and "(б) рух до беззбитку й (в) IV проти RV — невідомо: " + phrase in line, line
    assert "(б) ✓" not in line and "(в) ✓" not in line


def test_a_missing_corrupt_or_stale_snapshot_never_makes_8b_8c_ok(journal_db, tmp_path, monkeypatch):
    text = "/check BTC-24OCT26-80000-P long 300 size 0.1 target 78000 thesis x"  # ok with a fresh snapshot (above)
    snap, now = _near_snap()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for path, phrase in (
        (None, "знімок асистента не задано"),
        (tmp_path / "missing.json", "знімка асистента немає"),
        (bad, "файл знімка асистента пошкоджений"),
    ):
        out = rp.handle(text, journal_db, now, path)
        assert statuses(out)["8"] == "? невідомо" and "невідомо: " + phrase in out, phrase
    assert not (tmp_path / "missing.json").exists()
    path = _write(tmp_path, snap)
    # 31 min after the stamp: stale, so the old prices are not judged
    out = rp.handle(text, journal_db, now - 300 + 31 * 60, path)
    assert statuses(out)["8"] == "? невідомо" and "знімок 23.10 00:11 UTC старший за 30 хв" in out
    assert statuses(rp.handle(text, journal_db, now - 300 + 29 * 60, path))["8"] == "✓ виконано"
    # a chain carried from an older snapshot keeps its own (old) time: stale too, though the file is fresh
    snap["sections"]["chain_near"]["asof_utc"] = "2026-10-22T20:00:00Z"
    out = rp.handle(text, journal_db, now, _write(tmp_path, snap))
    assert statuses(out)["8"] == "? невідомо" and "знімок 22.10 20:00 UTC старший за 30 хв" in out
    # a strike without IV: (c) unknown, so rule 8 is unknown even with (b) held
    snap, now = _near_snap()
    for r in snap["sections"]["chain_near"]["data"]["strikes"]:
        if r["strike"] == 80000.0:
            r["put"]["iv"] = None
    out = rp.handle(text, journal_db, now, _write(tmp_path, snap))
    assert statuses(out)["8"] == "? невідомо" and "(б) ✓" in out and "(в) ? IV страйка в знімку немає" in out
    # an unexpected failure while reading leaves (b), (c) unknown, the reply still comes
    monkeypatch.setattr(rp, "market_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = rp.handle(text, journal_db, now, path)
    assert statuses(out)["8"] == "? невідомо" and "несподіваний формат знімка (RuntimeError)" in out


def test_snapshot_text_is_escaped_and_never_changes_a_limit(journal_db, tmp_path):
    snap, now = _near_snap()
    snap["sections"]["chain_near"]["data"]["currency"] = "<b>BTC</b>"
    snap["deposit_usd"] = 1e9  # not read: the limits are rules_check's constants
    out = rp.handle("/check BTC-24OCT26-80000-P long 300 size 0.1 target 78000 thesis x", journal_db, now, _write(tmp_path, snap))
    assert "<b>BTC</b>" not in out and "&lt;b&gt;BTC&lt;/b&gt;" in out and statuses(out)["8"] == "? невідомо"
    assert "премія $30 при межі $100" in out
    snap, now = _near_snap()
    snap["sections"]["vrp"]["data"]["current"]["rv_30d"] = 5.0  # a wild RV moves the checks, never the premium cap
    out = rp.handle("/check BTC-24OCT26-80000-P long 1200 size 0.1 target 78000 thesis x", journal_db, now, _write(tmp_path, snap))
    assert statuses(out)["8"] == "✗ порушено" and "при межі $100 — завелика" in out


def test_every_parser_error_code_has_ukrainian_words():
    import tradedesk.analytics.check_command as cc

    codes = set(re.findall(r"_err\(\s*\"(\w+)\"", Path(cc.__file__).read_text(encoding="utf-8")))
    assert codes and codes <= set(rp.ERRORS_UA)
