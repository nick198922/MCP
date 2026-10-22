"""Close an open trade in the local journal from the owner's Telegram command and answer with its P&L and R.

    uv run python scripts/journal_close_tg.py --text "/close 12 86000 fees 4.5 note took profit before CPI"
    uv run python scripts/journal_close_tg.py --text "..." --db PATH      # another SQLite file

Called by scripts/tg_bridge.py for "/close …" (the bridge runs on the system python3, so it starts this script with
uv in the project's environment); the bridge has already checked that the message is the owner's, in the group and
topic it serves, and sends the printed Telegram HTML back. The text is untrusted: analytics.close_command parses it
strictly into the journal_close fields (id, exit, note) plus fees. The close goes the journal_close tool's way:
Store.init, journal_get, only a row with status 'open', P&L, R and outcome from analytics.journal (pnl, r_multiple,
outcome_of), then the same column update as journal_update_close but conditional (``AND status = 'open'``, rowcount
checked), so a row closed meanwhile by the demo trader or journal_close is never overwritten. Two differences, both
written into the row: fees (USD) are subtracted from the P&L and noted, and an option row gets no R (its stop is a
price of the underlying, not of the premium). Rows mirrored by the demo trader (tags auto / demo-paper / bybit-demo)
are refused: the bot closes them. An exit of 0 is accepted only for an option (expired worthless). The dashboard's
journal DB is used; a missing file is refused rather than created. Nothing here places, changes or cancels an order or
touches a limit: it only updates one journal row. Never raises: any failure is a short message.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from journal_trade_tg import DB_DEFAULT, SIDE_UA, THESIS_SHOWN, _h, fmt, usd

from tradedesk.analytics import rules_check as rc
from tradedesk.analytics.close_command import MAX_NOTE, parse_close_command
from tradedesk.analytics.journal import outcome_of, r_multiple
from tradedesk.analytics.journal import pnl as pnl_fn
from tradedesk.store.db import Store

USAGE = (
    "Формат: <code>/close 12 86000 fees 4.5 note чому вийшов</code>\n"
    "Обов'язково: id угоди (з відповіді на /trade) і ціна виходу. Необов'язково: fees — комісії в $ за вхід і вихід "
    "(P&amp;L у журналі — за їх вирахуванням), note — нотатка до кінця повідомлення. Опціон: ціна — $ за 1 монету "
    "(менше за 1 — лише з $, напр. $0.4); опціон згорів: <code>/close 12 0</code>."
)
ERRORS_UA = {
    "empty": "порожнє повідомлення",
    "not_close_command": "повідомлення має починатися з /close",
    "too_long": "повідомлення задовге",
    "missing_id": "немає id угоди",
    "bad_id": "id — ціле додатне число, напр. 12 або #12",
    "missing_exit": "немає ціни виходу",
    "bad_number": "число має бути додатним, на кшталт 86000, 0.1, 0,1 або 86k (не 86,000)",
    "missing_value": "після слова немає числа",
    "unknown_word": "незнайоме слово: дозволено fees, note",
    "duplicate": "поле вказано двічі",
    "note_too_long": f"нотатка довша за {MAX_NOTE} знаків",
    "option_exit_in_coin": "схоже на премію в BTC/ETH: вкажіть $ за 1 монету, напр. 1500 або $0.4",
    "bad_side": "у цієї угоди напрям не long і не short — закрийте її через journal_close у Claude",
}
# execution/demo_trader.py mirrors its demo-account trades into the same journal with these tags and closes them itself
BOT_TAGS = frozenset({"auto", "demo-paper", "bybit-demo"})
# journal_update_close's columns, but only while the row is still open (that method has no status guard)
CLOSE_SQL = (
    "UPDATE journal SET closed_ts = ?, status = 'closed', exit = ?, outcome = ?, pnl = ?, r_multiple = ?, note = ? "
    "WHERE id = ? AND status = 'open'"
)
STATUS_WORD = {"closed": "уже закрита", "cancelled": "скасована"}
OUTCOME_UA = {"win": "у плюс", "loss": "у мінус", "break_even": "в нуль"}
CAVEAT = "Лише запис у локальний журнал: жодних ордерів, ліміти не змінюються. Позицію на біржі закривайте самі."


def signed_usd(v: float) -> str:
    return ("+" if v > 0 else "-" if v < 0 else "") + usd(abs(v))


def error_reply(code: str, field: str | None = None) -> str:
    why = ERRORS_UA.get(code, "не вдалося розібрати")
    return f"❌ Угоду не закрито: {_h(why)}" + (f" (поле {_h(field)})" if field else "") + ".\n" + USAGE


def stored_note(fees: float | None, note: str | None) -> str | None:
    """The journal note: the owner's text, preceded by the fees when given (the stored P&L is net of them)."""
    parts = ([f"fees ${fmt(fees)} (pnl net of fees)"] if fees is not None else []) + ([note] if note else [])
    return "; ".join(parts) or None


