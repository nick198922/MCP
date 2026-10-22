"""Record a trade in the local journal from the owner's Telegram command and answer with rules 1, 2, 4, 5 and 8 for it.

    uv run python scripts/journal_trade_tg.py --text "/trade BTC long 84000 size 0.1 stop 82000 target 90000 thesis ..."
    uv run python scripts/journal_trade_tg.py --text "..." --db PATH      # another SQLite file

Called by scripts/tg_bridge.py for "/trade …" (the bridge runs on the system python3, so it starts this script with
uv in the project's environment); the bridge has already checked that the message is the owner's, in the group and
topic it serves, and sends the printed Telegram HTML back. The text is untrusted: analytics.trade_command parses it
strictly (an unknown word is an error, not a guess) into the journal_add fields only. The row goes in through the
same store call as the journal_add tool (Store.init + journal_insert, status 'open', opened now, tags ['telegram']
plus 'option' for an option symbol), into the dashboard's journal DB that the MCP server writes too; a missing file is
refused rather than created. The rules come from analytics.rules_check with its default thresholds (deposit
$10 000), as on the panel: trade_checks on the new row among the owner's real trades of the journal (no airdrop, no
*-paper rows: dashboard.journal_kind), read back through the same Store right after the insert, and open_risk on the
open ones. Rules 1, 5 and 8 depend on the new row only; rule 2 (total open risk <= 3 %, at most 3 alt longs) and rule
4 (day stop) on the journal, so they are 'unknown' when it cannot be read. Nothing here places, changes or cancels an
order or touches a limit: it only writes one journal row. Never raises: any failure is a short message.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard as db  # noqa: E402

from tradedesk.analytics import rules_check as rc  # noqa: E402
from tradedesk.analytics.trade_command import MAX_THESIS, parse_trade_command  # noqa: E402
from tradedesk.store.db import Store  # noqa: E402

DB_DEFAULT = Path(os.environ.get("TRADEDESK_DATA_DIR", "/var/lib/tradedesk-mcp")) / "tradedesk.sqlite"  # = dashboard.JOURNAL_DB
SOURCE_TAG = "telegram"
THESIS_SHOWN = 200  # characters of the thesis echoed back
USAGE = (
    "Формат: <code>/trade BTC long 84000 size 0.1 stop 82000 target 90000 thesis чому входжу</code>\n"
    "Обов'язково: символ, long / short, ціна входу, size (монети / контракти). Необов'язково: stop, target, thesis "
    "(до кінця повідомлення). Опціон: <code>/trade BTC-31OCT26-90000-C long 1200 size 0.01 target 95000 thesis …</code> — "
    "вхід у $ за 1 монету (менше за 1 — лише з $, напр. $0.4), target — ціна базового активу."
)
ERRORS_UA = {
    "empty": "порожнє повідомлення",
    "not_trade_command": "повідомлення має починатися з /trade",
    "too_long": "повідомлення задовге",
    "missing_symbol": "немає символу",
    "bad_symbol": "символ — лише латинські літери, цифри та / : . _ -, до 40 знаків",
    "missing_side": "немає напрямку: long або short",
    "missing_entry": "немає ціни входу",
    "bad_number": "число має бути додатним, на кшталт 84000, 0.1, 0,1 або 84k (не 84,000)",
    "missing_value": "після слова немає числа",
    "unknown_word": "незнайоме слово: дозволено size, stop, target, thesis",
    "duplicate": "поле вказано двічі",
    "missing_size": "немає size (розмір у монетах чи контрактах)",
    "stop_wrong_side": "стоп з іншого боку від входу (у лонга — нижче, у шорта — вище)",
    "target_wrong_side": "ціль з іншого боку від входу (у лонга — вище, у шорта — нижче)",
    "thesis_too_long": f"теза довша за {MAX_THESIS} знаків",
    "option_entry_in_coin": "схоже на премію в BTC/ETH: вкажіть $ за 1 монету, напр. 1200 або $0.4",
    "option_expired": "дата експірації опціону вже минула — перевірте дату в символі",
}
STATUS_UA = {rc.OK: "✓ виконано", rc.VIOLATED: "✗ порушено", rc.UNKNOWN: "? невідомо", rc.NA: "— не стосується"}
SIDE_UA = {"long": "лонг", "short": "шорт"}
CAVEAT = (
    "Лише запис у локальний журнал: жодних ордерів, ліміти не змінюються. Правила — з депозитом $10 000; "
    "2 і 4 — за реальними угодами журналу (відкрита — доки не закрито /close; імпорт з бірж може запізнюватися)."
)
STOP_REASON_UA = {"streak": "2 збиткові поспіль", "day_loss": "−2 % за день", "both": "2 збиткові поспіль і −2 % за день"}
NO_JOURNAL = "журнал не прочитано"


def _h(x: object) -> str:
    return html.escape(str(x), quote=False)


def fmt(v: float) -> str:
    """84000 -> '84 000', 0.1 -> '0.1', 0.00000123 -> '0.00000123'."""
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    whole, _, frac = s.partition(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return whole + (f".{frac}" if frac else "")


def usd(v: float) -> str:
    """$ to the cent from $1 up; below $1 three significant digits, so a tiny premium never reads as '$0'."""
    if v == 0 or abs(v) >= 1:
        return "$" + fmt(round(v, 2))
    s = fmt(float(f"{v:.3g}"))
    return "$" + (s if s != "0" else f"{v:.2e}")


def error_reply(parsed: dict[str, Any]) -> str:
    why = ERRORS_UA.get(parsed.get("error") or "", "не вдалося розібрати")
    field = parsed.get("field")
    return f"❌ Угоду не записано: {_h(why)}" + (f" (поле {_h(field)})" if field else "") + ".\n" + USAGE


def tags_for(trade: dict[str, Any]) -> list[str]:
    return [SOURCE_TAG] + (["option"] if rc.option_expiry_ts(trade["symbol"]) is not None else [])


def _rule1_line(trade: dict[str, Any], status: str, budget: float) -> str:
    is_opt = rc.option_expiry_ts(trade["symbol"]) is not None
    size, entry, stop = trade["size"], trade["entry"], trade["stop"]
    if is_opt and trade["side"] == "long":
        why = f"ризик = премія {usd(size * entry)} при межі {usd(budget)}"
    elif is_opt:
        why = "проданий опціон: ризик залежить від стопу на біржі чи захисних крил, із запису його не порахувати"
    elif stop is None:
        why = "без стопу ризик не порахувати: додайте stop"
    else:
        why = f"ризик до стопу {usd(size * abs(entry - stop))} при межі {usd(budget)}"
    return f"Правило 1 · ризик ≤ 1 % депозиту: {STATUS_UA[status]} — {_h(why)}."


def _rule2_line(check: dict[str, Any], risk: dict[str, Any] | None) -> str:
    head = f"Правило 2 · відкритий ризик ≤ {fmt(rc.OPEN_RISK_PCT)} %, ≤ {rc.MAX_ALT_LONGS} лонги в альтах"
    if risk is None:
        return f"{head}: {STATUS_UA[rc.UNKNOWN]} — {NO_JOURNAL}."
    alt = check["rules"][2]
    status = rc.NA if risk["status"] == alt == rc.NA else rc.conditions_status(risk["status"], alt)
    parts = [f"відкритий ризик {usd(risk['total'])} при межі {usd(risk['limit'])} (відкритих угод {risk['n_open']})"]
    if risk["n_unknown"]:
        parts.append(f"ризик ще {risk['n_unknown']} не пораховано (без стопу або проданий опціон)")
    if alt in (rc.OK, rc.VIOLATED):
        parts.append(f"лонгів в альтах відкрито {check['alt_longs']}" + (" — новий альт понад межу" if alt == rc.VIOLATED else ""))
    elif alt == rc.UNKNOWN:
        parts.append("символ HIP-3 (акції, золото) у ліміт лонгів в альтах не входить")
    return f"{head}: {STATUS_UA[status]} — {_h('; '.join(parts))}."


def _rule4_line(check: dict[str, Any], stops: dict[str, dict[str, Any]] | None) -> str:
    head = "Правило 4 · денний стоп"
    if stops is None:
        return f"{head}: {STATUS_UA[rc.UNKNOWN]} — {NO_JOURNAL}."
    status = check["rules"][4]
    if status == rc.VIOLATED:
        s = stops[rc.utc_day(check["opened_ts"])]
        at = datetime.fromtimestamp(s["ts"], UTC).strftime("%H:%M")
        why = f"сьогодні спрацював о {at} UTC ({STOP_REASON_UA.get(s['reason'], s['reason'])}): нових угод до кінця дня UTC не відкривати"
    else:
        why = f"сьогодні (UTC) за закритими угодами журналу не спрацював ({rc.DAY_LOSING_STREAK} збиткові поспіль або −{fmt(rc.DAY_LOSS_PCT)} %)"
    return f"{head}: {STATUS_UA[status]} — {_h(why)}."


def _rule5_line(trade: dict[str, Any], status: str) -> str:
    head = f"Правило 5 · ціль ≥ {fmt(rc.MIN_RR)} × стоп"
    if status == rc.NA:
        return f"{head}: {STATUS_UA[status]} (опціон)."
    entry, stop, target = trade["entry"], trade["stop"], trade["target"]
    if status == rc.UNKNOWN:
        miss = [w for w, v in (("stop", stop), ("target", target)) if v is None]
        why = "R:R не порахувати" + (": додайте " + " і ".join(miss) if miss else "")
    else:
        why = f"R:R {fmt(round(abs(target - entry) / abs(entry - stop), 2))}"
    return f"{head}: {STATUS_UA[status]} — {_h(why)}."


def real_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The owner's real trades, as the dashboard's rules block takes them: no airdrop, no *-paper rows."""
    return [t for t in rows if db.journal_kind(db.journal_tags(t.get("tags")))[0] == "real"]


