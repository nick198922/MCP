"""The assistant's morning brief to Telegram: a short text from the snapshot JSON with buttons to the dashboard pages.

    python3 scripts/assistant_brief_tg.py                  # send to the group's alerts topic
    python3 scripts/assistant_brief_tg.py --print          # print the Telegram HTML and the buttons, send nothing
    python3 scripts/assistant_brief_tg.py --out DIR        # write DIR/brief.html (+ DIR/brief.png), send nothing
    python3 scripts/assistant_brief_tg.py --full --print   # the long text (every block), e.g. to compare

Read-only and offline: the text is built from the same snapshot file as the dashboard's «Бриф» tab
(``assistant_snapshot`` collector job; path and field names from scripts/dashboard.py), never from the network. Every
string from the snapshot is untrusted and HTML-escaped for Telegram. The message is short (owner 22.10: a long text in
Telegram is hard to read): BTC and ETH with the 24 h change, DVOL with its 1-year percentile, one «Режим циклу» line
for BTC (section ``cycle``, the cycle_regime tool, labelled «контекст, не сигнал»; when yesterday's state differs from
the day before, a separate short message follows the brief: `cycle_change`), the 1-3 most important
items of the day, one line with BTC's risk-neutral probabilities to the month expiry (inside ±1σ and in the «Бриф»
tab's two tail ranges, labelled «ризик-нейтральна»; it replaces the ±1σ item; omitted without ``chain`` / ``em_month``),
one line with the state of the paper recommendation (section ``paper_rec``: «актуальна / змінилась /
більше не актуальна», labelled «гіпотеза»; omitted without data), the owner's open Bybit options (section
``my_options``: a total line with P&L and theta per day, then at most 3 positions with the risk-neutral chance of
profit, labelled «ризик-нейтральний, не порада»; omitted without data or without open positions), up to three
«Потоки» lines for BTC (section ``flows``, the market_flow tool: the 24 h descriptive sentence, funding and
liquidations, the Coinbase premium against its 30-day median, labelled «описово, не сигнал»), the next events in
Kyiv time and one caveat line; the dashboard pages carry the rest. A missing or
unreadable snapshot gives an explained message, not an error; older than 30 min adds a warning; older than 6 h gives
only the stamp and the warning (no prices, no countdowns). Event countdowns are counted from the send time, past events
are dropped. The caveats are never cut by the length limit.
Buttons: URL buttons to the dashboard pages (Бриф, Опціони, Радар, Журнал, Polymarket) under the base URL from
$TRADEDESK_PANEL_URL or, when that is unset, from the file scripts/server/23-tailscale.sh writes (DASHBOARD_URL_PATH:
`tailscale serve` https URL of this machine in the owner's tailnet). Only a plain https URL is used (no credentials,
query or fragment); with none the text goes out without buttons.
The chart (BTC 120 days, options' ±1σ cone to the month expiry, weekly straddle breakevens) needs matplotlib, imported
lazily: without it only the text goes out. Sending goes through scripts/tg_send.py (token and chat id from
~/.config/tradedesk/agents.env, never read or printed here). Scheduled by deploy/user-units/tradedesk-assistant-brief.*
(08:00 Europe/Kyiv) while the flag file of scripts/server/22-assistant-brief-timer.sh exists (a tradedesk-owned file,
so not an owner-only switch).
"""

from __future__ import annotations

