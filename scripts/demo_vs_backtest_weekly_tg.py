"""Weekly «demo vs backtest» summary to Telegram: every closed demo trade replayed by the backtest of its setup.

    python scripts/demo_vs_backtest_weekly_tg.py                 # send to the group's demo topic
    python scripts/demo_vs_backtest_weekly_tg.py --print         # print the Telegram HTML and the button, send nothing
    python scripts/demo_vs_backtest_weekly_tg.py --out DIR       # write DIR/demo_vs_backtest.html, send nothing
    python scripts/demo_vs_backtest_weekly_tg.py --db PATH       # another SQLite file (default: the demo trader's DB)

BACKLOG 0.5 (owner 23.10, item a), slice 2: the short weekly message of scripts/demo_vs_backtest.py. The replay is that
script's ``run`` (closed ``demo_trade`` rows over a ``mode=ro`` connection; public candles through ``rt.exchanges``, so
core rate limits, cache and health apply; a failed fetch gives the verdict "немає даних", never an error) and the
numbers are analytics.demo_vs_backtest.summary: the running total from the first demo trade (a trade the backtest had
not resolved last week counts once it is) and the trades closed in the last 7 days. Below
analytics.demo_vs_backtest.MIN_TRADES decided trades the text says "замало для висновку". Short: a title, up to five
lines, one caveat sentence (at most 8 lines).

A missing database, a store without ``demo_trade`` (before schema v19) or no closed demo trades give a short
explanation, never an error. Setup names and notes are untrusted: control / format characters are stripped, the text is
cut and HTML-escaped. The «Журнал» button is forecast_weekly_tg.journal_buttons (/journal, where the demo trader's
closed trades are listed). Sending goes through scripts/tg_send.py (token never read here) into the ``demo`` topic,
where the demo trader posts its entries and exits (TELEGRAM_TOPIC_DEMO, scripts/telegram_topics.py). Scheduled by
deploy/user-units/tradedesk-demo-vs-backtest-weekly.* (Sunday 20:30 Europe/Kyiv) while the flag file of
scripts/server/29-demo-vs-backtest-weekly-timer.sh exists. Nothing here trades: no keys, no orders, read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import demo_vs_backtest as dv  # noqa: E402
import forecast_weekly_tg as fw  # noqa: E402
import journal_weekly_tg as jw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import demo_vs_backtest as dvb  # noqa: E402

WEEK_MS = 7 * dv.DAY_MS
MAX_SETUPS = 4
CAVEAT = (
    "Не порада: звірка лише читає демо-журнал і публічні свічки Bybit, нічого не торгує; R бектесту — смуга (порядок цін "
    f"у годині невідомий), угоди до 22.10 переносили стоп за +30 % ROE; менше {dvb.MIN_TRADES} звірених угод — опис, не висновок."
)
_h = ab._h  # Telegram HTML escaping
clean = jw.clean  # untrusted text: control / format characters dropped, cut, escaped
journal_buttons = fw.journal_buttons


def probe(path: Path) -> str | None:
    """None when table demo_trade can be read; else 'missing' (no file), 'no_table' (a store before schema v19) or
    'unreadable: …'. Read-only connection; never raises."""
    try:
        if not path.exists():
            return "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return f"unreadable: {type(e).__name__}"
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'demo_trade'").fetchone() is None:
            return "no_table"
        return None
    except (sqlite3.Error, OSError) as e:
        return f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def collect(path: Path, venue: str, now: float) -> tuple[list[dvb.Replay] | None, str | None, list[str]]:
    """(replays, error, notes): every closed demo trade replayed by scripts/demo_vs_backtest.run. Never raises."""
    err = probe(path)
    if err:
        return None, err, []
    try:
        rows, _total, _week, _week_ms, notes = asyncio.run(dv.run(str(path), 0, 7, venue, int(now * 1000)))
    except Exception as e:  # noqa: BLE001 - the message must still go out
        return None, f"replay failed: {type(e).__name__}", []
    return rows, None, notes


def _r(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}".replace("-", "−")


def _day(ms: int) -> str:
    return db.kyiv(datetime.fromtimestamp(ms / 1000, UTC), "%d.%m")


def _counts(s: dict[str, Any]) -> str:
    c = s["counts"]
    out = f"закрито {s['n']}, звірено {s['decided']}: збіг {c['match']}, немає сигналу {c['no_signal']}, розбіжність {c['mismatch']}"
    open_ = [
        f"{label} {c[k]}"
        for k, label in (("pending", "бектест ще відкритий"), ("no_data", "немає даних"), ("unsupported", "не відтворюється"))
        if c[k]
    ]
    return out + (f"; не вирішено: {', '.join(open_)}" if open_ else "")


def _verdict_line(s: dict[str, Any]) -> str:
    if s["enough"]:
        out = f"Висновок: {s['conclusion']}."
    else:
        out = f"Висновок: замало для висновку (звірено {s['decided']}, потрібно ≥ {s['min_n']}), числа — лише опис."
    if s["n_r"]:
        out += (
            f" Середнє R в одиницях ризику бектесту (угод з обома R: {s['n_r']}): демо {_r(s['mean_r_demo_bt'])}, "
            f"бектест {_r(s['mean_r_bt'])}, різниця {_r(s['mean_r_diff'])}."
        )
    if s["n_entry"]:
        out += f" Вхід демо проти закриття свічки: {_r(s['mean_entry_edge_r'])}R у середньому (+ — краще)."
    return _h(out)


def _setups_line(s: dict[str, Any]) -> str:
    items = sorted(s["per_setup"].items(), key=lambda kv: (-sum(kv[1].values()), str(kv[0])))
    parts = []
    for setup, c in items[:MAX_SETUPS]:
        undecided = c["pending"] + c["no_data"] + c["unsupported"]
        parts.append(
            f"{clean(setup, 12)}: збіг {c['match']}, немає сигналу {c['no_signal']}, розбіжність {c['mismatch']}"
            + (f", не вирішено {undecided}" if undecided else "")
        )
    more = len(items) - len(parts)
    out = "За сетапами: " + "; ".join(parts) + (f"; ще {more}" if more else "") + "."
    if s["mismatch_reasons"]:
        reasons = ", ".join(f"{dv.REASON_UA.get(k, k)} {v}" for k, v in sorted(s["mismatch_reasons"].items()))
        out += f" Причини розбіжностей: {_h(reasons)}."
    return out


def build_summary(rows: list[dvb.Replay] | None, now: float, err: str | None = None, notes: list[str] | None = None) -> str:
    """The weekly «demo vs backtest» summary as Telegram HTML: title, up to five lines, one caveat (at most 8 lines).
    Pure (no I/O, time passed in); never raises. ``rows`` = the replays of every closed demo trade (None with ``err``
    from collect); ``notes`` = fetch failures and warnings of the replay (only the count and the first are shown)."""
    head = f"<b>Демо проти бектесту за тиждень · {_h(db.kyiv(datetime.fromtimestamp(now, UTC)))}{ab._tz()}</b>"
    tail = ["", f"<i>{_h(CAVEAT)}</i>"]
    try:
        if rows is None:
            why = {
                "missing": "файла бази ще немає (його створюють MCP-сервер, колектор і демо-трейдер).",
                "no_table": "таблиці <code>demo_trade</code> ще немає: база до схеми v19 (з'явиться, коли демо-трейдер "
                "або MCP-сервер відкриють її новим кодом).",
            }.get(err or "", f"звірку не вдалося виконати ({_h(err or 'невідома причина')}).")
            return ab._fit([head, f"<b>Звірки немає:</b> {why}"], tail)
        if not rows:
            return ab._fit([head, "<b>Закритих демо-угод ще немає:</b> звірка з'явиться з першою закритою угодою демо-трейдера."], tail)
        now_ms = int(now * 1000)
        week_ms = now_ms - WEEK_MS
        total = dvb.summary(rows)
        week_rows = [x for x in rows if x.closed_ts is not None and x.closed_ts >= week_ms]
        first = min((int(x.closed_ts) for x in rows if x.closed_ts is not None), default=now_ms)
        span = f"{_day(week_ms)}–{_day(now_ms)}"
        body = [
            head,
            _h(f"Від першої демо-угоди (з {_day(first)}): {_counts(total)}."),
            _verdict_line(total),
            _setups_line(total),
            _h(f"За тиждень {span}: {_counts(dvb.summary(week_rows))}.")
            if week_rows
            else _h(f"За тиждень {span}: демо-угод не закривали."),
        ]
        if notes:
            body.append(f"Не все завантажилось ({len(notes)}), перше: {clean(notes[0], 80)}. Такі угоди мають вердикт «немає даних».")
        return ab._fit(body, tail)
    except Exception as e:  # noqa: BLE001 - an odd row must not drop the message
        return ab._fit([head, _h(f"Підсумок не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")], tail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=Path(dv.DEFAULT_DB), help="SQLite file (default: the demo trader's DB)")
    ap.add_argument("--venue", default="bybit", help="ccxt id of the candles (default bybit)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write demo_vs_backtest.html into this directory, send nothing")
    ap.add_argument("--topic", default="demo", help="forum topic (default: demo, the demo trader's)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    now = time.time()
    rows, err, notes = collect(a.db, a.venue, now)
    text = build_summary(rows, now, err, notes)
    buttons = journal_buttons()
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            links = ", ".join(f"{label} {url}" for label, url in buttons) or f"none (no ${ab.PANEL_URL_ENV} and no {ab.DASHBOARD_URL_PATH})"
            print(f"\n[buttons: {links}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "demo_vs_backtest.html").write_text(text + "\n", encoding="utf-8")
        return 0
    return 0 if tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons) else 1


if __name__ == "__main__":
    sys.exit(main())
