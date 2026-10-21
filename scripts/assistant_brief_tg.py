"""The assistant's morning brief to Telegram: a compact text from the snapshot JSON plus an optional PNG chart.

    python3 scripts/assistant_brief_tg.py                  # send to the group's alerts topic
    python3 scripts/assistant_brief_tg.py --print          # print the Telegram HTML, send nothing
    python3 scripts/assistant_brief_tg.py --out DIR        # write DIR/brief.html (+ DIR/brief.png), send nothing

Read-only and offline: the text is built from the same snapshot file as the dashboard's «Бриф» tab
(``assistant_snapshot`` collector job; path and field names from scripts/dashboard.py), never from the network. Every
string from the snapshot is untrusted and HTML-escaped for Telegram. A missing or unreadable snapshot gives an
explained message, not an error; older than 30 min adds a warning; older than 6 h gives only the stamp and the
warning (no prices, no countdowns). Event countdowns are counted from the send time, past events are dropped. The
caveats are never cut by the length limit. The chart (BTC 120 days, options' ±1σ cone to the month expiry, weekly
straddle breakevens) needs matplotlib, imported lazily: without it only the text goes out. Sending goes through
scripts/tg_send.py (token and chat id from ~/.config/tradedesk/agents.env, never read or printed here).
Scheduled by deploy/user-units/tradedesk-assistant-brief.* (08:00 Europe/Kyiv) while the flag file of
scripts/server/22-assistant-brief-timer.sh exists (a tradedesk-owned file, so not an owner-only switch).
"""

from __future__ import annotations

import argparse
import html
import io
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dashboard as db  # noqa: E402
import tg_send  # noqa: E402

MAX_EVENTS = 5
CAVEATS = (
    "Ймовірності й діапазони з цін опціонів — ризик-нейтральні: це ціна страхування в ринку, а не прогноз.",
    "Рекомендації асистента — лише «гіпотеза»: перевірку методу (H1) 18.10 відхилено, перевагу не доведено; паперовий журнал її перевіряє.",
    "Не інвестиційна порада: бриф лише показує дані й нічого не торгує.",
)
LABELS = {"calm": "спокій", "risk": "ризик", "ev": "подія"}


def _h(x: object) -> str:
    """Escape text for Telegram HTML (snapshot strings are untrusted)."""
    return html.escape(str(x), quote=False)


def _pts(x: float | None) -> str:
    """Signed volatility points from a decimal (0.052 -> +5.2)."""
    return db._pc(None if x is None else x * 100, 1).replace(" %", "")


def _tz() -> str:
    """' за Києвом' only when the Kyiv zone is known; without tzdata db.kyiv() already prints '… UTC'."""
    return " за Києвом" if db.KYIV is not None else ""


def _age(snap: dict, now: float) -> float | None:
    asof = db.parse_utc(snap.get("asof_utc"))
    return None if asof is None else now - asof.timestamp()


def _when(x: dict, now: float) -> tuple[datetime | None, float | None]:
    """An event's time and hours from ``now`` (not the snapshot's own ``hours_ahead``, which ages with the file)."""
    ts = db._iso(x.get("ts_utc"))
    return ts, None if ts is None else (ts.timestamp() - now) / 3600


def _stamp(snap: dict, now: float) -> list[str]:
    asof = db.parse_utc(snap.get("asof_utc"))
    if asof is None:
        return ["<b>Увага:</b> час знімка невідомий, дані можуть бути застарілими."]
    age = now - asof.timestamp()
    out = [f"Знімок {_h(db.kyiv(asof))}{_tz()} · {_h(db.fmt_age(age))}"]
    if age > db.SNAPSHOT_OLD_S:
        out.insert(
            0,
            "<b>Увага: знімок старший за 6 год</b> — ціни й події в ньому вже не поточні, тому блоки брифу не "
            "показано. Завдання колектора <code>assistant_snapshot</code>, схоже, зупинилось.",
        )
    elif age > db.SNAPSHOT_STALE_S:
        out.insert(
            0,
            "<b>Увага: знімок старший за 30 хв</b> — завдання колектора <code>assistant_snapshot</code>, схоже, "
            "зупинилось; дані можуть бути неактуальні.",
        )
    miss = [db.SECTION_UA.get(str(n), db._clean(n, 30)) for n in db._list(snap.get("missing"))]
    stale = [db.SECTION_UA.get(str(n), db._clean(n, 30)) for n in db._list(snap.get("stale"))]
    if miss:
        out.append(_h("Без даних: " + ", ".join(miss)))
    if stale:
        out.append(_h("З попереднього знімка: " + ", ".join(stale)))
    return out