import argparse
import html
import io
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
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
CAVEAT_SHORT = "Не інвестиційна порада. Ймовірності з опціонів ризик-нейтральні; рекомендації — лише «гіпотеза» (H1 відхилено 18.10)."
LABELS = {"calm": "спокій", "risk": "ризик", "ev": "подія"}
MAX_KEY_SHORT = 3  # «Головне сьогодні» items in the short brief, risk first
MAX_EVENTS_SHORT = 2
PANEL_URL_ENV = "TRADEDESK_PANEL_URL"
DASHBOARD_URL_PATH = Path("/var/lib/tradedesk-mcp/assistant/dashboard_url")  # written by scripts/server/23-tailscale.sh
PAGES = (("Бриф", "brief"), ("Опціони", "options"), ("Радар", "radar"), ("Журнал", "journal"), ("Polymarket", "polymarket"))
BUTTONS_PER_ROW = 3
PAPER_STATE_UA = {"actual": "актуальна", "changed": "змінилась", "no_longer_actual": "більше не актуальна"}
PAPER_PICK_UA = {  # analytics.option_replay.STRUCTURES; "_14d" = the ~14-day expiry, the rest ~40 days
    "long_straddle": "купівля стреддлу ATM",
    "short_straddle": "продаж стреддлу ATM",
    "long_strangle25": "купівля стренглу 25Δ",
    "short_strangle25": "продаж стренглу 25Δ",
    "iron_condor": "залізний кондор 25/10Δ",
    "long_strangle10": "купівля стренглу 10Δ",
    "reverse_iron_fly": "зворотний залізний метелик",
    "call_debit_spread": "колл-дебетовий спред",
    "put_debit_spread": "пут-дебетовий спред",
}
PAPER_TRIGGER_UA = {"regime": "через зміну режиму", "event": "перед FOMC / CPI"}
MAX_MY_OPTIONS = 3  # the owner's Bybit option positions listed under their total line
SIDE_UA = {"long": "лонг", "short": "шорт"}


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


def _stamp(snap: dict, now: float, short: bool = False) -> list[str]:
    """Snapshot time, stale warnings and the sections without data. `short` drops the time line of a fresh snapshot."""
    asof = db.parse_utc(snap.get("asof_utc"))
    if asof is None:
        return ["<b>Увага:</b> час знімка невідомий, дані можуть бути застарілими."]
    age = now - asof.timestamp()
    fresh = short and age <= db.SNAPSHOT_STALE_S
    out = [] if fresh else [f"Знімок {_h(db.kyiv(asof))}{_tz()} · {_h(db.fmt_age(age))}"]
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
    dvol, pr, spr = _dvol(snap)
    if dvol is not None:
        txt = f"DVOL {db._nf(dvol * 100, 1)}"
        txt += f" — {db._nf(pr * 100)}-й перцентиль за рік" if pr is not None else " (перцентиля немає)"
        if spr is not None:
            txt += f"; IV − RV30 {_pts(spr)} вп"
        lines.append(_h(txt))
    return lines


