"""Check the calls tracker: did each blogger's entry zone get touched, and what did it give? Writes a Markdown report.

    uv run python scripts/calls_check.py [--calls docs/calls/calls.csv] [--out docs/calls/REPORT.md]

Daily bars come from the project's providers (Binance USDT-M perp, then Bybit, then Hyperliquid, unless a call names
its own ``venue:SYMBOL``); only complete UTC days count. Rules of the evaluation: src/tradedesk/analytics/calls.py.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from tradedesk.analytics.calls import Bar, Call, CallResult, evaluate_call, parse_calls, summarize
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

DAY_MS = 86_400_000
STATUS = {
    "waiting": "чекає зону",
    "expired": "не дійшло",
    "open": "у позиції",
    "ended": "закрито за часом",
    "target": "ціль ✓",
    "stopped": "стоп ✗",
    "no_data": "немає даних",
}


def _ms(d: date) -> int:
    return int(datetime.combine(d, time(0), tzinfo=UTC).timestamp() * 1000)


def _day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def candidates(call: Call) -> list[str]:
    if call.symbol:
        return [call.symbol]
    a = call.asset
    return [f"binanceusdm:{a}/USDT:USDT", f"bybit:{a}/USDT:USDT", f"hyperliquid:{a}"]


async def fetch_bars(rt, call: Call, today: date, markets: dict[str, set[str]]) -> tuple[list[Bar], str]:
    """Complete daily bars of the call's window from the first venue that has the market; ([], '') if none."""
    last = min(call.end, today - timedelta(days=1))
    if last < call.start:
        return [], ""
    start_ms, end_ms = _ms(call.start), _ms(last) + DAY_MS - 1
    for spec in candidates(call):
        venue, _, sym = spec.partition(":")
        try:
            if venue == "hyperliquid":
                raw = (await rt.hyperliquid.candles(sym, "1d", start_ms, end_ms)).data or []
                rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])] for c in raw]
            else:
                if venue not in markets:
                    markets[venue] = set(((await rt.exchanges.markets(venue)).data or {}).keys())
                if sym not in markets[venue]:
                    continue
                rows = (await rt.exchanges.ohlcv(venue, sym, "1d", call.horizon_days + 2, since_ms=start_ms, until_ms=end_ms)).data or []
        except (ProviderError, KeyError, TypeError, ValueError) as e:
            logging.warning("%s %s: %s", call.id, spec, e)
            continue
        bars = [Bar(_day(int(r[0])), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if len(r) >= 5]
        bars = [b for b in bars if call.start <= b.day <= last]
        if bars:
            return bars, spec
    return [], ""


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f} %"


def _price(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", " ") if x >= 1000 else f"{x:.4g}"


def _zone(c: Call) -> str:
    return _price(c.zone_low) if c.zone_low == c.zone_high else f"{_price(c.zone_low)}–{_price(c.zone_high)}"


def render(pairs: list[tuple[Call, CallResult, str]], errors: list[str], today: date) -> str:
    out = [
        "# Трекер закликів: звіт",
        "",
        f"Звіт на {today.isoformat()} (`scripts/calls_check.py`; денні свічки перпів, лише завершені дні UTC). "
        "Заклики: [`calls.csv`](calls.csv); правила оцінки: [`src/tradedesk/analytics/calls.py`](../../src/tradedesk/analytics/calls.py).",
        "",
        "Як читати:",
        "- «Результат» — дохідність угоди від входу в зоні до цілі, стопу або останнього закриття, у напрямку заклику.",
        "- «Одразу» — якби діяли в день після заклику, не чекаючи зони. Заклик чогось вартий, якщо результат стабільно кращий.",
        "",
        "## Підсумок за джерелами",
        "",
        "| Джерело | Закликів | З даними | Дійшло до зони | Ціль | Стоп | Сер. результат | Сер. «одразу» |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for source, s in summarize([(c, r) for c, r, _ in pairs]).items():
        out.append(
            f"| {source} | {s['calls']} | {s['with_data']} | {s['touched']} | {s['target']} | {s['stopped']} "
            f"| {_pct(s['mean_result'])} | {_pct(s['mean_hold'])} |"
        )
    out += [
        "",
        "## Заклики",
        "",
        "| id | Дата | Актив | Сторона | Зона | Ціль / стоп | Статус | Дійшло (днів) | Вхід | Результат | Макс. рух | Макс. просадка | «Одразу» | Дані |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c, r, spec in pairs:
        if r.first_open and not c.zone_low / 3 <= r.first_open <= c.zone_high * 3:
            spec += " ⚠ ціна далеко від зони: перевірте тікер"
        touched = f"{r.touched_on.isoformat()} ({r.days_to_touch})" if r.touched_on else "—"
        out.append(
            f"| {c.id} | {c.day.isoformat()} | {c.asset} | {c.side} | {_zone(c)} | {_price(c.target)} / {_price(c.stop)} "
            f"| {STATUS.get(r.status, r.status)} | {touched} | {_price(r.entry)} | {_pct(r.result)} | {_pct(r.max_gain)} "
            f"| {_pct(r.max_drawdown)} | {_pct(r.hold_return)} | {spec or '—'} |"
        )
    if errors:
        out += ["", "## Рядки calls.csv з помилками", ""] + [f"- {e}" for e in errors]
    return "\n".join(out) + "\n"


async def run(calls_text: str, today: date) -> tuple[str, int, int]:
    """The report for the calls in ``calls_text``, the number of calls and of refused rows."""
    calls, errors = parse_calls(calls_text)
    _, rt = build_server()
    pairs: list[tuple[Call, CallResult, str]] = []
    markets: dict[str, set[str]] = {}
    try:
        for c in calls:
            bars, spec = await fetch_bars(rt, c, today, markets)
            pairs.append((c, evaluate_call(c, bars, today), spec))
    finally:
        await rt.aclose()
    return render(pairs, errors, today), len(pairs), len(errors)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--calls", type=Path, default=Path("docs/calls/calls.csv"))
    ap.add_argument("--out", type=Path, default=Path("docs/calls/REPORT.md"))
    ap.add_argument("--today", type=date.fromisoformat, default=datetime.now(UTC).date(), help="UTC date (default: now)")
    a = ap.parse_args()
    report, n, bad = asyncio.run(run(a.calls.read_text(encoding="utf-8"), a.today))
    a.out.write_text(report, encoding="utf-8")
    print(f"{n} calls, {bad} refused rows -> {a.out}")


if __name__ == "__main__":
    main()
