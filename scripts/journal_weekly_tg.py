"""Weekly review of the owner's real journal trades to Telegram: the last completed ISO week and this one.

    python scripts/journal_weekly_tg.py                 # send to the group's alerts topic
    python scripts/journal_weekly_tg.py --print         # print the Telegram HTML and the button, send nothing
    python scripts/journal_weekly_tg.py --out DIR       # write DIR/journal.html, send nothing
    python scripts/journal_weekly_tg.py --db PATH       # another SQLite file (default: the dashboard's journal DB)

Read-only and offline: the rows come from the ``journal`` table over a ``mode=ro`` SQLite connection
(rules_weekly_tg.load_journal), never from the network. Only the owner's real trades count: no airdrop and no *-paper
rows (dashboard.journal_kind, as the panel's summary), and no rows of the demo trader (tags auto / demo-paper /
bybit-demo, journal_close_tg.BOT_TAGS). The numbers are analytics.journal.weekly_review by the ISO week (UTC) of the
close: trades closed, win share, P&L in total and per trade (as the journal recorded it), the average R over the rows
with a recorded R (a stop), else "R невідомо", the best and the worst trade, and a split by source (the panel's venue
label). Under 30 closed trades a week is marked "замало для висновку". Short: a title, up to five lines, one caveat
sentence (at most 8 lines).

A missing database, a store without the journal table, an empty journal or no real trades give a short explanation,
never an error. Symbols are untrusted: control, format (bidi, zero-width) and private-use characters are stripped,
the text is cut and HTML-escaped; notes and theses are not shown. The «Журнал» button is
forecast_weekly_tg.journal_buttons (/journal under the dashboard URL). Sending goes through scripts/tg_send.py (token
never read here). Scheduled by deploy/user-units/tradedesk-journal-weekly.* (Sunday 20:00 Europe/Kyiv) while the flag
file of scripts/server/28-journal-weekly-timer.sh exists. Nothing here trades: it only reads the journal.
"""

from __future__ import annotations

import argparse
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import rules_weekly_tg as rw  # noqa: E402
import tg_send  # noqa: E402
from journal_close_tg import BOT_TAGS  # noqa: E402

from tradedesk.analytics import journal as jr  # noqa: E402
from tradedesk.analytics import rules_check as rc  # noqa: E402

CAVEAT = (
    "Не порада: розбір лише читає журнал і не ставить жодних ордерів; P&L — як записано в журналі, "
    f"а менше {jr.SMALL_SAMPLE} угод за тиждень — опис, не доказ переваги чи її відсутності."
)
SYMBOL_SHOWN = 24  # characters of a symbol in the message
MANUAL = "вручну"  # the panel's "без біржі": journal_add and /trade rows
MAX_SOURCES = 4
_h = ab._h  # Telegram HTML escaping
journal_buttons = rw.journal_buttons  # the same «Журнал» button as the forecast and rules summaries
load_journal = rw.load_journal


def real_trades(rows: list[dict]) -> list[dict]:
    """The owner's real trades with their source label: rules_weekly_tg.real_trades (no airdrop, no *-paper) minus
    the demo trader's rows; ``source`` = the panel's venue label ('Bybit', 'Bybit опціони', …, 'вручну')."""
    out = []
    for t in rw.real_trades(rows):
        tags = db.journal_tags(t.get("tags"))
        if BOT_TAGS.intersection(tags):
            continue
        label = db.journal_kind(tags)[1]
        out.append({**t, "source": MANUAL if label == "без біржі" else label})
    return out


def clean(text: object, limit: int = SYMBOL_SHOWN) -> str:
    """Untrusted text for one line: control / format / private-use / unassigned characters dropped, whitespace
    collapsed, cut to ``limit`` characters, HTML-escaped."""
    s = "".join(ch if not unicodedata.category(ch).startswith("C") else " " for ch in str(text or ""))
    s = " ".join(s.split()) or "?"
    return _h(s if len(s) <= limit else s[: limit - 1] + "…")


def _trade(t: dict) -> str:
    return f"{clean(t.get('symbol'))} {_h(db._usd(t['pnl'], True))} (#{_h(t.get('id'))})"


def _r_part(w: dict) -> str:
    if not w.get("r_count"):
        return "R невідомо (стопу в цих угодах немає)"
    return f"середній R {w['avg_r']:+.2f} за {w['r_count']} з {w['closed_trades']} угод зі стопом"