def _dvol(snap: dict) -> tuple[float | None, float | None, float | None]:
    """(DVOL, its 365-day percentile, IV − RV30) as decimals; DVOL falls back to the dvol section."""
    cur = (db._data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    dvol = db._num(cur.get("dvol"))
    if dvol is None:
        lat = (db._data(snap, "dvol") or {}).get("latest")
        v = db._num(lat.get("value_pct")) if isinstance(lat, dict) else None
        dvol = v / 100 if v is not None else None
    return dvol, db._num(cur.get("dvol_percentile_365d")), db._num(cur.get("spread_vs_rv30"))


def _market_short(snap: dict, now: float) -> list[str]:
    """Two lines: BTC and ETH with the 24 h change, then DVOL with its percentile."""
    tick, parts = db._tickers(snap), []
    for base in ("BTC", "ETH"):
        t = tick.get(base)
        last = db._num(t.get("last")) if t else None
        if last is not None:
            parts.append(f"{base} <b>{_h(db._nf(last, 0 if last >= 1000 else 2))}</b> {_h(db._pc(t.get('percentage'), 2))}")
    lines = [" · ".join(parts) + " <i>за 24 год</i>"] if parts else []
    dvol, pr, _ = _dvol(snap)
    if dvol is not None:
        tail = f"{db._nf(pr * 100)}-й перцентиль за рік" if pr is not None else "перцентиля немає"
        lines.append(f"DVOL <b>{_h(db._nf(dvol * 100, 1))}</b> · {_h(tail)}")
    return lines


def _key(snap: dict, now: float) -> list[str]:
    """«Головне сьогодні»: the dashboard's key items as text lines (label, then the item)."""
    return [f"• <i>{LABELS[c]}</i> · {_h(t)}" for c, t in _key_items(snap, now)]


def _key_short(snap: dict, now: float, month: bool = True) -> list[str]:
    """The 1-3 most important key items: risk first, then in the dashboard's order; events have their own lines."""
    items = sorted(_key_items(snap, now, events=False, month=month), key=lambda it: it[0] != "risk")[:MAX_KEY_SHORT]
    return [f"• <i>{LABELS[c]}</i> · {_h(t)}" for c, t in items]


def _ranges(snap: dict, now: float) -> list[str]:
    """One line: BTC's risk-neutral probability to finish the month expiry inside the ±1σ band (section ``em_month``)
    and in the two open tails of the «Бриф» tab's scenarios (db.range_probabilities on the ``chain`` smile, same
    numbers as the tab). A tail that overlaps the ±1σ band is left out. [] without data (the stamp's «Без даних» line
    already names the section). Never raises."""
    try:
        chain, m = db._data(snap, "chain"), db._em(snap, "em_month")
        sm = db.chain_smile(chain) if chain else None
        if not sm or not m:
            return []
        lo, hi, move = db._num(m.get("sigma_lo")), db._num(m.get("sigma_hi")), db._num(m.get("sigma_move_pct"))
        exp = db.expiry_utc(chain.get("expiry"))
        if lo is None or hi is None or not 0 < lo < hi or exp is None or m.get("expiry") != chain.get("expiry"):
            return []
        F, T, pts = sm
        inside = max(0.0, db.prob_above(pts, F, T, lo) - db.prob_above(pts, F, T, hi))
        bands = db.range_probabilities(pts, F, T, db.scenario_cuts(F))
        width = f"±{db._nf(move, 1)} %, " if move is not None else ""
        parts = [f"±1σ ({width}{db._k(lo)} – {db._k(hi)}) — {db._nf(inside * 100)} %"]
        if bands[0]["hi"] <= lo:
            parts.append(f"нижче {db._k(bands[0]['hi'])} — {db._nf(bands[0]['p'] * 100)} %")
        if bands[-1]["lo"] >= hi:
            parts.append(f"вище {db._k(bands[-1]['lo'])} — {db._nf(bands[-1]['p'] * 100)} %")
        txt = f"BTC до {db.kyiv(exp, '%d.%m')}: " + "; ".join(parts) + "."
        return [f"• <i>ризик-нейтральна</i> · {_h(txt)}"]
    except Exception:  # noqa: BLE001 - an odd section only drops this line
        return []


def _day_short(snap: dict, now: float) -> list[str]:
    """The day's group of the short brief: the key items, the range line (it replaces the month ±1σ item), the paper
    recommendation."""
    rng = _ranges(snap, now)
    return _key_short(snap, now, month=not rng) + rng + _paper(snap, now)


def _key_items(snap: dict, now: float, events: bool = True, month: bool = True) -> list[tuple[str, str]]:
    """(label key, plain text) of «Головне сьогодні»; `events` adds the next-event item, `month` the ±1σ-to-the-month
    item (the short brief drops it when its range line already gives the same band)."""
    items: list[tuple[str, str]] = []
    w, m = db._em(snap, "em_week"), (db._em(snap, "em_month") if month else None)
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
    ev = db._data(snap, "events") if events else None
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
    return items


def _pick_ua(pick: object) -> str | None:
    """A method candidate ('short_strangle25', 'long_straddle_14d') in Ukrainian with its tenor; unknown names as text."""
    if not isinstance(pick, str) or not pick:
        return None
    base, short = (pick[:-4], True) if pick.endswith("_14d") else (pick, False)
    if base not in PAPER_PICK_UA:
        return db._clean(pick, 40)
    return f"{PAPER_PICK_UA[base]}, ~{14 if short else 40} дн"


def _paper(snap: dict, now: float) -> list[str]:
    """One line: the state of the paper recommendation (section ``paper_rec``), labelled «гіпотеза»; [] without data
    (the stamp's «Без даних» line already names the section). Never raises."""
    try:
        d = db._data(snap, "paper_rec")
        state = PAPER_STATE_UA.get(str(d.get("state"))) if d else None
        if state is None:
            return []
        name, action = _pick_ua(d.get("pick")), d.get("action")
        if d.get("state") == "no_longer_actual":
            detail = "метод сьогодні не обирає жодної структури"
        elif not name:
            detail = ""
        elif action == "open":
            trig = PAPER_TRIGGER_UA.get(str(d.get("trigger")))
            detail = "нова паперова угода" + (f" {trig}" if trig else "") + f" — {name}"
        elif d.get("state") == "changed":
            detail = f"метод тепер обрав би {name}; чинну угоду лишено (тригера немає)"
        else:
            detail = f"{name} — чинна паперова угода"
        asof = db.parse_utc(d.get("asof_utc"))
        when = f" (рішення {db.kyiv(asof, '%d.%m')})" if asof else ""
        txt = f"Паперова рекомендація {state}" + (f": {detail}" if detail else "") + f"{when}."
        return [f"• <i>гіпотеза</i> · {_h(txt)}"]
    except Exception:  # noqa: BLE001 - an odd section only drops this line
        return []


def _usd(x: object) -> str:
    """Signed dollars, whole from $10 (+$1 234, −$15) and with cents below (−$0.54: the owner's lots are 0.01 BTC);
    '—' without a number."""
    v = db._num(x)
    return "—" if v is None else ("+" if v >= 0 else "−") + "$" + db._nf(abs(v), 2 if abs(v) < 10 else 0)


def _my_options(snap: dict, now: float) -> list[str]:
    """The owner's open Bybit options (section ``my_options``): one total line and at most MAX_MY_OPTIONS positions,
    the largest unrealised P&L first. The chance of profit is labelled risk-neutral and not advice. [] without data
    (the stamp's «Без даних» line names the section) or without open positions. Never raises."""
    try:
        d = db._data(snap, "my_options")
        rows = [p for p in db._list(d.get("positions")) if isinstance(p, dict)] if d else []
        if not rows:
            return []
        asof = db.parse_utc(d.get("asof_utc"))
        when = f", на {db.kyiv(asof, '%H:%M')}" if asof else ""
        head = (
            f"Мої опціони Bybit ({len(rows)}{when}): P&L {_usd(d.get('upnl_usd'))}, тета {_usd(d.get('theta_usd_day'))}/день; "
            "шанс прибутку — ризик-нейтральний, не порада."
        )
        out = [f"• <i>мої опціони</i> · {_h(head)}"]
        for p in rows[:MAX_MY_OPTIONS]:
            size, pop, days = db._num(p.get("size")), db._num(p.get("pop")), db._num(p.get("days"))
            txt = f"{db._clean(p.get('symbol') or '?', 40)} {SIDE_UA.get(str(p.get('side')), '?')} {f'{size:g}' if size is not None else '?'}: "
            txt += f"P&L {_usd(p.get('upnl_usd'))}, тета {_usd(p.get('theta_usd_day'))}/день"
            if pop is not None:
                txt += f", шанс прибутку {db._nf(pop * 100)} %"
            if days is not None:
                txt += f", {db._nf(days, 1)} дн"
            out.append("  ◦ " + _h(txt))
        return out
    except Exception:  # noqa: BLE001 - an odd section only drops these lines
        return []


def _events(snap: dict, now: float) -> list[str]:
    ev = db._data(snap, "events")
    if ev is None:
        return []
    out = []
    for x in db.merge_same_day(ev.get("events")):
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


def _events_short(snap: dict, now: float) -> list[str]:
    """The next 1-2 events in Kyiv time, counted from now; a high-impact one within 48 h is labelled «ризик»."""
    ev = db._data(snap, "events")
    if ev is None:
        return []
    picked = db.merge_same_day(ev.get("events"))
    if not picked and isinstance(ev.get("next_event"), dict):
        picked = [ev["next_event"]]
    out = []
    for x in picked:
        if len(out) >= MAX_EVENTS_SHORT:
            break
        ts, hrs = _when(x, now)
        if hrs is not None and hrs < 0:  # already past at send time
            continue
        extra = [db._in_hours(hrs)] if hrs is not None else []
        if x.get("source") == "seed":
            extra.append("час орієнтовний")
        txt = f"{db.kyiv(ts)}{_tz() if ts else ''} — {db._clean(x.get('title'), 80)}"
        txt += f" ({', '.join(extra)})" if extra else ""
        near = hrs is not None and hrs <= 48 and str(x.get("impact")) == "high"
        if near:
            txt += ": до події з високим впливом менше 48 год — менший розмір або обмежений ризик."
        out.append(f"• <i>{LABELS['risk' if near else 'ev']}</i> · {_h(txt)}")
    return out or [f"• <i>{LABELS['calm']}</i> · Подій з високим впливом на 14 днів немає."]


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


def _musd(x: float) -> str:
    """Unsigned dollars in short form: 850 тис $, 12 млн $, 1.23 млрд $."""
    a = abs(x)
    return f"{a / 1e9:.2f} млрд $" if a >= 1e9 else (f"{a / 1e6:.0f} млн $" if a >= 1e6 else f"{a / 1e3:.0f} тис $")


def _flows(snap: dict, now: float, short: bool = False) -> list[str]:
    """«Потоки» (section ``flows``: market_flow for BTC, 1h, summaries only), at most three lines: the tool's 24 h
    descriptive sentence; funding and liquidations over 24 h; the Coinbase premium against its 30-day median with the
    label «описово, не сигнал» (single flow metrics did not predict direction in our tests). [] without data; never
    raises. `short` marks the first line «потоки» (the short brief has no block titles)."""
    try:
        d = db._data(snap, "flows") or {}
        summ = d.get("summary") if isinstance(d.get("summary"), dict) else {}
        s = summ.get("24h")
        if not isinstance(s, dict):
            return []
        out = []
        text = d.get("sentence_ua")
        if isinstance(text, str) and text.strip():
            out.append(_h(db._clean(text, 300)))
        parts = []
        f = db._num(s.get("funding_annualized_mean"))
        if f is not None:
            parts.append(f"Funding {db._pc(f * 100, 1)} річних (середнє за 24 год)")
        lg, sh = db._num(s.get("liq_long")), db._num(s.get("liq_short"))
        if lg is not None and sh is not None:
            parts.append(f"{'л' if parts else 'Л'}іквідації за 24 год: лонги {_musd(lg)}, шорти {_musd(sh)}")
        if parts:
            out.append(_h("; ".join(parts)))
        p = db._num(s.get("premium_adj_mean"))
        tail = "описово, не сигнал"
        if p is not None:
            bp = f"{p * 1e4:+.1f}".replace("-", "−")
            out.append(_h(f"Премія Coinbase до медіани 30 дн: {bp} б.п. (середнє за 24 год) · {tail}"))
        elif out:
            out.append(_h("Описово, не сигнал: поодинці ці метрики напрямок не передбачали."))
        if short and out:
            out[0] = f"• <i>потоки</i> · {out[0]}"
        return out
    except Exception:  # noqa: BLE001 - an odd section only drops this block
        return []


def _cycle(snap: dict, now: float) -> list[str]:
    """«Режим циклу» (section ``cycle``: cycle_regime for BTC), one line under the market lines: the tool's Ukrainian
    one-liner labelled «контекст, не сигнал» (pre-registered states, not backtested yet). [] without data; never raises."""
    try:
        text = (db._data(snap, "cycle") or {}).get("line_ua")
        if not isinstance(text, str) or not text.strip():
            return []
        return [_h(f"{db._clean(text, 300)} · контекст, не сигнал")]
    except Exception:  # noqa: BLE001 - an odd section only drops this line
        return []


CYCLE_STATE_UA = {"bottom": "дно", "accumulation": "накопичення", "overheat": "перегрів", "growth": "ріст", "decline": "падіння"}
CYCLE_CHANGE_NOTE = "Контекст, не сигнал: стани зафіксовано в картці до даних і ще не перевірені на історії."


def cycle_change(snap: dict | None, now: float) -> str | None:
    """The separate Telegram message when BTC's cycle state of yesterday (UTC) differs from the last decided day before
    it (section ``cycle``, its ``history``), as HTML; None when it did not change, the snapshot is older than
    db.SNAPSHOT_OLD_S, its last day is not yesterday (a carried-forward section: the change was told already) or the
    data is odd. Pure, never raises. The brief runs once a day, so each change is told once."""
    try:
        age = _age(snap, now) if snap else None
        if age is None or age > db.SNAPSHOT_OLD_S:
            return None
        data = db._data(snap, "cycle") or {}
        hist = [h for h in db._list(data.get("history")) if isinstance(h, dict)]
        if not hist:
            return None
        last = hist[-1]
        yesterday = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).date().isoformat()  # noqa: UP017
        if last.get("date") != yesterday or last.get("state") not in CYCLE_STATE_UA:
            return None
        decided = [h for h in hist if h.get("state") in CYCLE_STATE_UA]
        prev = decided[-2] if len(decided) >= 2 else None
        if prev is None or prev["state"] == last["state"]:
            return None
        changes = sum(1 for a, b in zip(decided, decided[1:], strict=False) if a["state"] != b["state"])
        d = datetime.fromisoformat(str(last["date"]))
        out = [
            f"<b>Режим циклу BTC змінився:</b> {CYCLE_STATE_UA[prev['state']]} → {CYCLE_STATE_UA[last['state']]} "
            f"(день {d:%d.%m}, закриття 00:00 UTC)"
        ]
        line = data.get("line_ua")
        if isinstance(line, str) and line.strip():
            out.append(_h(db._clean(line, 300)))
        out.append(_h(f"Змін стану за {len(hist)} днів: {changes}."))
        out.append(f"<i>{_h(CYCLE_CHANGE_NOTE)}</i>")
        return "\n".join(out)
    except Exception:  # noqa: BLE001 - an odd section only drops the message
        return None