def build_reply(row: dict[str, Any], gross: float, fees: float | None, note: str | None) -> str:
    """Telegram HTML for a closed trade: what was closed, net P&L, outcome and R. Pure."""
    is_opt = rc.option_expiry_ts(row["symbol"]) is not None
    net = float(row["pnl"])
    lines = [
        f"<b>✅ Закрито в журналі: угода #{row['id']}</b>",
        " · ".join(
            [_h(row["symbol"]), SIDE_UA.get(row["side"], _h(row["side"])), fmt(float(row["size"]))]
            + [f"вхід {fmt(float(row['entry']))} → вихід {fmt(float(row['exit']))}"]
        ),
    ]
    # Telegram HTML: a bare '&' breaks the whole message's markup, hence '&amp;'
    pnl_line = f"P&amp;L: {signed_usd(net)} ({OUTCOME_UA.get(row['outcome'], _h(row['outcome']))})"
    if fees is None:
        pnl_line += " — без комісій (не вказано fees)"
    elif fees > 0:
        pnl_line += f" — за вирахуванням комісії {usd(fees)}; без неї {signed_usd(gross)}"
    lines.append(pnl_line + ".")
    r_val, stop = row.get("r_multiple"), row.get("stop")
    if r_val is not None:
        lines.append(f"R: {r_val:+.2f} (стоп {fmt(float(stop))}; за ціною, без комісії).")
    elif is_opt:
        why = "для опціону стоп стосується ціни базового активу, не премії"
        if row["side"] == "long":
            why += f"; результат до сплаченої премії {net / (float(row['size']) * float(row['entry'])):+.0%}"
        lines.append(f"R: не рахую — {_h(why)}.")
    elif stop is None:
        lines.append("R: невідомо — стоп не записано.")
    else:
        lines.append("R: невідомо — стоп записано не з того боку від входу.")
    if note:
        shown = " ".join(note.split())
        lines.append("Нотатка: " + _h(shown if len(shown) <= THESIS_SHOWN else shown[: THESIS_SHOWN - 1] + "…"))
    lines += ["", f"<i>{_h(CAVEAT)}</i>"]
    return "\n".join(lines)


def is_bot_row(row: dict[str, Any]) -> bool:
    """True for a journal row the demo trader wrote (and will close itself)."""
    tags = row.get("tags")
    return isinstance(tags, list) and any(isinstance(t, str) and t in BOT_TAGS for t in tags)


