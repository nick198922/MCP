"""The assistant's Sunday radar to Telegram: the «Поточна ситуація» lines of the dashboard's «Радар» tab and buttons to it.

    python3 scripts/radar_weekly_tg.py                 # send to the group's alerts topic
    python3 scripts/radar_weekly_tg.py --print         # print the Telegram HTML and the buttons, send nothing
    python3 scripts/radar_weekly_tg.py --out DIR       # write DIR/radar.html, send nothing
    python3 scripts/radar_weekly_tg.py --asset ETH     # ETH's lines instead of BTC's

Read-only and offline: the text comes from the `radar` section of the same snapshot file as the dashboard (the
``assistant_snapshot`` collector job), never from the network. The lines are the dashboard's own
``dashboard.radar_situation`` (the mock-up's rSummary: price, IV by tenor and the front, IV Rank, skew, the «hot»
forward-vol week), so the message and the tab never disagree. Short by the owner's decision of 22.10 (a long text in
Telegram is hard to read): a title, up to five lines, one caveat sentence; the charts are SVG on the radar page, no PNG.
Every string from the snapshot is untrusted and HTML-escaped. A missing snapshot, radar section, asset or surface
archive gives a short explanation, never an error; a snapshot older than 30 min adds a warning, older than 6 h gives
only the warning. Buttons «Радар BTC» / «Радар ETH» open /radar?asset=… under the dashboard URL that
scripts/assistant_brief_tg.py resolves ($TRADEDESK_PANEL_URL or the file of scripts/server/23-tailscale.sh); with no
usable URL the text goes out without buttons. Sending goes through scripts/tg_send.py (token never read here).
Scheduled by deploy/user-units/tradedesk-radar-weekly.* (Sunday 19:30 Europe/Kyiv) while the flag file of
scripts/server/25-radar-weekly-timer.sh exists.
"""

from __future__ import annotations

import argparse
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

CAVEAT = "Не порада: усе тут — гіпотези без доведеної переваги (метод рекомендацій H1 відхилено 18.10); IV і ймовірності з опціонів ризик-нейтральні."
BUTTONS_PER_ROW = 2
_h = ab._h  # Telegram HTML escaping of untrusted snapshot text


def _stamp(snap: dict, now: float) -> tuple[list[str], bool]:
    """(warning lines, too old to show the data). A fresh snapshot needs no line: each radar line carries its own dates."""
    asof = db.parse_utc(snap.get("asof_utc"))
    if asof is None:
        return ["<b>Увага:</b> час знімка невідомий, дані можуть бути застарілими."], False
    age = now - asof.timestamp()
    when = f"{_h(db.kyiv(asof))}{ab._tz()}, {_h(db.fmt_age(age))}"
    if age > db.SNAPSHOT_OLD_S:
        msg = (
            f"<b>Увага: знімок старший за 6 год</b> ({when}), тому радар не показано. Завдання колектора "
            "<code>assistant_snapshot</code>, схоже, зупинилось."
        )
        return [msg], True
    if age > db.SNAPSHOT_STALE_S:
        return [f"<b>Увага: знімок старший за 30 хв</b> ({when}): завдання <code>assistant_snapshot</code>, схоже, зупинилось."], False
    return [], False


def _lines(snap: dict, asset: str) -> list[str]:
    """Up to five «Поточна ситуація» lines of `asset`, or one line saying why there are none."""
    sec = db._section(snap, "radar")
    if not sec:
        return [
            "Радару немає: секції «радар» у знімку ще немає (колектор без радару або завдання "
            "<code>assistant_snapshot</code> ще не відпрацювало з новим кодом)."
        ]
    a = db._rd(db._rd(sec.get("data")).get("assets")).get(asset)
    if not isinstance(a, dict):
        return [_h(f"Даних {asset} у секції радару немає.")]
    out = [f"• <i>{_h(lab)}</i> · {_h(t)}" for _, lab, t in db.radar_situation(a, asset)]
    if not out:
        out = ["Поточну ситуацію скласти нема з чого: у секції радару немає ні ціни, ні IV, ні IV Rank, ні скью."]
    if db._rd(a.get("surface")).get("now_day") is None:
        out.append(
            "Архіву поверхні ще немає: ціна, IV за строками, скью й форвардна вола з'являться, коли завдання "
            "<code>radar_archive</code> запише рядок."
        )
    return out


def build_radar(snap: dict | None, now: float, err: str | None = None, asset: str = "BTC") -> str:
    """The Sunday radar as Telegram HTML: title, warnings, up to five lines, one caveat. Pure (no I/O, time passed in);
    never raises."""
    asset = str(asset).upper() if str(asset).upper() in db.RADAR_ASSETS else db.RADAR_ASSETS[0]
    head = f"<b>Радар тижня · {asset} · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{ab._tz()}</b>"  # noqa: UP017
    tail = ["", f"<i>{_h(CAVEAT)}</i>"]
    if snap is None:
        why = {
            "missing": "знімка асистента ще немає: завдання колектора <code>assistant_snapshot</code> вимкнене або не відпрацювало.",
            "corrupt": "файл знімка пошкоджений; наступний прогін колектора (кожні 10 хв) його перепише.",
        }.get(err or "", f"знімок не читається ({_h(err or 'невідома причина')}).")
        return ab._fit([head, f"<b>Радару сьогодні немає:</b> {why}"], tail)
    try:
        warn, old = _stamp(snap, now)
        if old:
            return ab._fit([head, *warn], tail)
        try:
            lines = _lines(snap, asset)
        except Exception as e:  # noqa: BLE001 - an odd section must not drop the message
            lines = [_h(f"Радар не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")]
        return ab._fit([head, *warn, *lines], tail)
    except Exception as e:  # noqa: BLE001 - the message always goes out
        return ab._fit([head, _h(f"Знімок не вдалося прочитати ({type(e).__name__}).")], tail)


def radar_buttons(path: Path | None = None, environ: dict[str, str] | None = None, key_file: Path | None = None) -> list[tuple[str, str]]:
    """«Радар BTC» / «Радар ETH» URL buttons to /radar?asset=… under ab.panel_base(), with the dashboard's key (k=)
    when it has one; [] when there is no usable URL."""
    base = ab.panel_base(path, environ)
    if not base:
        return []
    key = db.dashboard_key(key_file)
    out = []
    for asset in db.RADAR_ASSETS:
        q = {"asset": asset, **({"k": key} if key else {})}
        out.append((f"Радар {asset}", f"{base}radar?{urllib.parse.urlencode(q)}"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", type=Path, default=db.SNAPSHOT_PATH, help="snapshot JSON (default: the dashboard's)")
    ap.add_argument("--asset", default="BTC", choices=db.RADAR_ASSETS, type=str.upper, help="whose lines (default: BTC)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write radar.html into this directory, send nothing")
    ap.add_argument("--topic", default="alerts", help="forum topic (default: alerts)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    snap, err = db.load_snapshot(a.snapshot)
    text = build_radar(snap, time.time(), err, a.asset)
    buttons = radar_buttons()
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            links = ", ".join(f"{label} {url}" for label, url in buttons) or f"none (no ${ab.PANEL_URL_ENV} and no {ab.DASHBOARD_URL_PATH})"
            print(f"\n[buttons: {links}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "radar.html").write_text(text + "\n", encoding="utf-8")
        return 0
    return 0 if tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons, per_row=BUTTONS_PER_ROW) else 1


if __name__ == "__main__":
    sys.exit(main())