def build_brief(snap: dict | None, now: float, err: str | None = None, short: bool = False) -> str:
    """The brief as Telegram HTML. Pure (no I/O, time passed in); never raises. `short` (what is sent): a few lines
    without block titles — BTC/ETH, DVOL, the 1-3 most important items, the risk-neutral range line, the paper
    recommendation, the next events, one caveat line; the long
    version has every block and the three caveats."""
    head = f"<b>Бриф асистента · {_h(db.kyiv(datetime.fromtimestamp(now, timezone.utc)))}{_tz()}</b>"  # noqa: UP017
    if short:
        tail = ["", f"<i>{_h(CAVEAT_SHORT)}</i>"]
    else:
        tail = ["", "<b>Застереження</b>"] + [f"• {_h(c)}" for c in CAVEATS]
    if snap is None:
        why = {
            "missing": "знімка асистента ще немає: завдання колектора <code>assistant_snapshot</code> вимкнене або не відпрацювало.",
            "corrupt": "файл знімка пошкоджений; наступний прогін колектора (кожні 10 хв) його перепише.",
        }.get(err or "", f"знімок не читається ({_h(err or 'невідома причина')}).")
        return _fit([head, f"<b>Брифу сьогодні немає:</b> {why}"], tail)
    try:
        out = [head] + _stamp(snap, now, short)
        age = _age(snap, now)
        if age is not None and age > db.SNAPSHOT_OLD_S:  # yesterday's prices and countdowns would mislead
            return _fit(out, tail)
        if short:
            # the range and paper recommendation lines sit with the day's items (no blank before them)
            blocks = (
                ("", lambda s, n: _market_short(s, n) + _cycle(s, n)),
                ("", _day_short),
                ("", lambda s, n: _flows(s, n, short=True)),
                ("", _my_options),
                ("", _events_short),
            )
        else:
            blocks = (
                ("Ринок", lambda s, n: _market(s, n) + _cycle(s, n)),
                ("Головне сьогодні", lambda s, n: _key(s, n) + _paper(s, n)),
                ("Потоки BTC · описово, не сигнал", _flows),
                ("Мої опціони Bybit", _my_options),
                ("Події · час " + ("Київ" if db.KYIV is not None else "UTC"), _events),
                ("Funding · річних", _funding),
            )
        for title, fn in blocks:
            try:
                lines = fn(snap, now)
            except Exception as e:  # noqa: BLE001 - one odd section must not drop the brief
                lines = [_h(f"Блок не вдалося зібрати: несподіваний формат даних ({type(e).__name__}).")]
            if lines and title:
                out += ["", f"<b>{title}</b>"] + lines
            elif lines:  # short: the first group sits right under the title, the others after a blank line
                out += lines if len(out) == 1 else [""] + lines
        if not short and (db._data(snap, "funding_btc") or db._data(snap, "funding_eth")):
            out.append("<i>Додатний funding — лонги платять шортам; до комісій і базису.</i>")
        return _fit(out, tail)
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        return _fit([head, _h(f"Знімок не вдалося прочитати ({type(e).__name__}).")], tail)


