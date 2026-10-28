"""Daily journal of the cycle regime and its evaluation (IDEA-20261028-btc-cycle-regime-gauge, step 2). Pure functions,
no I/O; time comes in.

* `due_days`: the closed UTC days the journal should hold by `now` (a day is due LOG_LAG after its 00:00 UTC close,
  so Binance has published the daily bars and the 00:00 funding settlement), inside a one-week lookback so a collector
  outage heals itself; days already stored are left out.
* `log_row`: one `cycle_regime_log` row from an `analytics.cycle_regime.cycle_regime` result.
* `evaluate_journal`: «state -> BTC return over the next 30 / 90 days» from the journal rows. Counts EPISODES (a run of
  consecutive journal rows with the same state; an undecided day ends the run; a date gap does not, so outages never
  add episodes) as well as days. A horizon is filled only when it has elapsed (the close of day + h is <= now) and the
  journal holds that day's close; returns are simple decimals (0.05 = +5 %) of the Binance spot close.
* `render_ua`: the owner's Ukrainian markdown table.

Days inside one episode, and the 30 / 90-day windows of neighbouring days, overlap: the independent information is
about the number of episodes, not of days. The card judges the journal only after 12 months (kill criterion).
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import cycle_regime as cr

LOG_LAG_MS = 5 * 60_000  # written from 00:05 UTC for the day that closed at 00:00
LOOKBACK_DAYS = 7  # due days reach back a week: a short outage heals, older gaps stay gaps
HORIZONS = (30, 90)
MIN_MONTHS = 12  # the card's forward judgement point
FEW_EPISODES = 5  # below this a cell is marked «мало»
DAYS_PER_MONTH = 30.44

# Explicit columns of cycle_regime_log besides date / base / state / trend / version / created_ts / components / missing.
COMPONENT_COLUMNS = (
    "close",
    "perp_close",
    "sma365",
    "price_to_sma",
    "sma_slope_30d",
    "drawdown_365d",
    "dist_30d_low",
    "rsi14",
    "basis7",
    "funding7",
    "premium7",
)


def _iso(day_open_ms: int) -> str:
    return datetime.fromtimestamp(day_open_ms / 1000, UTC).date().isoformat()


def due_days(now: datetime, have: Iterable[str] = (), lookback: int = LOOKBACK_DAYS) -> list[int]:
    """Close times (unix ms, 00:00 UTC) of the closed days due by `now` and not in `have` (YYYY-MM-DD of the day,
    i.e. of the bar that closes then), oldest first, at most `lookback` days back."""
    now_ms = int(now.timestamp() * 1000)
    last_close = (now_ms - LOG_LAG_MS) // cr.DAY_MS * cr.DAY_MS
    stored = set(have)
    closes = [last_close - k * cr.DAY_MS for k in range(lookback - 1, -1, -1)]
    return [c for c in closes if _iso(c - cr.DAY_MS) not in stored]


def _fnum(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def log_row(result: Mapping[str, Any], base: str, created_ts: int, missing: Sequence[str] = ()) -> dict[str, Any] | None:
    """The journal row of one `cycle_regime` result (`now` = that day's close), or None when it has no state."""
    comps = result.get("components")
    state = result.get("state")
    if not comps or state is None:
        return None
    tc = result.get("trend_confirmed")
    return {
        "date": result["date"],
        "base": base,
        "state": state,
        "trend": result.get("trend"),
        "trend_confirmed": None if tc is None else int(bool(tc)),
        **{c: _fnum(comps.get(c)) for c in COMPONENT_COLUMNS},
        "components": json.dumps({k: comps[k] for k in sorted(comps)}, allow_nan=False, default=str),
        "missing": ",".join(sorted(set(missing))) or None,
        "version": cr.VERSION,
        "created_ts": int(created_ts),
    }


# ------------------------------------------------------------------------------------------------------- evaluation


def _day(s: Any) -> date | None:
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        return None


def episodes(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Runs of consecutive rows (sorted by date) with the same state: {"state", "start", "end", "days"}. A row without
    a state ends the run and starts none."""
    out: list[dict[str, Any]] = []
    prev: str | None = None
    for r in rows:
        s = r.get("state")
        if s is None:
            prev = None
            continue
        if out and s == prev:
            out[-1]["end"] = r["date"]
            out[-1]["days"] += 1
        else:
            out.append({"state": s, "start": r["date"], "end": r["date"], "days": 1})
        prev = s
    return out


def _stats(xs: Sequence[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "share_up": None}
    return {
        "n": len(xs),
        "mean": math.fsum(xs) / len(xs),
        "median": statistics.median(xs),
        "share_up": sum(1 for x in xs if x > 0) / len(xs),
    }


def evaluate_journal(
    rows: Iterable[Mapping[str, Any]], now: datetime, horizons: Sequence[int] = HORIZONS, min_months: float = MIN_MONTHS
) -> dict[str, Any]:
    """Per state and horizon: days and episodes in the journal, and the forward spot return over the elapsed horizons
    by day (`by_day`) and from the first day of each episode (`by_episode`); `all_days` is the unconditional
    baseline (every day with a state). `pending` = horizon not elapsed yet, `no_price` = elapsed but the journal lacks
    that day's close. `thin` = the journal covers fewer than `min_months` months."""
    clean: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _day(r.get("date"))
        c = _fnum(r.get("close"))
        if d is None or c is None or c <= 0 or d.isoformat() in clean:
            continue
        clean[d.isoformat()] = {"date": d.isoformat(), "state": r.get("state"), "close": c, "version": r.get("version")}
    ordered = [clean[k] for k in sorted(clean)]
    price = {r["date"]: r["close"] for r in ordered}
    last_closed = now.astimezone(UTC).date() - timedelta(days=1)  # the bar of `now`'s day has not closed

    def fwd(d: str, h: int) -> float | str:
        t = date.fromisoformat(d) + timedelta(days=h)
        if t > last_closed:
            return "pending"
        p = price.get(t.isoformat())
        return "no_price" if p is None else p / price[d] - 1.0

    eps = episodes(ordered)
    states: dict[str, Any] = {}
    for s in cr.STATES:
        days_s = [r["date"] for r in ordered if r["state"] == s]
        eps_s = [e for e in eps if e["state"] == s]
        by_h: dict[str, Any] = {}
        for h in horizons:
            dr = [fwd(d, h) for d in days_s]
            er = [fwd(e["start"], h) for e in eps_s]
            by_h[str(h)] = {
                "by_day": _stats([x for x in dr if isinstance(x, float)]),
                "by_episode": _stats([x for x in er if isinstance(x, float)]),
                "pending_days": sum(1 for x in dr if x == "pending"),
                "no_price_days": sum(1 for x in dr if x == "no_price"),
            }
        states[s] = {"days": len(days_s), "episodes": len(eps_s), "horizons": by_h}
    all_days: dict[str, Any] = {}
    for h in horizons:
        xs = [fwd(r["date"], h) for r in ordered if r["state"] is not None]
        all_days[str(h)] = _stats([x for x in xs if isinstance(x, float)])
    first = ordered[0]["date"] if ordered else None
    last = ordered[-1]["date"] if ordered else None
    span_days = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 if first and last else 0
    months = span_days / DAYS_PER_MONTH
    return {
        "asof_utc": now.astimezone(UTC).isoformat(timespec="seconds"),
        "first": first,
        "last": last,
        "rows": len(ordered),
        "undecided_days": sum(1 for r in ordered if r["state"] is None),
        "gap_days": max(0, span_days - len(ordered)),
        "months": months,
        "thin": months < min_months,
        "versions": sorted({str(r["version"]) for r in ordered if r["version"]}),
        "horizons": [int(h) for h in horizons],
        "episodes_total": len(eps),
        "states": states,
        "all_days": all_days,
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f} %"


def _cell(st: Mapping[str, Any]) -> str:
    if not st["n"]:
        return "—"
    return f"{_pct(st['mean'])} (n={st['n']})"


def render_ua(ev: Mapping[str, Any], base: str = "BTC") -> str:
    """The owner's markdown: what the journal holds, the caveats first, then the table."""
    hs = ev["horizons"]
    lines = [f"# Режим циклу {base}: стан → дохідність {base} за {' / '.join(str(h) for h in hs)} днів (журнал)", ""]
    if not ev["rows"]:
        lines.append(f"Звіт на {ev['asof_utc']}. Журнал `cycle_regime_log` для {base} порожній: колектор ще нічого не записав.")
        return "\n".join(lines) + "\n"
    lines.append(
        f"Звіт на {ev['asof_utc']}. Журнал: {ev['first']} … {ev['last']} ({ev['rows']} днів, {ev['months']:.1f} міс.; "
        f"епізодів {ev['episodes_total']}; без стану {ev['undecided_days']}; пропущених дат {ev['gap_days']}); "
        f"версія правил: {', '.join(ev['versions']) or '—'}."
    )
    lines.append("")
    if ev["thin"]:
        lines.append(
            f"**Даних замало:** журнал коротший за {MIN_MONTHS} місяців — числа описові, не висновок (картка оцінює журнал "
            f"лише після {MIN_MONTHS} місяців)."
        )
        lines.append("")
    if len(ev["versions"]) > 1:
        lines += ["**Увага:** у журналі кілька версій правил — їх не можна змішувати; рахувати окремо.", ""]
    lines += [
        "Дні одного епізоду й сусідні вікна 30/90 днів перекриваються: незалежних спостережень приблизно стільки, скільки "
        "епізодів, а не днів. «Від початку епізоду» — дохідність від першого дня кожного епізоду. Горизонт рахується лише "
        "тоді, коли він минув. Контекст, не сигнал: стани зафіксовано в картці до даних.",
        "",
    ]
    head = ["Стан", "Днів", "Епізодів"]
    for h in hs:
        head += [f"{h} дн: за днями", f"{h} дн: від початку епізоду", f"{h} дн: ще не минуло"]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for s in cr.STATES:
        st = ev["states"][s]
        row = [cr.STATE_UA[s], str(st["days"]), str(st["episodes"])]
        for h in hs:
            c = st["horizons"][str(h)]
            ep = _cell(c["by_episode"])
            if c["by_episode"]["n"] and c["by_episode"]["n"] < FEW_EPISODES:
                ep += ", мало"
            row += [_cell(c["by_day"]), ep, str(c["pending_days"])]
        lines.append("| " + " | ".join(row) + " |")
    base_row = ["усі дні (безумовно)", str(ev["rows"] - ev["undecided_days"]), str(ev["episodes_total"])]
    for h in hs:
        base_row += [_cell(ev["all_days"][str(h)]), "", ""]
    lines.append("| " + " | ".join(base_row) + " |")
    lines.append("")
    lines.append("Дохідність — проста зміна закриття споту Binance (+5.0 % = 0.05); n — кількість днів або епізодів з результатом.")
    return "\n".join(lines) + "\n"