def _rule8_line(trade: dict[str, Any], check: dict[str, Any], cap: float, now: float) -> str:
    status = check["rules"][8]
    if status == rc.NA:
        return f"Правило 8 · купівля опціонів: {STATUS_UA[status]} (це не куплений опціон)."
    parts = [f"премія {usd(trade['size'] * trade['entry'])} при межі {usd(cap)}" + (" — завелика" if check["big_premium"] else "")]
    exp = rc.option_expiry_ts(trade["symbol"])
    zone = check["buy_zone"]
    if exp is not None:
        parts.append(f"до експірації {fmt(round((exp - now) / 86400.0, 1))} дн.")
    if zone == rc.ZONE_BAN:
        parts.append("менше 24 год до експірації — купувати не можна")
    elif zone == rc.ZONE_COND:
        conds = check["buy_conditions"] or {}
        thesis_ok = conds.get("thesis") == rc.OK
        parts.append("1–3 доби: лише з трьома умовами; теза й ціль " + ("записані" if thesis_ok else "не записані — умова (а) не виконана"))
        parts.append("рух до беззбитку й IV проти RV тут не перевіряються — перевірте в конструкторі «Опціони»")
    elif zone == rc.ZONE_FREE:
        parts.append("понад 3 доби — лише ліміт премії")
    else:
        parts.append("експірацію з символу не прочитано")
    return f"Правило 8 · купівля опціонів: {STATUS_UA[status]} — {_h('; '.join(parts))}."


