"""Check a planned trade against the owner's rules from his Telegram /check command, before it is placed; records nothing.

    uv run python scripts/rules_pretrade_tg.py --text "/check BTC call premium 80 expiry 36h target 95000 thesis ..."
    uv run python scripts/rules_pretrade_tg.py --text "/check SOL long 180 stop 170 target 200 risk 90"
    uv run python scripts/rules_pretrade_tg.py --text "..." --db PATH      # another SQLite file

Called by scripts/tg_bridge.py for "/check …" (the bridge runs on the system python3, so it starts this script with
uv in the project's environment); the bridge has already checked that the message is the owner's, in the group and
topic it serves, and sends the printed Telegram HTML back. The text is untrusted: analytics.check_command parses it
strictly into a hypothetical journal row (as_journal_row). The rules are analytics.rules_check with its default
thresholds (deposit $10 000), the same calls as the /trade answer: trade_checks on that row among the owner's real
trades (no airdrop, no *-paper rows), open_risk and day_stops with the row included, so a line /trade would record
gets the same statuses here (an option sale, which /trade records, is refused). Rules 1, 5 and 8 depend on the row only; 2 and 4 on the journal, read over a
``mode=ro`` connection (rules_weekly_tg.load_journal), so they are 'unknown' when it cannot be read. 'unknown' is
never 'ok'. At most 8 lines: the verdict, the trade, rules 1, 2, 4, 5, 8 and one disclaimer line.

Read-only: nothing is written to the journal (or anywhere), no order is placed, changed or cancelled, no limit is
touched, no key is read, no network. Never raises: any failure is a short message.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules_weekly_tg as rw  # noqa: E402
from journal_trade_tg import SIDE_UA, STATUS_UA, STOP_REASON_UA, _h, fmt, usd  # noqa: E402

from tradedesk.analytics import rules_check as rc  # noqa: E402
from tradedesk.analytics.check_command import CHECK_ID, MAX_THESIS, as_journal_row, parse_check_command  # noqa: E402

RULES = (1, 2, 4, 5, 8)
RIGHT_UA = {"call": "кол", "put": "пут"}
NO_JOURNAL = "журнал не прочитано"
USAGE = (
    "Формат: <code>/check BTC call premium 80 expiry 36h target 95000 thesis чому</code> (купівля опціону; "
    "або символ опціону з датою, напр. BTC-31OCT26-90000-C, тоді без expiry) чи "
    "<code>/check SOL long 180 stop 170 target 200 risk 90</code> (risk — $ до стопу; або size). "
    "premium — $ загалом. thesis — останнім: забирає весь кінець повідомлення. "
    "Рядок /trade теж підходить, крім продажу опціону. Нічого не записується."
)
ERRORS_UA = {
    "empty": "порожнє повідомлення",
    "not_check_command": "повідомлення має починатися з /check",
    "too_long": "повідомлення задовге",
    "missing_symbol": "немає символу",
    "bad_symbol": "символ — лише латинські літери, цифри та / : . _ -, до 40 знаків",
    "missing_side": "немає напрямку: long / short, або call / put для опціону",
    "missing_entry": "немає ціни входу",
    "bad_number": "число має бути додатним, на кшталт 84000, 0.1, 0,1 або 84k (не 84,000)",
    "missing_value": "після слова немає значення",
    "unknown_word": "незнайоме слово: дозволено premium, risk, size, stop, target, expiry, thesis",
    "duplicate": "поле вказано двічі",
    "missing_size": "немає size: вхід опціону — $ за 1 монету, або вкажіть premium загалом",
    "stop_wrong_side": "стоп з іншого боку від входу (у лонга — нижче, у шорта — вище)",
    "target_wrong_side": "ціль з іншого боку від входу (у лонга — вище, у шорта — нижче)",
    "thesis_too_long": f"теза довша за {MAX_THESIS} знаків",
    "option_entry_in_coin": "схоже на премію в BTC/ETH: вкажіть $ за 1 монету, напр. 1200 або $0.4",
    "option_premium_in_coin": "схоже на премію в BTC/ETH: premium — $ загалом, напр. 80 або $0.5",
    "kind_conflict": "call / put суперечить символу опціону (C / P у кінці) — перевірте символ",
    "option_expired": "дата експірації опціону вже минула — перевірте дату в символі",
    "option_sale": "перевіряю лише купівлю опціону: ризик проданого залежить від стопу чи крил",
    "option_field": "для купленого опціону ризик — це премія: stop і risk не потрібні",
    "missing_premium": "немає premium (скільки $ платите загалом)",
    "premium_and_entry": "вкажіть premium загалом або entry + size, не обидва",
    "missing_expiry": "немає expiry: години чи дні до експірації, напр. expiry 36h або 2d",
    "expiry_conflict": "символ опціону вже містить дату експірації: expiry не потрібен",
    "bad_expiry": "expiry — години чи дні до експірації, напр. 36h, 2d, 1.5d (до 366 днів)",
    "not_an_option": "premium і expiry — для опціону: напишіть call / put або символ опціону",
    "size_and_risk": "вкажіть size або risk, не обидва",
    "risk_needs_stop": "risk рахується до стопу: додайте stop",
}
DISCLAIMER = (
    "Статистика дотримання правил, не порада: нічого не записано, жодних ордерів; депозит $10 000, 2 і 4 — за реальними угодами журналу."
)
VERDICT_UA = {rc.VIOLATED: "✗ порушено", rc.UNKNOWN: "? невідомо", rc.OK: "✓ виконано"}


def error_reply(parsed: dict[str, Any]) -> str:
    why = ERRORS_UA.get(parsed.get("error") or "", "не вдалося розібрати")
    field = parsed.get("field")
    return f"❌ Перевірку не виконано: {_h(why)}" + (f" (поле {_h(field)})" if field else "") + ".\n" + USAGE


def _hours_left(check: dict[str, Any], now: float) -> float:
    return (float(check["expiry_ts"]) - now) / 3600.0


def _what_line(check: dict[str, Any], now: float) -> str:
    th = "теза є" if check["thesis"] else "тези немає"
    if check["kind"] == "option":
        h = _hours_left(check, now)
        name = _h(check["symbol"]) + (f" {RIGHT_UA[check['right']]}" if check["right"] and check["expiry_from"] == "text" else "")
        parts = [
            name,
            "купівля опціону",
            f"премія {usd(check['premium'])}",
            f"до експірації {fmt(round(h / 24.0, 1))} дн. ({fmt(round(h))} год)",
        ]
        if check["target"] is not None:
            parts.append(f"ціль {fmt(check['target'])}")
        return " · ".join([*parts, th])
    parts = [_h(check["symbol"]), SIDE_UA[check["side"]], f"вхід {fmt(check['entry'])}"]
    if check["stop"] is not None:
        parts.append(f"стоп {fmt(check['stop'])}")
    if check["target"] is not None:
        parts.append(f"ціль {fmt(check['target'])}")
    if check["risk"] is not None:
        parts.append(f"ризик {usd(check['risk'])} (розмір {fmt(round(check['size'], 8))})")
    elif check["size"] is not None:
        parts.append(f"розмір {fmt(check['size'])}")
    return " · ".join([*parts, th])


def _rule1(check: dict[str, Any], st: str) -> str:
    budget = rc.DEPOSIT_USD * rc.RISK_PCT / 100.0
    if check["kind"] == "option":
        why = f"премія {usd(check['premium'])} при межі {usd(budget)}"
    elif check["stop"] is None:
        why = "без стопу ризик не порахувати: додайте stop"
    elif check["risk"] is None:
        why = "додайте risk ($ до стопу) або size"
    else:
        why = f"ризик до стопу {usd(check['risk'])} при межі {usd(budget)}"
    return f"Правило 1 · ризик ≤ 1 % депозиту: {STATUS_UA[st]} — {_h(why)}."


def _rule2(c: dict[str, Any], risk: dict[str, Any] | None, st: str) -> str:
    head = f"Правило 2 · відкритий ризик ≤ {fmt(rc.OPEN_RISK_PCT)} %, ≤ {rc.MAX_ALT_LONGS} лонги в альтах"
    if risk is None:
        return f"{head}: {STATUS_UA[st]} — {NO_JOURNAL}."
    parts = [f"разом з цією {usd(risk['total'])} при межі {usd(risk['limit'])} (відкритих у журналі {risk['n_open'] - 1})"]
    if risk["n_unknown"]:
        parts.append(f"ризик {risk['n_unknown']} угод не пораховано (без стопу чи розміру, або проданий опціон)")
    alt = c["rules"][2]
    if alt in (rc.OK, rc.VIOLATED):
        parts.append(f"лонгів в альтах з цією {c['alt_longs']}" + (" — новий альт понад межу" if alt == rc.VIOLATED else ""))
    elif alt == rc.UNKNOWN:
        parts.append("символ HIP-3 (акції, золото) у ліміт лонгів в альтах не входить")
    return f"{head}: {STATUS_UA[st]} — {_h('; '.join(parts))}."


def _rule4(stops: dict[str, dict[str, Any]] | None, st: str, now: float) -> str:
    head = "Правило 4 · денний стоп"
    if stops is None:
        return f"{head}: {STATUS_UA[st]} — {NO_JOURNAL}."
    if st == rc.VIOLATED:
        s = stops[rc.utc_day(now)]
        at = datetime.fromtimestamp(s["ts"], UTC).strftime("%H:%M")
        why = f"сьогодні спрацював о {at} UTC ({STOP_REASON_UA.get(s['reason'], s['reason'])}): до кінця дня UTC нових угод не відкривати"
    else:
        why = "сьогодні (UTC) за закритими угодами журналу не спрацював"
    return f"{head}: {STATUS_UA[st]} — {_h(why)}."


def _rule5(check: dict[str, Any], st: str) -> str:
    head = f"Правило 5 · ціль ≥ {fmt(rc.MIN_RR)} × стоп"
    if st == rc.NA:
        return f"{head}: {STATUS_UA[st]} (опціон)."
    if st == rc.UNKNOWN:
        miss = [w for w in ("stop", "target") if check[w] is None]
        why = "R:R не порахувати" + (": додайте " + " і ".join(miss) if miss else "")
    else:
        why = f"R:R {fmt(round(abs(check['target'] - check['entry']) / abs(check['entry'] - check['stop']), 2))}"
    return f"{head}: {STATUS_UA[st]} — {_h(why)}."


def _rule8(check: dict[str, Any], c: dict[str, Any], st: str) -> str:
    head = "Правило 8 · купівля опціонів"
    if st == rc.NA:
        return f"{head}: {STATUS_UA[st]} (це не купівля опціону)."
    cap = rc.DEPOSIT_USD * rc.PREMIUM_PCT / 100.0
    parts = [f"премія {usd(check['premium'])} при межі {usd(cap)}" + (" — завелика" if c["big_premium"] else "")]
    zone = c["buy_zone"]
    if zone == rc.ZONE_BAN:
        parts.append("менше 24 год до експірації — купувати не можна")
    elif zone == rc.ZONE_COND:
        if (c["buy_conditions"] or {}).get("thesis") == rc.OK:
            cond_a = "теза й ціль є"
        else:
            miss = [w for w, v in (("тези", check["thesis"]), ("цілі", check["target"])) if not v]
            cond_a = "немає " + (" й ".join(miss) or "тези й цілі") + " — умова (а) не виконана"
        parts.append("1–3 доби: лише з трьома умовами; " + cond_a)
        parts.append("рух до беззбитку й IV проти RV тут не перевіряються — конструктор «Опціони»")
    elif zone == rc.ZONE_FREE:
        parts.append("понад 3 доби — лише ліміт премії")
    else:
        parts.append("час до експірації не прочитано")
    return f"{head}: {STATUS_UA[st]} — {_h('; '.join(parts))}."


def evaluate(check: dict[str, Any], now: float, journal: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Rule statuses of the planned trade: {statuses: {n: status}, check: its trade_checks row, risk: open_risk or
    None, stops: day_stops or None}. ``journal`` = all journal rows (None = not read: rules 2 and 4 'unknown').
    Pure: analytics.rules_check on the hypothetical row among the real trades, as journal_trade_tg.build_reply."""
    row = as_journal_row(check, now)
    others = None if journal is None else rw.real_trades(journal)
    rows = [row] if others is None else [*others, row]
    c = next(x for x in rc.trade_checks(rows, now_ts=now) if x["id"] == CHECK_ID)
    risk = None if others is None else rc.open_risk(rows)
    stops = None if others is None else rc.day_stops(rows)
    st = dict(c["rules"])
    if others is None:
        st[2] = st[4] = rc.UNKNOWN
    else:
        alt = c["rules"][2]
        st[2] = rc.NA if risk["status"] == alt == rc.NA else rc.conditions_status(risk["status"], alt)
    return {"statuses": st, "check": c, "risk": risk, "stops": stops}


