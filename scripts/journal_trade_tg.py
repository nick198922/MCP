"""Record a trade in the local journal from the owner's Telegram command and answer with rules 1 and 8 for it.

    uv run python scripts/journal_trade_tg.py --text "/trade BTC long 84000 size 0.1 stop 82000 target 90000 thesis ..."
    uv run python scripts/journal_trade_tg.py --text "..." --db PATH      # another SQLite file

Called by scripts/tg_bridge.py for "/trade …" (the bridge runs on the system python3, so it starts this script with
uv in the project's environment); the bridge has already checked that the message is the owner's, in the group and
topic it serves, and sends the printed Telegram HTML back. The text is untrusted: analytics.trade_command parses it
strictly (an unknown word is an error, not a guess) into the journal_add fields only. The row goes in through the
same store call as the journal_add tool (Store.init + journal_insert, status 'open', opened now, tags ['telegram']
plus 'option' for an option symbol), into the dashboard's journal DB that the MCP server writes too; a missing file is
refused rather than created. Rules 1 and 8 come from analytics.rules_check.trade_checks on the new row with its
default thresholds (deposit $10 000), as on the panel. Nothing here places, changes or cancels an order or touches a
limit: it only writes one journal row. Never raises: any failure is a short message.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
import time
from pathlib import Path
from typing import Any

from tradedesk.analytics import rules_check as rc
from tradedesk.analytics.trade_command import MAX_THESIS, parse_trade_command
from tradedesk.store.db import Store

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
    "правила 2, 4 і 5 — на панелі й у тижневому звіті."
)


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


def build_reply(trade: dict[str, Any], trade_id: int, now: float) -> str:
    """Telegram HTML for a recorded trade: what was written and rules 1 and 8 on it. Pure (time passed in); the rules
    are analytics.rules_check.trade_checks on this one row with its default thresholds."""
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
    check = rc.trade_checks([row], now_ts=now)[0]
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
    lines.append(_rule8_line(trade, check, rc.DEPOSIT_USD * rc.PREMIUM_PCT / 100.0, now))
    lines += ["", f"<i>{_h(CAVEAT)} Закрити — journal_close (id {trade_id}).</i>"]
    return "\n".join(lines)


async def record(db_path: Path, trade: dict[str, Any], now: float) -> int:
    """Write the trade as journal_add does (Store.init, then journal_insert, status 'open'); the new id."""
    store = Store(db_path)
    try:
        await store.init()
        return await store.journal_insert(
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
        trade_id = asyncio.run(record(db_path, trade, now))
    except Exception as e:  # noqa: BLE001 - the owner gets a reason, the bridge keeps running
        return f"❌ Угоду не записано: збій бази ({_h(type(e).__name__)}). Спробуйте ще раз або journal_add у Claude."
    try:
        return build_reply(trade, trade_id, now)
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
