"""Weekly summary of the owner's rule violations to Telegram: the last completed ISO week and this one.

    python scripts/rules_weekly_tg.py                 # send to the group's alerts topic
    python scripts/rules_weekly_tg.py --print         # print the Telegram HTML and the button, send nothing
    python scripts/rules_weekly_tg.py --out DIR       # write DIR/rules.html, send nothing
    python scripts/rules_weekly_tg.py --db PATH       # another SQLite file (default: the dashboard's journal DB)

Read-only and offline: the rows come from the ``journal`` table (schema v3) over a ``mode=ro`` SQLite connection,
never from the network. Only the owner's real trades count (no airdrop, no *-paper rows: dashboard.journal_kind), and
the numbers are analytics.rules_check.weekly_violations with its default thresholds (deposit $10 000), the function
the dashboard's «Порушення правил за тиждень» block mirrors with a tested stdlib copy, so the message and the panel
agree. Rules 1, 2, 4, 5 and 8; the week is the ISO week (UTC) of the opening. As on the panel, a rule 4 flag judged on
the close time (a row without an opening time) is approximate: shown apart and left out of the "violated" count and
its P&L. Short: a title, up to five lines, one caveat sentence (at most 8 lines).

A missing database, a store without the journal table, an empty journal or no real trades give a short explanation,
never an error. Any text is HTML-escaped. The «Журнал» button is forecast_weekly_tg.journal_buttons (/journal under
the dashboard URL). Sending goes through scripts/tg_send.py (token never read here). Scheduled by
deploy/user-units/tradedesk-rules-weekly.* (Monday 09:00 Europe/Kyiv) while the flag file of
scripts/server/27-rules-weekly-timer.sh exists. Nothing here trades: it only reads the journal.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import forecast_weekly_tg as fw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import rules_check as rc  # noqa: E402

CAVEAT = (
    "Не порада: звіт лише читає журнал і нічого не торгує; правила 1 і 5 потребують стопу (в імпорті з бірж його немає), "
    "депозит — сьогоднішні $10 000 для всіх тижнів, повна таблиця — на панелі."
)
RULE_NAMES = dict(db.RULES_WEEK_HEAD)  # {1: "1 · ризик ≤ 1 %", ...}: the panel's column names
_h = ab._h  # Telegram HTML escaping
journal_buttons = fw.journal_buttons  # the same «Журнал» button as the forecast summary


def load_journal(path: Path) -> tuple[list[dict] | None, str | None]:
    """(rows, None) from table journal over a read-only connection (newest first, at most dashboard.JOURNAL_MAX_ROWS),
    or (None, reason): 'missing' (no file), 'no_table' (a store before schema v3), 'unreadable: …'. Never raises."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'journal'").fetchone() is None:
            return None, "no_table"
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM journal ORDER BY COALESCE(closed_ts, opened_ts) DESC, id DESC LIMIT ?", (db.JOURNAL_MAX_ROWS,)
            )
        ]
        return rows, None
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def real_trades(rows: list[dict]) -> list[dict]:
    """The owner's real trades, as the dashboard passes them to its rules block: no airdrop, no *-paper rows."""
    return [t for t in rows if db.journal_kind(db.journal_tags(t.get("tags")))[0] == "real"]


def _monday(week: str) -> date:
    y, w = week.split("-W")
    return date.fromisocalendar(int(y), int(w), 1)


def _rules_part(w: dict) -> str:
    """'Найчастіше: 8 · опціони — 3, ….' over exact violations (rule 4 on the close time left out), most first."""
    top = sorted(((w["rules"][n]["violated"], n) for n in rc.RULES if w["rules"][n]["violated"]), key=lambda x: (-x[0], x[1]))
    return "Найчастіше: " + ", ".join(f"{RULE_NAMES.get(n, n)} — {v}" for v, n in top) + "." if top else ""