def verdict_text(st: dict[int, str]) -> str:
    """'✗ порушено: 8 · ? невідомо: 2, 4' or '✓ виконано: 1, 2, 4, 8': violated first, unknown never counted as ok."""
    groups = {s: [str(n) for n in RULES if st[n] == s] for s in (rc.VIOLATED, rc.UNKNOWN, rc.OK)}
    if not groups[rc.VIOLATED] and not groups[rc.UNKNOWN]:
        return VERDICT_UA[rc.OK] + ": " + (", ".join(groups[rc.OK]) or "—")
    return " · ".join(f"{VERDICT_UA[s]}: {', '.join(groups[s])}" for s in (rc.VIOLATED, rc.UNKNOWN) if groups[s])


def build_reply(check: dict[str, Any], now: float, journal: list[dict[str, Any]] | None) -> str:
    """Telegram HTML, at most 8 lines: verdict, the trade, rules 1, 2, 4, 5, 8, one disclaimer. Pure (time passed in)."""
    ev = evaluate(check, now, journal)
    st, c = ev["statuses"], ev["check"]
    verdict = verdict_text(st)
    lines = [
        f"<b>Перевірка перед угодою: {_h(verdict)}</b>",
        _what_line(check, now),
        _rule1(check, st[1]),
        _rule2(c, ev["risk"], st[2]),
        _rule4(ev["stops"], st[4], now),
        _rule5(check, st[5]),
        _rule8(check, c, st[8]),
        f"<i>{_h(DISCLAIMER)}</i>",
    ]
    return "\n".join(lines)


def handle(text: str, db_path: Path, now: float) -> str:
    """Parse, read the journal (read-only) and answer. Never raises."""
    parsed = parse_check_command(text, now=now)
    if not parsed["ok"]:
        return error_reply(parsed)
    journal, _err = rw.load_journal(db_path)
    try:
        return build_reply(parsed["check"], now, journal)
    except Exception as e:  # noqa: BLE001 - the owner gets a reason, the bridge keeps running
        return f"❌ Перевірку не вдалося зібрати ({_h(type(e).__name__)}). Нічого не записано."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text", required=True, help="the owner's /check message")
    ap.add_argument("--db", type=Path, default=rw.db.JOURNAL_DB, help="SQLite file (default: the dashboard's journal DB)")
    a = ap.parse_args(argv)
    print(handle(a.text, a.db, time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