def _market(snap: dict, now: float) -> list[str]:
    lines = []
    tick = db._tickers(snap)
    for base in ("BTC", "ETH", "SOL"):
        t = tick.get(base)
        last = db._num(t.get("last")) if t else None
        if last is None:
            continue
        lines.append(f"{base} <b>{_h(db._nf(last, 0 if last >= 1000 else 2))}</b> ({_h(db._pc(t.get('percentage'), 2))} за 24 год)")
    fng = db._data(snap, "fng") or {}
    latest = fng.get("latest") if isinstance(fng.get("latest"), dict) else {}
    if db._num(latest.get("value")) is not None:
        lines.append(
            _h(
                f"Страх і жадібність {db._nf(latest['value'])} ({db._fng_zone(latest)}); "
                f"7 дн {db._nf(fng.get('mean_7d'))}, 30 дн {db._nf(fng.get('mean_30d'))}"
            )
        )
    cur = (db._data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    dvol = db._num(cur.get("dvol"))
    if dvol is None:
        lat = (db._data(snap, "dvol") or {}).get("latest")
        v = db._num(lat.get("value_pct")) if isinstance(lat, dict) else None
        dvol = v / 100 if v is not None else None
    if dvol is not None:
        pr, spr = db._num(cur.get("dvol_percentile_365d")), db._num(cur.get("spread_vs_rv30"))
        txt = f"DVOL {db._nf(dvol * 100, 1)}"
        txt += f" — {db._nf(pr * 100)}-й перцентиль за рік" if pr is not None else " (перцентиля немає)"
        if spr is not None:
            txt += f"; IV − RV30 {_pts(spr)} вп"
        lines.append(_h(txt))
    return lines


def _key(snap: dict, now: float) -> list[str]:
    """«Головне сьогодні»: the dashboard's key items as text lines (label, then the item)."""
    items: list[tuple[str, str]] = []
    w, m = db._em(snap, "em_week"), db._em(snap, "em_month")
    if w and db._num(w.get("straddle_pct")) is not None:
        items.append(
            (
                "calm",
                f"Опціони закладають ±{db._nf(w['straddle_pct'], 1)} % до {db._exp_label(snap, 'week')} "
                f"(беззбиток тижневого стреддлу {db._k(w.get('breakeven_lo'))} – {db._k(w.get('breakeven_hi'))}).",
            )
        )
    if m and db._num(m.get("sigma_move_pct")) is not None:
        items.append(
            (
                "calm",
                f"±{db._nf(m['sigma_move_pct'], 1)} % (1σ) до {db._exp_label(snap, 'month')}: "
                f"{db._k(m.get('sigma_lo'))} – {db._k(m.get('sigma_hi'))} (ризик-нейтрально).",
            )
        )
    cur = (db._data(snap, "vrp") or {}).get("current")
    pr = db._num(cur.get("dvol_percentile_365d")) if isinstance(cur, dict) else None
    if pr is not None:
        items.append(
            (
                "risk" if pr >= 0.8 else "calm",
                "Рівень волатильності сам по собі не сигнал: «купувати дешеву волатильність» перевірено 18.10 і відхилено.",
            )
        )
    sk = db._skew_ref(snap)
    if sk:
        rr, exp = db._num(sk["rr"]), db._clean(sk.get("expiry"), 12)
        if rr < 0:
            items.append(
                (
                    "risk" if rr <= -0.02 else "calm",
                    f"Пути 25Δ дорожчі за коли на {db._nf(-rr * 100, 1)} вп ({exp}): ринок платить за захист.",
                )
            )
        else:
            items.append(("calm", f"Коли 25Δ дорожчі за пути на {db._nf(rr * 100, 1)} вп ({exp}): ринок платить за участь у зростанні."))
    ev = db._data(snap, "events")
    if ev is not None:
        nxt = ev.get("next_event") if isinstance(ev.get("next_event"), dict) else None
        ts, hrs = _when(nxt, now) if nxt else (None, None)
        if nxt and (hrs is None or hrs >= 0):  # an event already past at send time is dropped
            txt = f"{db._clean(nxt.get('title'), 80)} — {db.kyiv(ts)}{_tz()}"
            txt += f", {db._in_hours(hrs)}." if hrs is not None else "."
            if hrs is not None:
                near = hrs <= 48 and str(nxt.get("impact")) == "high"
            else:
                near = ev.get("high_impact_within_48h") is True
            if near:
                items.append(("risk", txt + " До події з високим впливом менше 48 год: менший розмір або обмежений ризик."))
            else:
                items.append(("ev", txt))
        elif not nxt:
            items.append(("calm", "Подій з високим впливом на 14 днів немає."))
    return [f"• <i>{LABELS[c]}</i> · {_h(t)}" for c, t in items]


def _events(snap: dict, now: float) -> list[str]:
    ev = db._data(snap, "events")
    if ev is None:
        return []
    out = []
    for x in db._list(ev.get("events")):
        if len(out) >= MAX_EVENTS:
            break
        if not isinstance(x, dict):
            continue
        ts, hrs = _when(x, now)
        if hrs is not None and hrs < 0:  # already past at send time
            continue
        imp = str(x.get("impact") or "")
        extra = [db.IMPACT_UA.get(imp, db._clean(imp, 12))] if imp else []
        if hrs is not None:
            extra.append(db._in_hours(hrs))
        if x.get("source") == "seed":
            extra.append("час орієнтовний")
        tail = f" ({', '.join(extra)})" if extra else ""
        out.append("• " + _h(f"{db.kyiv(ts)} — {db._clean(x.get('title'), 100)}{tail}"))
    return out


def _funding(snap: dict, now: float) -> list[str]:
    out = []
    for base, name in (("BTC", "funding_btc"), ("ETH", "funding_eth")):
        venues = [
            v
            for v in db._list((db._data(snap, name) or {}).get("venues"))
            if isinstance(v, dict) and db._num(v.get("annualized")) is not None
        ]
        if venues:
            parts = [
                f"{db.VENUE_NAMES.get(str(v.get('venue')), db._clean(v.get('venue'), 20))} {db._pc(db._num(v['annualized']) * 100, 1)}"
                for v in venues
            ]
            out.append(_h(f"{base}: " + ", ".join(parts)))
    return out


def build_brief(snap: dict | None, now: float, err: str | None = None) -> str:
    """The brief as Telegram HTML. Pure (no I/O, time passed in); never raises."""
    head = f"<b>Бриф асистента · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{_tz()}</b>"  # noqa: UP017
    tail = ["", "<b>Застереження</b>"] + [f"• {_h(c)}" for c in CAVEATS]
    if snap is None:
        why = {
            "missing": "знімка асистента ще немає: завдання колектора <code>assistant_snapshot</code> вимкнене або не відпрацювало.",
            "corrupt": "файл знімка пошкоджений; наступний прогін колектора (кожні 10 хв) його перепише.",
        }.get(err or "", f"знімок не читається ({_h(err or 'невідома причина')}).")
        return _fit([head, f"<b>Брифу сьогодні немає:</b> {why}"], tail)
    try:
        out = [head] + _stamp(snap, now)
        age = _age(snap, now)
        if age is not None and age > db.SNAPSHOT_OLD_S:  # yesterday's prices and countdowns would mislead
            return _fit(out, tail)
        for title, fn in (
            ("Ринок", _market),
            ("Головне сьогодні", _key),
            ("Події · час " + ("Київ" if db.KYIV is not None else "UTC"), _events),
            ("Funding · річних", _funding),
        ):
            try:
                lines = fn(snap, now)
            except Exception as e:  # noqa: BLE001 - one odd section must not drop the brief
                lines = [_h(f"Блок не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")]
            if lines:
                out += ["", f"<b>{title}</b>"] + lines
        if db._data(snap, "funding_btc") or db._data(snap, "funding_eth"):
            out.append("<i>Додатний funding — лонги платять шортам; до комісій і базису.</i>")
        return _fit(out, tail)
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        return _fit([head, _h(f"Знімок не вдалося прочитати ({type(e).__name__}).")], tail)


def _fit(body: list[str], tail: list[str]) -> str:
    """Join body and caveats within Telegram's limit: the body is cut (at a line), the caveats never are, so
    tg_send.clip has nothing left to cut from the end."""
    end = "\n".join(tail)
    budget = tg_send.TEXT_LIMIT - len(end) - 1 - 2  # clip may add "\n…" past its limit
    return tg_send.clip("\n".join(body), budget) + "\n" + end


def chart_png(snap: dict | None) -> bytes | None:
    """BTC daily closes for the snapshot's 120 days with the ±1σ cone to the month expiry and the weekly straddle
    breakevens, as PNG bytes. None when matplotlib is missing, the data is too thin or anything fails."""
    if not snap:
        return None
    try:
        pts = []
        for r in db._list((db._data(snap, "ohlc") or {}).get("rows")):
            if isinstance(r, list) and len(r) >= 5 and db._num(r[0]) is not None and db._num(r[4]) is not None:
                pts.append((db._num(r[0]), db._num(r[4])))
        if len(pts) < 2:
            return None
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception:  # noqa: BLE001 - no matplotlib: text only
            return None
        pts.sort()
        xs = [datetime.fromtimestamp(t / 1000, timezone.utc) for t, _ in pts]  # noqa: UP017
        ys = [c for _, c in pts]
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)
        ax.plot(xs, ys, color="#1f2328", lw=1.5, label="BTC, закриття дня (Binance)")
        exp = snap.get("expiries") if isinstance(snap.get("expiries"), dict) else {}
        m, w = db._em(snap, "em_month"), db._em(snap, "em_week")
        tm, tw = db.expiry_utc(exp.get("month")), db.expiry_utc(exp.get("week"))
        if m and tm and db._num(m.get("sigma_lo")) and db._num(m.get("sigma_hi")) and tm > xs[-1]:
            lo, hi = db._num(m["sigma_lo"]), db._num(m["sigma_hi"])
            ax.fill(
                [xs[-1], tm, tm], [ys[-1], hi, lo], color="#2f6db5", alpha=0.18, label=f"±1σ до {db._exp_label(snap, 'month')} (опціони)"
            )
            for y in (lo, hi):
                ax.annotate(db._k(y), (tm, y), fontsize=8, color="#2f6db5", ha="right", va="center")
        if w and tw and db._num(w.get("breakeven_lo")) and db._num(w.get("breakeven_hi")) and tw > xs[-1]:
            for i, y in enumerate((db._num(w["breakeven_lo"]), db._num(w["breakeven_hi"]))):
                ax.plot(
                    [xs[-1], tw],
                    [y, y],
                    color="#b8761d",
                    lw=1.6,
                    ls="--",
                    label=f"беззбиток стреддлу до {db._exp_label(snap, 'week')}" if not i else None,
                )
        ax.annotate(db._nf(ys[-1]), (xs[-1], ys[-1]), fontsize=8, xytext=(-4, 6), textcoords="offset points", ha="right")
        ax.set_title("BTC за 120 днів і що закладають опціони (ризик-нейтрально)", fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
        fig.autofmt_xdate()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - the chart is optional; the text brief still goes out
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", type=Path, default=db.SNAPSHOT_PATH, help="snapshot JSON (default: the dashboard's)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--out", type=Path, help="write brief.html (and brief.png) into this directory, send nothing")
    ap.add_argument("--no-chart", action="store_true", help="text only")
    ap.add_argument("--topic", default="alerts", help="forum topic (default: alerts)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    snap, err = db.load_snapshot(a.snapshot)
    text = build_brief(snap, time.time(), err)
    png = None if a.no_chart else chart_png(snap)
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            print(f"\n[chart: {'PNG ' + str(len(png)) + ' bytes' if png else 'none (matplotlib missing or no data)'}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "brief.html").write_text(text + "\n", encoding="utf-8")
            if png:
                (a.out / "brief.png").write_bytes(png)
        return 0
    ok = tg_send.send(text, silent=a.silent, topic=a.topic)
    if ok and png:
        caption = f"BTC 120 днів, ±1σ до {_h(db._exp_label(snap, 'month'))} з опціонів (ризик-нейтрально)"
        tg_send.send_photo(png, caption, silent=True, topic=a.topic)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
