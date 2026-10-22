"""Cancel an open trade in the local journal from the owner's Telegram command (a trade that never filled).

    uv run python scripts/journal_cancel_tg.py --text "/cancel 12 note the limit never filled"
    uv run python scripts/journal_cancel_tg.py --text "..." --db PATH      # another SQLite file

Called by scripts/tg_bridge.py for "/cancel …" (the bridge runs on the system python3, so it starts this script with
uv in the project's environment); the bridge has already checked that the message is the owner's, in the group and
topic it serves, and sends the printed Telegram HTML back. The text is untrusted: analytics.cancel_command parses it
strictly into the id and an optional note. The cancel goes the journal_close tool's ``cancelled=true`` way: Store.init,
journal_get, only a row with status 'open', then the same columns as its journal_update_close call (closed_ts now,
status and outcome 'cancelled', exit, pnl and r_multiple NULL, note as given) but conditional (``AND status = 'open'``,
rowcount checked), so a row closed meanwhile by the demo trader or journal_close is never overwritten. Rows mirrored
by the demo trader (tags auto / demo-paper / bybit-demo) are refused: the bot manages them. The dashboard's journal DB
is used; a missing file is refused rather than created. Nothing here places, changes or cancels an order or touches a
limit: it only updates one journal row. Never raises: any failure is a short message.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from journal_close_tg import STATUS_WORD, is_bot_row
from journal_trade_tg import DB_DEFAULT, SIDE_UA, THESIS_SHOWN, _h, fmt

from tradedesk.analytics.cancel_command import MAX_NOTE, parse_cancel_command
from tradedesk.store.db import Store

USAGE = (
    "Формат: <code>/cancel 12 note чому скасовую</code>\n"
    "Обов'язково: id угоди (з відповіді на /trade). Необов'язково: note — нотатка до кінця повідомлення. "
    "Скасування — для угоди, що не відбулася (без виходу й P&amp;L); вийшли з позиції — <code>/close 12 ЦІНА</code>."
)
ERRORS_UA = {
    "empty": "порожнє повідомлення",
    "not_cancel_command": "повідомлення має починатися з /cancel",
    "too_long": "повідомлення задовге",
    "missing_id": "немає id угоди",
    "bad_id": "id — ціле додатне число, напр. 12 або #12",
    "unknown_word": "після id дозволено лише note",
    "note_too_long": f"нотатка довша за {MAX_NOTE} знаків",
}
# journal_close(cancelled=true)'s columns, but only while the row is still open (journal_update_close has no status guard)
CANCEL_SQL = (
    "UPDATE journal SET closed_ts = ?, status = 'cancelled', exit = NULL, outcome = 'cancelled', pnl = NULL, "
    "r_multiple = NULL, note = ? WHERE id = ? AND status = 'open'"
)
CAVEAT = "Лише запис у локальний журнал: жодних ордерів, ліміти не змінюються. Ордер чи позицію на біржі скасовуйте самі."


def error_reply(code: str, field: str | None = None) -> str:
    why = ERRORS_UA.get(code, "не вдалося розібрати")
    return f"❌ Угоду не скасовано: {_h(why)}" + (f" (поле {_h(field)})" if field else "") + ".\n" + USAGE


def build_reply(row: dict[str, Any]) -> str:
    """Telegram HTML for a cancelled trade: what was cancelled and what that means for the stats. Pure."""
    lines = [
        f"<b>✅ Скасовано в журналі: угода #{row['id']}</b>",
        " · ".join(
            [_h(row["symbol"]), SIDE_UA.get(row["side"], _h(row["side"])), fmt(float(row["size"])), f"вхід {fmt(float(row['entry']))}"]
        ),
        "Статус: скасована — без виходу й P&amp;L; у статистику журналу й відкритий ризик не входить.",
    ]
    note = row.get("note")
    if note:
        shown = " ".join(note.split())
        lines.append("Нотатка: " + _h(shown if len(shown) <= THESIS_SHOWN else shown[: THESIS_SHOWN - 1] + "…"))
    lines += ["", f"<i>{_h(CAVEAT)}</i>"]
    return "\n".join(lines)


async def cancel_trade(db_path: Path, cancel: dict[str, Any], now: float, progress: dict[str, bool]) -> dict[str, Any]:
    """Cancel as journal_close(cancelled=true) does (Store.init, journal_get, status must be 'open', then its column
    update, here only while the row is still open). {'error': code, ...} when the row cannot be cancelled, else
    {'row': the cancelled row}. Sets progress['written'] once the update is committed."""
    store = Store(db_path)
    try:
        await store.init()
        row = await store.journal_get(cancel["id"])
        if row is None:
            return {"error": "not_found"}
        if is_bot_row(row):
            return {"error": "bot_row"}
        if row["status"] != "open":
            return {"error": "not_open", "status": row["status"]}
        cancelled = {
            **row,
            "closed_ts": int(now),
            "status": "cancelled",
            "exit": None,
            "outcome": "cancelled",
            "pnl": None,
            "r_multiple": None,
            "note": cancel["note"],
        }
        cur = await store.conn.execute(CANCEL_SQL, (cancelled["closed_ts"], cancelled["note"], cancel["id"]))
        await store.conn.commit()
        if not cur.rowcount:  # closed or cancelled (or removed) by another process since journal_get
            again = await store.journal_get(cancel["id"])
            return {"error": "not_open", "status": again["status"]} if again else {"error": "not_found"}
        progress["written"] = True
        return {"row": cancelled}
    finally:
        await store.aclose()


def handle(text: str, db_path: Path, now: float) -> str:
    """Parse, cancel and answer. Never raises: a bad command gives the format, a store failure a short reason."""
    parsed = parse_cancel_command(text)
    if not parsed["ok"]:
        return error_reply(parsed["error"] or "", parsed.get("field"))
    cancel = parsed["cancel"]
    if not db_path.exists():
        return f"❌ Угоду не скасовано: файла журналу {_h(db_path)} немає (його створює MCP-сервер)."
    progress: dict[str, bool] = {}
    try:
        res = asyncio.run(cancel_trade(db_path, cancel, now, progress))
    except Exception as e:  # noqa: BLE001 - the owner gets a reason, the bridge keeps running
        if progress.get("written"):
            return (
                f"✅ Скасовано в журналі: угода #{cancel['id']}. Після запису збій бази ({_h(type(e).__name__)}), "
                "відповідь не зібрано; стан дивіться в панелі. Повторно не скасовуйте."
            )
        return f"❌ Угоду не скасовано: збій бази ({_h(type(e).__name__)}). Спробуйте ще раз або journal_close у Claude."
    err = res.get("error")
    if err == "not_found":
        return f"❌ Угоду не скасовано: угоди #{cancel['id']} у журналі немає."
    if err == "bot_row":
        return f"❌ Угоду не скасовано: угода #{cancel['id']} — це позиція демо-бота, нею керує бот; журнал не змінено."
    if err == "not_open":
        return f"❌ Угоду не скасовано: угода #{cancel['id']} {_h(STATUS_WORD.get(res['status'], res['status']))}; журнал не змінено."
    try:
        return build_reply(res["row"])
    except Exception as e:  # noqa: BLE001 - the row is cancelled; only the reply text failed
        return f"✅ Скасовано в журналі: угода #{cancel['id']}. Відповідь не вдалося зібрати ({_h(type(e).__name__)})."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text", required=True, help="the owner's /cancel message")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT, help="SQLite file (default: the dashboard's journal DB)")
    a = ap.parse_args(argv)
    print(handle(a.text, a.db, time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
