"""Weekly summary of the forecast log to Telegram: sample size, Brier against the references and the RV30 model.

    python scripts/forecast_weekly_tg.py                 # send to the group's alerts topic
    python scripts/forecast_weekly_tg.py --print         # print the Telegram HTML and the button, send nothing
    python scripts/forecast_weekly_tg.py --out DIR       # write DIR/forecast.html, send nothing
    python scripts/forecast_weekly_tg.py --db PATH       # another SQLite file (default: the dashboard's journal DB)

Read-only and offline: the rows come from the ``forecast_log`` table (schema v20, ``prob_rv`` from v23) over a
``mode=ro`` SQLite connection, never from the network. The numbers are analytics.forecast_log's own ``score``,
``compare`` and ``weekly`` (the dashboard's «Журнал прогнозів» block uses tested stdlib copies of the same functions),
so the message and the panel never disagree. Short (owner, 22.10): a title, four lines, one caveat sentence.

The probabilities are RISK-NEUTRAL (the Deribit smile), not the assistant's forecast and not advice; the sample size is
the number of settled (asset, expiry) outcomes, and below analytics.forecast_log.MIN_EXPIRIES the text says there is no
conclusion. The RV30 comparison is descriptive until that many expiry dates. A missing database, a store older than
schema v20, an empty table or only open forecasts give a short explanation, never an error. Any text from the data is
HTML-escaped. The «Журнал» button opens /journal under the dashboard URL that scripts/assistant_brief_tg.py resolves
($TRADEDESK_PANEL_URL or the file of scripts/server/23-tailscale.sh); with no usable URL the text goes out without it.
Sending goes through scripts/tg_send.py (token never read here). Scheduled by
deploy/user-units/tradedesk-forecast-weekly.* (Saturday 12:00 Europe/Kyiv, after Friday's 08:00 UTC delivery) while
the flag file of scripts/server/26-forecast-weekly-timer.sh exists.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import forecast_log as fl  # noqa: E402

CAVEAT = (
    "Не порада і не прогноз асистента: це ризик-нейтральні ймовірності з опціонів Deribit; журнал перевіряє гіпотезу, перевагу не показано."
)
_h = ab._h  # Telegram HTML escaping of anything that comes from the data


def load_forecasts(path: Path) -> tuple[list[dict] | None, str | None]:
    """(rows, None) from table forecast_log over a read-only connection, or (None, reason): 'missing' (no file),
    'no_table' (a store older than schema v20), 'unreadable: …'. Never raises."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forecast_log'").fetchone() is None:
            return None, "no_table"
        cols = {r[1] for r in con.execute("PRAGMA table_info(forecast_log)")}
        rv = "prob_rv" if "prob_rv" in cols else "NULL AS prob_rv"  # the RV30 model arrives with schema v23
        rows = [
            dict(r)
            for r in con.execute(
                f"SELECT asset, kind, expiry_ts, prob, ref_prob, {rv}, status, outcome FROM forecast_log"
                " ORDER BY issued_ts DESC, id DESC LIMIT ?",
                (db.FC_MAX_ROWS,),
            )
        ]
        return rows, None
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def _bs(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def _signed(x: float | None, fmt: str = "+.3f") -> str:
    return "—" if x is None else format(x, fmt).replace("-", "−")


def _skill(x: float | None) -> str:
    return "—" if x is None else _signed(100 * x, "+.0f") + " %"


def _date(ts: object) -> str:
    return db.kyiv(datetime.fromtimestamp(int(ts), timezone.utc), "%d.%m")  # noqa: UP017 - as dashboard.py


def _sample_line(s: dict, n_open: int) -> str:
    n_exp = s["n_expiries"]
    head = f"Розраховано прогнозів: {s['n']}, експірацій (актив × дата): {n_exp}" + (f", відкритих: {n_open}" if n_open else "")
    if n_exp < fl.MIN_EXPIRIES:
        return _h(f"{head}: замало для висновку (потрібно ≥ {fl.MIN_EXPIRIES} експірацій), числа нижче — лише опис.")
    return _h(f"{head}: числа нижче — опис, перевагу вони не доводять.")


def _brier_line(s: dict) -> str:
    out = f"Brier опціонів {_bs(s['brier'])} (0 — ідеально, 0.25 — завжди 50 %)."
    if s.get("brier_on_ref") is not None:
        out += (
            f" Проти фіксованого еталона (вище спота 0.5, ±1σ 0.683): {_bs(s['brier_on_ref'])} проти {_bs(s['brier_ref'])}, "
            f"навичка {_skill(s['skill_vs_ref'])}."
        )
    out += (
        f" Проти кліматології (частота в цій самій вибірці, заднім числом): {_bs(s['brier'])} проти "
        f"{_bs(s['brier_climatology'])}, навичка {_skill(s['skill_vs_climatology'])}."
    )
    return _h(out)


def _rv_line(c: dict) -> str:
    if not c.get("n"):
        return _h("Опціони проти RV30: розрахованих пар ще немає (модель RV30 пишеться зі схеми v23).")
    ci = c.get("ci95")
    band = f"ДІ 95 % {_signed(ci[0])} … {_signed(ci[1])}" if ci else "ДІ — від 2 дат експірації"
    out = (
        f"Опціони проти RV30: Brier {_bs(c['brier_risk_neutral'])} проти {_bs(c['brier_rv30'])}, різниця {_signed(c['diff'])} "
        f"(мінус — опціони точніші; {band}), дат експірації: {c['n_dates']}"
    )
    if not c.get("enough"):
        return _h(f"{out} — лише опис, висновок — від {fl.MIN_EXPIRIES} дат.")
    if ci is not None and ci[1] < 0:
        verdict = "ДІ нижче нуля: на цій вибірці точніші опціони"
    elif ci is not None and ci[0] > 0:
        verdict = "ДІ вище нуля: на цій вибірці точніша RV30"
    else:
        verdict = "різниця в межах шуму (ДІ перетинає нуль)"
    return _h(f"{out} — {verdict}.")


def _week_line(rows: list[dict], weeks: list[dict]) -> str:
    w = weeks[-1]
    dates = sorted(
        {
            int(x["expiry_ts"])
            for x in rows
            if x.get("outcome") is not None and x.get("prob") is not None and fl.iso_week(int(x["expiry_ts"])) == w["week"]
        }
    )
    when = ", ".join(_date(t) for t in dates)
    ref = f"; на подіях з еталоном {_bs(w['brier_on_ref'])} проти {_bs(w['brier_ref'])}" if w.get("brier_on_ref") is not None else ""
    return _h(
        f"Останній розрахований тиждень {w['week']} (експірація {when}): прогнозів {w['n']}, експірацій {w['n_expiries']}, "
        f"Brier {_bs(w['brier'])}{ref}. Один тиждень — не сигнал."
    )


def build_summary(rows: list[dict] | None, now: float, err: str | None = None) -> str:
    """The weekly forecast-log summary as Telegram HTML: title, up to four lines, one caveat (at most 8 lines). Pure
    (no I/O, time passed in); never raises. ``rows`` = forecast_log rows (None with ``err`` from load_forecasts)."""
    head = f"<b>Журнал прогнозів за тиждень · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{ab._tz()}</b>"  # noqa: UP017
    tail = ["", f"<i>{_h(CAVEAT)}</i>"]
    try:
        if rows is None:
            why = {
                "missing": "файла бази ще немає (його створюють MCP-сервер і колектор).",
                "no_table": "таблиці <code>forecast_log</code> ще немає: база до схеми v20 (з'явиться, коли MCP-сервер або колектор відкриють її новим кодом).",
            }.get(err or "", f"базу не вдалося прочитати ({_h(err or 'невідома причина')}).")
            return ab._fit([head, f"<b>Підсумку немає:</b> {why}"], tail)
        if not rows:
            return ab._fit(
                [
                    head,
                    "<b>Прогнозів ще немає:</b> завдання колектора <code>forecast_log</code> вимкнене (вмикач "
                    "<code>TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1</code>) або ще не відпрацювало.",
                ],
                tail,
            )
        n_open = sum(1 for x in rows if x.get("status") == "open")
        s = fl.score(rows)
        if not s["n"]:
            nxt = min((int(x["expiry_ts"]) for x in rows if x.get("status") == "open" and x.get("expiry_ts") is not None), default=None)
            when = f"після delivery {_date(nxt)} (08:00 UTC)" if nxt is not None else "після першої delivery"
            return ab._fit([head, _h(f"Розрахованих прогнозів ще немає (відкритих: {n_open}); перша оцінка — {when}.")], tail)
        body = [head, _sample_line(s, n_open), _brier_line(s), _rv_line(fl.compare(rows))]
        weeks = fl.weekly(rows)
        if weeks:
            body.append(_week_line(rows, weeks))
        return ab._fit(body, tail)
    except Exception as e:  # noqa: BLE001 - an odd row must not drop the message
        return ab._fit([head, _h(f"Підсумок не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")], tail)


def journal_buttons(path: Path | None = None, environ: dict[str, str] | None = None, key_file: Path | None = None) -> list[tuple[str, str]]:
    """One «Журнал» URL button to /journal under ab.panel_base(), with the dashboard's key (k=) when it has one;
    [] when there is no usable URL."""
    base = ab.panel_base(path, environ)
    if not base:
        return []
    key = db.dashboard_key(key_file)
    q = f"?{urllib.parse.urlencode({'k': key})}" if key else ""
    return [("Журнал", f"{base}journal{q}")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=db.JOURNAL_DB, help="SQLite file (default: the dashboard's journal DB)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write forecast.html into this directory, send nothing")
    ap.add_argument("--topic", default="alerts", help="forum topic (default: alerts)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    rows, err = load_forecasts(a.db)
    text = build_summary(rows, time.time(), err)
    buttons = journal_buttons()
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            links = ", ".join(f"{label} {url}" for label, url in buttons) or f"none (no ${ab.PANEL_URL_ENV} and no {ab.DASHBOARD_URL_PATH})"
            print(f"\n[buttons: {links}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "forecast.html").write_text(text + "\n", encoding="utf-8")
        return 0
    return 0 if tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons) else 1


if __name__ == "__main__":
    sys.exit(main())