def build_reply(trade: dict[str, Any], trade_id: int, now: float, journal: list[dict[str, Any]] | None = None) -> str:
    """Telegram HTML for a recorded trade: what was written and rules 1, 2, 4, 5 and 8 on it. Pure (time passed in);
    the rules are analytics.rules_check (trade_checks, open_risk, day_stops) with its default thresholds on this row
    among the real trades of ``journal`` (all journal rows; None = not read: rules 2 and 4 are 'unknown')."""
    row = {
        "id": trade_id,
        "opened_ts": int(now),
        "closed_ts": None,
        "status": "open",
        "symbol": trade["symbol"],
        "side": trade["side"],
        "size": trade["size"],
        "entry": trade["entry"],
        "stop": trade["stop"],
        "target": trade["target"],
        "thesis": trade["thesis"],
        "tags": tags_for(trade),
        "pnl": None,
    }
    others = None if journal is None else [r for r in real_trades(journal) if r.get("id") != trade_id]
    rows = [row] if others is None else [*others, row]
    check = next(c for c in rc.trade_checks(rows, now_ts=now) if c["id"] == trade_id)
    head = f"<b>✅ Записано в журнал: угода #{trade_id}</b>"
    what = [_h(trade["symbol"]), SIDE_UA[trade["side"]], f"{fmt(trade['size'])} за {fmt(trade['entry'])}"]
    if trade["stop"] is not None:
        what.append(f"стоп {fmt(trade['stop'])}")
    if trade["target"] is not None:
        what.append(f"ціль {fmt(trade['target'])}")
    lines = [head, " · ".join(what)]
    thesis = trade["thesis"]
    if thesis:
        shown = " ".join(thesis.split())
        lines.append("Теза: " + _h(shown if len(shown) <= THESIS_SHOWN else shown[: THESIS_SHOWN - 1] + "…"))
    else:
        lines.append("Тези немає (для купівлі опціону за 1–3 доби до експірації вона обов'язкова).")
    lines.append(_rule1_line(trade, check["rules"][1], rc.DEPOSIT_USD * rc.RISK_PCT / 100.0))
    lines.append(_rule2_line(check, None if others is None else rc.open_risk(rows)))
    lines.append(_rule4_line(check, None if others is None else rc.day_stops(rows)))
    lines.append(_rule5_line(trade, check["rules"][5]))
    lines.append(_rule8_line(trade, check, rc.DEPOSIT_USD * rc.PREMIUM_PCT / 100.0, now))
    lines += ["", f"<i>{_h(CAVEAT)} Закрити: /close {trade_id} ЦІНА_ВИХОДУ; не відбулася: /cancel {trade_id}.</i>"]
    return "\n".join(lines)