def _week_lines(label: str, span: str, w: dict) -> list[str]:
    """One or two lines for a week: the counts, then the best / worst trade and the split by source."""
    n = w["closed_trades"]
    if not n:
        return [_h(f"{label} ({span}): угод не закривали.")]
    head = _h(
        f"{label} ({span}): закрито {n}" + (" (замало для висновку)" if w["small_sample"] else "") + f", у плюс {w['wins']} "
        f"({w['win_rate'] * 100:.0f} %), P&L {db._usd(w['total_pnl'], True)}, на угоду {db._usd(w['expectancy_per_trade'], True)}; "
        f"{_r_part(w)}."
    )
    ends = f"Єдина: {_trade(w['best'])}." if n == 1 else f"Найкраща: {_trade(w['best'])}, найгірша: {_trade(w['worst'])}."
    srcs = w["by_source"][:MAX_SOURCES]
    more = len(w["by_source"]) - len(srcs)
    split = ", ".join(f"{_h(s['source'])} {s['n']} ({_h(db._usd(s['pnl'], True))})" for s in srcs) + (f", ще {more}" if more else "")
    return [head, f"{ends} За джерелом: {split}."]


def build_summary(rows: list[dict] | None, now: float, err: str | None = None) -> str:
    """The weekly journal review as Telegram HTML: title, up to five lines, one caveat (at most 8 lines). Pure (no I/O,
    time passed in); never raises. ``rows`` = all journal rows (None with ``err`` from load_journal); the real trades
    are picked here."""
    head = f"<b>Журнал угод за тиждень · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{ab._tz()}</b>"  # noqa: UP017
    tail = ["", f"<i>{_h(CAVEAT)}</i>"]
    try:
        if rows is None:
            why = {
                "missing": "файла бази ще немає (його створюють MCP-сервер і колектор).",
                "no_table": "таблиці <code>journal</code> у базі ще немає: вона з'являється, коли MCP-сервер уперше відкриває базу.",
            }.get(err or "", f"базу не вдалося прочитати ({_h(err or 'невідома причина')}).")
            return ab._fit([head, f"<b>Розбору немає:</b> {why}"], tail)
        if not rows:
            return ab._fit(
                [head, "<b>Угод ще немає:</b> розбір з'явиться з першою закритою угодою (<code>/trade</code>, імпорт з бірж)."], tail
            )
        real = real_trades(rows)
        if not real:
            return ab._fit(
                [head, _h(f"Реальних угод немає: у журналі лише паперові, airdrop-рядки чи угоди демо-бота ({len(rows)}).")], tail
            )
        rev = jr.weekly_review(real, now_ts=now)
        last, this = rev["last"], rev["this"]
        last_mon = datetime.fromtimestamp(last["start_ts"], timezone.utc).date()  # noqa: UP017
        this_mon = datetime.fromtimestamp(this["start_ts"], timezone.utc).date()  # noqa: UP017
        body = [head]
        body += _week_lines(
            f"Минулий тиждень {rc.iso_week(last['start_ts'])}", f"{last_mon:%d.%m}–{last_mon + timedelta(days=6):%d.%m}", last
        )
        body += _week_lines(f"Цей тиждень {rc.iso_week(this['start_ts'])}", f"з {this_mon:%d.%m}", this)
        p = rev["latest"]
        if not last["closed_trades"] and not this["closed_trades"] and p:
            body.append(
                _h(
                    f"Останній тиждень із закритими угодами — {rc.iso_week(p['start_ts'])}: закрито {p['closed_trades']}, "
                    f"P&L {db._usd(p['total_pnl'], True)}."
                )
            )
        return ab._fit(body, tail)
    except Exception as e:  # noqa: BLE001 - an odd row must not drop the message
        return ab._fit([head, _h(f"Розбір не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")], tail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=db.JOURNAL_DB, help="SQLite file (default: the dashboard's journal DB)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write journal.html into this directory, send nothing")
    ap.add_argument("--topic", default="alerts", help="forum topic (default: alerts)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    rows, err = load_journal(a.db)
    text = build_summary(rows, time.time(), err)
    buttons = journal_buttons()
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            links = ", ".join(f"{label} {url}" for label, url in buttons) or f"none (no ${ab.PANEL_URL_ENV} and no {ab.DASHBOARD_URL_PATH})"
            print(f"\n[buttons: {links}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "journal.html").write_text(text + "\n", encoding="utf-8")
        return 0
    return 0 if tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons) else 1


if __name__ == "__main__":
    sys.exit(main())