async def close_trade(db_path: Path, close: dict[str, Any], exit_raw: str, now: float, progress: dict[str, bool]) -> dict[str, Any]:
    """Close as journal_close does (Store.init, journal_get, status must be 'open', analytics.journal for P&L / R /
    outcome, then its column update, here only while the row is still open). {'error': code, ...} when the row cannot
    be closed, else {'row': the closed row, 'gross'}. Sets progress['written'] once the update is committed, so the
    caller can tell a failure after the write (e.g. in aclose) from one before it."""
    store = Store(db_path)
    try:
        await store.init()
        row = await store.journal_get(close["id"])
        if row is None:
            return {"error": "not_found"}
        if is_bot_row(row):
            return {"error": "bot_row"}
        if row["status"] != "open":
            return {"error": "not_open", "status": row["status"]}
        if row["side"] not in ("long", "short"):
            return {"error": "bad_side"}
        is_opt = rc.option_expiry_ts(row["symbol"]) is not None
        exit_ = float(close["exit"])
        if exit_ <= 0 and not is_opt:
            return {"error": "bad_number"}  # only an option can expire worthless
        if is_opt and 0 < exit_ < 1 and not exit_raw.startswith("$"):
            return {"error": "option_exit_in_coin"}
        gross = pnl_fn(row["side"], float(row["size"]), float(row["entry"]), exit_)
        net = gross - (close["fees"] or 0.0)
        r_val = None if is_opt else r_multiple(row["side"], float(row["entry"]), row.get("stop"), exit_)
        closed = {
            **row,
            "closed_ts": int(now),
            "status": "closed",
            "exit": exit_,
            "outcome": outcome_of(net),
            "pnl": net,
            "r_multiple": r_val,
            "note": stored_note(close["fees"], close["note"]),
        }
        cur = await store.conn.execute(
            CLOSE_SQL,
            (closed["closed_ts"], exit_, closed["outcome"], net, r_val, closed["note"], close["id"]),
        )
        await store.conn.commit()
        if not cur.rowcount:  # closed (or removed) by another process since journal_get
            again = await store.journal_get(close["id"])
            return {"error": "not_open", "status": again["status"]} if again else {"error": "not_found"}
        progress["written"] = True
        return {"row": closed, "gross": gross}
    finally:
        await store.aclose()


def handle(text: str, db_path: Path, now: float) -> str:
    """Parse, close and answer. Never raises: a bad command gives the format, a store failure a short reason."""
    parsed = parse_close_command(text)
    if not parsed["ok"]:
        return error_reply(parsed["error"] or "", parsed.get("field"))
    close = parsed["close"]
    if not db_path.exists():
        return f"❌ Угоду не закрито: файла журналу {_h(db_path)} немає (його створює MCP-сервер)."
    progress: dict[str, bool] = {}
    try:
        res = asyncio.run(close_trade(db_path, close, parsed["exit_raw"] or "", now, progress))
    except Exception as e:  # noqa: BLE001 - the owner gets a reason, the bridge keeps running
        if progress.get("written"):
            return (
                f"✅ Закрито в журналі: угода #{close['id']}. Після запису збій бази ({_h(type(e).__name__)}), "
                "відповідь не зібрано; P&amp;L дивіться в панелі. Повторно не закривайте."
            )
        return f"❌ Угоду не закрито: збій бази ({_h(type(e).__name__)}). Спробуйте ще раз або journal_close у Claude."
    err = res.get("error")
    if err == "not_found":
        return f"❌ Угоду не закрито: угоди #{close['id']} у журналі немає."
    if err == "bot_row":
        return f"❌ Угоду не закрито: угода #{close['id']} — це позиція демо-бота, вона закривається ботом; журнал не змінено."
    if err == "not_open":
        return f"❌ Угоду не закрито: угода #{close['id']} {_h(STATUS_WORD.get(res['status'], res['status']))}; журнал не змінено."
    if err:
        return error_reply(err, "exit" if err in ("option_exit_in_coin", "bad_number") else None)
    try:
        return build_reply(res["row"], res["gross"], close["fees"], close["note"])
    except Exception as e:  # noqa: BLE001 - the row is closed; only the reply text failed
        return f"✅ Закрито в журналі: угода #{close['id']}. Відповідь не вдалося зібрати ({_h(type(e).__name__)})."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text", required=True, help="the owner's /close message")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT, help="SQLite file (default: the dashboard's journal DB)")
    a = ap.parse_args(argv)
    print(handle(a.text, a.db, time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