def _approx_part(w: dict) -> str:
    n = w["n_violating_approx"]
    if not n:
        return ""
    pnl = w["pnl_violating_approx"]
    return (
        f"Ще {n} — лише наближене правило 4 (без часу відкриття, оцінено за часом закриття)"
        + (f", P&L {db._usd(pnl, True)}" if pnl is not None else "")
        + ": у «точно порушили» не входять."
    )


def _week_lines(label: str, span: str, w: dict | None) -> list[str]:
    """One or two lines for a week: the counts, then the most broken rules and the approximate rule 4 flags."""
    if w is None or not w["n_trades"]:
        return [_h(f"{label} ({span}): угод не відкривали.")]
    k, pnl = w["n_violating"], w["pnl_violating"]
    head = f"{label} ({span}): перевірено угод {w['n_trades']}, " + (
        f"точно порушили хоч одне правило {k}" + (f", їхній P&L {db._usd(pnl, True)}" if pnl is not None else "") + "."
        if k
        else "точних порушень немає."
    )
    more = " ".join(x for x in (_rules_part(w), _approx_part(w)) if x)
    return [_h(head)] + ([_h(more)] if more else [])


def build_summary(rows: list[dict] | None, now: float, err: str | None = None) -> str:
    """The weekly rules summary as Telegram HTML: title, up to five lines, one caveat (at most 8 lines). Pure (no I/O,
    time passed in); never raises. ``rows`` = all journal rows (None with ``err`` from load_journal); the real trades
    are picked here."""
    head = f"<b>Правила власника за тиждень · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{ab._tz()}</b>"  # noqa: UP017
    tail = ["", f"<i>{_h(CAVEAT)}</i>"]
    try:
        if rows is None:
            why = {
                "missing": "файла бази ще немає (його створюють MCP-сервер і колектор).",
                "no_table": "таблиці <code>journal</code> у базі ще немає: вона з'являється, коли MCP-сервер уперше відкриває базу.",
            }.get(err or "", f"базу не вдалося прочитати ({_h(err or 'невідома причина')}).")
            return ab._fit([head, f"<b>Звіту немає:</b> {why}"], tail)
        if not rows:
            return ab._fit(
                [head, "<b>Угод ще немає:</b> звіт з'явиться з першою угодою (<code>journal_add</code> або імпорт з бірж)."], tail
            )
        real = real_trades(rows)
        if not real:
            return ab._fit([head, _h(f"Реальних угод немає: у журналі лише паперові чи airdrop-рядки ({len(rows)}).")], tail)
        weeks = {w["week"]: w for w in rc.weekly_violations(real, now_ts=now)}
        if not weeks:
            return ab._fit([head, _h("Відкритих чи закритих угод з часом відкриття немає: перевіряти нема що.")], tail)
        this_wk, last_wk = rc.iso_week(now), rc.iso_week(now - 7 * rc.DAY_S)
        last_mon, this_mon = _monday(last_wk), _monday(this_wk)
        body = [head]
        body += _week_lines(f"Минулий тиждень {last_wk}", f"{last_mon:%d.%m}–{last_mon + timedelta(days=6):%d.%m}", weeks.get(last_wk))
        body += _week_lines(f"Цей тиждень {this_wk}", f"з {this_mon:%d.%m}", weeks.get(this_wk))
        if not any(weeks.get(x, {}).get("n_trades") for x in (last_wk, this_wk)):
            past = [w for wk, w in sorted(weeks.items(), reverse=True) if wk < last_wk and w["n_trades"]]
            if past:
                p = past[0]
                body.append(
                    _h(f"Останній тиждень з угодами — {p['week']}: перевірено угод {p['n_trades']}, точно порушили {p['n_violating']}.")
                )
        return ab._fit(body, tail)
    except Exception as e:  # noqa: BLE001 - an odd row must not drop the message
        return ab._fit([head, _h(f"Звіт не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")], tail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=db.JOURNAL_DB, help="SQLite file (default: the dashboard's journal DB)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write rules.html into this directory, send nothing")
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
            (a.out / "rules.html").write_text(text + "\n", encoding="utf-8")
        return 0
    return 0 if tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons) else 1


if __name__ == "__main__":
    sys.exit(main())