def panel_base(path: Path | None = None, environ: dict[str, str] | None = None) -> str | None:
    """The dashboard's base URL ending in '/': $TRADEDESK_PANEL_URL when set (non-empty), else the file step 23 wrote.
    Only a plain https URL counts (host, optional path; no credentials, query, fragment or whitespace); None otherwise."""
    base = (os.environ if environ is None else environ).get(PANEL_URL_ENV, "").strip()
    if not base:
        try:
            base = (path or DASHBOARD_URL_PATH).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    try:
        u = urllib.parse.urlsplit(base)
    except ValueError:
        return None
    bad = any(c.isspace() or c in "\"'<>\\" for c in base)
    if bad or u.scheme != "https" or not u.hostname or "@" in u.netloc or u.query or u.fragment:
        return None
    return base.rstrip("/") + "/"


def dashboard_buttons(
    path: Path | None = None, environ: dict[str, str] | None = None, key_file: Path | None = None
) -> list[tuple[str, str]]:
    """(label, url) buttons to the dashboard pages under panel_base(), with the dashboard's key (?k=) when it has one;
    [] when there is no usable URL."""
    base = panel_base(path, environ)
    key = db.dashboard_key(key_file)
    q = f"?{urllib.parse.urlencode({'k': key})}" if key else ""  # the link opens in Telegram's browser too
    return [(label, base + page + q) for label, page in PAGES] if base else []


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
    ap.add_argument("--full", action="store_true", help="the long text with every block instead of the short one")
    ap.add_argument("--no-chart", action="store_true", help="text only")
    ap.add_argument("--topic", default="alerts", help="forum topic (default: alerts)")
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    a = ap.parse_args(argv)
    snap, err = db.load_snapshot(a.snapshot)
    buttons = dashboard_buttons()
    text = build_brief(snap, time.time(), err, short=not a.full)
    png = None if a.no_chart else chart_png(snap)
    change = cycle_change(snap, time.time())
    if a.print_only or a.out:
        if a.print_only:
            print(text)
            links = ", ".join(f"{label} {url}" for label, url in buttons) or f"none (no ${PANEL_URL_ENV} and no {DASHBOARD_URL_PATH})"
            print(f"\n[buttons: {links}]")
            print(f"[chart: {'PNG ' + str(len(png)) + ' bytes' if png else 'none (matplotlib missing or no data)'}]")
            print(f"[cycle change message: {change or 'none'}]")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "brief.html").write_text(text + "\n", encoding="utf-8")
            if png:
                (a.out / "brief.png").write_bytes(png)
        return 0
    ok = tg_send.send(text, silent=a.silent, topic=a.topic, buttons=buttons, per_row=BUTTONS_PER_ROW)
    if ok and png:
        caption = f"BTC 120 днів, ±1σ до {_h(db._exp_label(snap, 'month'))} з опціонів (ризик-нейтрально)"
        tg_send.send_photo(png, caption, silent=True, topic=a.topic)
    if ok and change:  # the card: a change of state is its own message, after the brief
        tg_send.send(change, silent=a.silent, topic=a.topic)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