async def record(db_path: Path, trade: dict[str, Any], now: float) -> tuple[int, list[dict[str, Any]] | None]:
    """Write the trade as journal_add does (Store.init, then journal_insert, status 'open'); the new id and the
    journal read back through the same store (journal_list, the new row included; None if that read failed)."""
    store = Store(db_path)
    try:
        await store.init()
        trade_id = await store.journal_insert(
            opened_ts=int(now),
            symbol=trade["symbol"],
            side=trade["side"],
            size=trade["size"],
            entry=trade["entry"],
            stop=trade["stop"],
            target=trade["target"],
            thesis=trade["thesis"],
            tags=tags_for(trade),
        )
        try:
            journal = await store.journal_list(limit=db.JOURNAL_MAX_ROWS)
        except Exception:  # noqa: BLE001 - the row is written; rules 2 and 4 become 'unknown'
            journal = None
        return trade_id, journal
    finally:
        await store.aclose()


def handle(text: str, db_path: Path, now: float) -> str:
    """Parse, record and answer. Never raises: a bad command gives the format, a store failure a short reason."""
    parsed = parse_trade_command(text, now=now)
    if not parsed["ok"]:
        return error_reply(parsed)
    trade = parsed["trade"]
    if not db_path.exists():
        return f"❌ Угоду не записано: файла журналу {_h(db_path)} немає (його створює MCP-сервер)."
    try:
        trade_id, journal = asyncio.run(record(db_path, trade, now))
    except Exception as e:  # noqa: BLE001 - the owner gets a reason, the bridge keeps running
        return f"❌ Угоду не записано: збій бази ({_h(type(e).__name__)}). Спробуйте ще раз або journal_add у Claude."
    try:
        return build_reply(trade, trade_id, now, journal)
    except Exception as e:  # noqa: BLE001 - the row is written; only the rules text failed
        return f"✅ Записано в журнал: угода #{trade_id}. Перевірку правил не вдалося зібрати ({_h(type(e).__name__)})."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text", required=True, help="the owner's /trade message")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT, help="SQLite file (default: the dashboard's journal DB)")
    a = ap.parse_args(argv)
    print(handle(a.text, a.db, time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
