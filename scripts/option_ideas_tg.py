"""Weekly option ideas for BTC and ETH at Bybit prices, to Telegram (owner 14.11: instead of the dry forecast-log
numbers — «short, actionable: expiry, strikes, premium, why and R:R»; Bybit, because he trades there).

    uv run python scripts/option_ideas_tg.py             # send to the alerts topic and log the ideas
    uv run python scripts/option_ideas_tg.py --print     # print the Telegram HTML, send and log nothing
    uv run python scripts/option_ideas_tg.py --risk 100  # max loss per idea, USD (owner's rule: $100 of $10k)

Quotes: Bybit v5 option tickers (bid/ask, mark IV, delta), daily closes: Binance spot, both through the project's
providers (rt.exchanges, so limits, retries and health apply). Rules of the ideas: the options playbook
(docs/knowledge/options-playbook.md) as ``option_ideas.RULES``; their conditions are measured by our MCP tools, called
in this process (deribit_vrp, deribit_atm_term, macro_calendar, skew_percentile — the last one reads the collector's
history in TRADEDESK_DB_PATH, defaulting to --db), so the numbers are the ones the chat sees. Up to 3 ideas per coin,
strongest evidence first, each with its rule and evidence status; ✗ and unmeasured rules are never sent.
Every sent idea is appended to IDEAS_LOG (JSON lines, with all legs and prices) so it can be settled at expiry
(``option_ideas.payoff_at``) and the record judged; the forecast-log line under the ideas comes from
scripts/forecast_weekly_tg.py's own summary functions (offline, read-only).

The ideas are hypotheses, not advice: risk-neutral probabilities, no tested edge of the structures after spreads.
Scheduled by deploy/user-units/tradedesk-forecast-weekly.* (Saturday 12:00 Europe/Kyiv, while its flag file exists).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402
import forecast_weekly_tg as fw  # noqa: E402
import tg_send  # noqa: E402

from tradedesk.analytics import forecast_log as fl  # noqa: E402
from tradedesk.analytics import option_ideas as oi  # noqa: E402
from tradedesk.collector.assistant_snapshot import _call as call_tool  # noqa: E402 - one MCP call -> {ok, data, …}, never raises

COINS = ("BTC", "ETH")
IDEAS_LOG = Path(os.environ.get("TRADEDESK_OPTION_IDEAS_LOG", "/var/lib/tradedesk-mcp/assistant/option_ideas.jsonl"))
KIND = {
    "iron_condor": "коридор (залізний кондор)",
    "put_debit_spread": "ведмежий пут-спред",
    "call_debit_spread": "бичачий кол-спред",
    "long_strangle": "купити рух (стренгл)",
}
TREND = {"bear": "ведмежий", "bull": "бичачий", "neutral": "нейтральний"}
CAVEAT = (
    "Ціни — bid/ask Bybit на момент розрахунку з комісією тейкера; ймовірності ризик-нейтральні (з цін опціонів), не прогноз. "
    "Переваги цих конструкцій не доведено: кожна ідея пишеться в паперовий журнал і буде оцінена на експірації."
)
_h = ab._h


def _usd(x: float) -> str:
    return f"${x:,.0f}".replace(",", " ")


def _px(x: float, coin: str) -> str:
    return f"{x:,.0f}".replace(",", " ") if coin == "BTC" or x >= 1000 else f"{x:,.1f}"


def _legs_line(idea: oi.Idea) -> str:
    parts = []
    for leg in idea.legs:
        verb = "продати" if leg.action == "sell" else "купити"
        parts.append(f"{verb} {'кол' if leg.is_call else 'пут'} {_px(leg.strike, idea.coin)}")
    return " · ".join(parts)


def idea_block(n: int, idea: oi.Idea, risk: float) -> list[str]:
    c = idea.coin
    head = f"<b>{n}. {c} · {KIND.get(idea.kind, idea.kind)} · {oi.expiry_label(idea.expiry_ms)} ({idea.days:.0f} дн)</b>"
    if idea.net > 0:
        money = f"Премія {_usd(idea.net)} · ризик {_usd(idea.max_loss)}"
    else:
        money = f"Ціна {_usd(-idea.net)} (це і є ризик)"
    rr = idea.reward_to_risk
    if idea.max_profit is None:
        money += " · прибуток без межі"
    elif rr:
        ratio = f"1 : {1 / rr:.1f}" if rr < 1 else f"{rr:.1f} : 1"
        money += f" · макс. прибуток {_usd(idea.max_profit)} · прибуток : ризик = {ratio}"
    be = " і ".join(_px(b, c) for b in idea.breakevens)
    if idea.kind == "iron_condor":
        where = f"заробляє, якщо {oi.expiry_label(idea.expiry_ms)} {c} між {be}"
    elif idea.kind == "long_strangle":
        where = f"заробляє, якщо {c} нижче або вище за {be}"
    else:
        where = f"беззбиток {be}"
    if idea.prob_profit is not None:
        where += f" (≈ {idea.prob_profit:.0%})"
    if idea.qty > 0:
        sz = f"Розмір під {_usd(risk)}: <b>{idea.qty:g} {c}</b> (ризик {_usd(idea.qty * idea.max_loss)})"
    else:
        sz = f"Мін. лот {oi.LOT[c]:g} {c} ризикує {_usd(oi.LOT[c] * idea.max_loss)} — більше за {_usd(risk)}"
    out = [head, _h(_legs_line(idea)), _h(f"{money}; {where}"), sz, f"<i>Чому: {_h(idea.why)}</i>"]
    rule = oi.RULES.get(idea.rule)
    if rule:
        out.append(_h(f"Правило: {rule.code} {rule.name} · докази: {rule.evidence}"))
    return out


def note_line(code: str, ctx: dict, coin: bool = True) -> str:
    """A context rule whose condition holds (a tag or a risk rule, not a trade)."""
    rule, chk = oi.RULES[code], ctx["checks"][code]
    who = f"{ctx['coin']}: " if coin else ""
    return _h(f"{who}{chk['detail']}. Правило: {rule.code} {rule.name} · докази: {rule.evidence} (контекст, не угода)")


def not_sent_line() -> str:
    bad = [f"{r.code} {r.name}" for r in oi.RULES.values() if r.kind == "trade" and r.status == "✗"]
    return _h(f"Не надсилаємо (✗ у плейбуку): {'; '.join(bad)}. Правила й докази: {oi.PLAYBOOK}.")


def ctx_line(ctx: dict) -> str:
    c = ctx["coin"]
    bits = [f"{c} {_px(ctx['spot'], c)}" if ctx.get("spot") else c, f"тренд {TREND.get(ctx.get('trend'), '—')}"]
    if ctx.get("iv") and ctx.get("rv30"):
        bits.append(f"IV {ctx['iv']:.0%} / реальна 30д {ctx['rv30']:.0%}")
    if ctx.get("skip"):
        bits.append("немає ліквідної експірації 7–24 дні")
    return _h(" · ".join(bits))


def forecast_line(path: Path) -> str:
    rows, _err = fw.load_forecasts(path)
    try:
        s = fl.score(rows or [])
    except Exception:  # noqa: BLE001 - the ideas go out without this line
        return ""
    if not rows or not s.get("n"):
        return ""
    dates = s.get("n_expiry_dates") or 0
    if dates < fl.MIN_EXPIRIES:
        return _h(
            f"Журнал прогнозів: Brier {fw._bs(s['brier'])} на {dates} датах експірації — замало для висновку (потрібно ≥ {fl.MIN_EXPIRIES})."
        )
    return _h(f"Журнал прогнозів: Brier {fw._bs(s['brier'])} на {dates} датах, навичка проти еталона {fw._skill(s.get('skill_vs_ref'))}.")


def build_message(per_coin: list[tuple[list[oi.Idea], dict]], now: float, risk: float, fline: str = "") -> str:
    head = f"<b>Опціонні ідеї на тиждень · {_h(db.kyiv(datetime.fromtimestamp(now, UTC)))}{ab._tz()}</b>"
    body = [head, *(ctx_line(ctx) for _, ctx in per_coin)]
    shown: set[str] = set()
    notes = []
    for _, ctx in per_coin:
        for code in ctx.get("notes") or []:
            if code in oi.MARKET_WIDE:
                if code not in shown:
                    shown.add(code)
                    notes.append(note_line(code, ctx, coin=False))
            else:
                notes.append(note_line(code, ctx))
    if notes:
        body += ["", *notes]
    n = 0
    for ideas, _ in per_coin:
        for idea in ideas:
            n += 1
            body += ["", *idea_block(n, idea, risk)]
    if not n:
        body += ["", _h("Цього тижня ідей нема: умови правил плейбуку, що надсилаються, не виконані (або немає ліквідних цін Bybit).")]
    tail = [""] + ([fline] if fline else []) + [f"<i>{not_sent_line()}</i>", f"<i>{_h(CAVEAT)}</i>"]
    return ab._fit(body, tail)


def _data(sec: dict) -> dict | None:
    return sec.get("data") if sec.get("ok") and isinstance(sec.get("data"), dict) else None


async def market_for(client, coin: str, calendar: dict | None) -> dict:
    """The MCP tools' data the playbook rules read for one coin (a failed tool leaves its rules unmeasured)."""
    vrp, term = await asyncio.gather(
        call_tool(client, "deribit_vrp", {"currency": coin, "history_days": 1000}),
        call_tool(client, "deribit_atm_term", {"currency": coin}),
    )
    m = {"vrp": _data(vrp), "term": _data(term), "calendar": calendar, "skew": None}
    iso = oi.month_expiry_iso(m["term"])
    if iso:
        args = {"currency": coin, "expiry": iso, "delta_target": "25", "lookback_days": oi.SKEW_LOOKBACK_DAYS, "min_points": 2}
        m["skew"] = _data(await call_tool(client, "skew_percentile", args))
    return m


async def gather(risk: float, now_ms: int) -> list[tuple[list[oi.Idea], dict]]:
    from mcp import Client

    from tradedesk.server import build_server

    server, rt = build_server()
    out = []
    try:
        async with Client(server) as c:
            calendar = _data(await call_tool(c, "macro_calendar", {"horizon_days": 14}))
            for coin in COINS:
                try:
                    market = await market_for(c, coin, calendar)
                    body = (
                        await rt.exchanges.public_get("bybit", "publicGetV5MarketTickers", {"category": "option", "baseCoin": coin})
                    ).data
                    rows = ((body or {}).get("result") or {}).get("list") or []
                    bars = (await rt.exchanges.ohlcv("binance", f"{coin}/USDT", "1d", 260)).data or []
                    closes = [float(b[4]) for b in bars[:-1] if isinstance(b, list | tuple) and len(b) >= 5]  # complete days only
                    out.append(oi.ideas_for(coin, oi.parse_bybit_tickers(rows, coin), closes, now_ms, risk, market=market))
                except Exception as e:  # noqa: BLE001 - one coin failing must not drop the other
                    out.append(([], {"coin": coin, "skip": f"{type(e).__name__}"}))
    finally:
        await rt.aclose()
    return out


def log_ideas(per_coin: list[tuple[list[oi.Idea], dict]], now: float, path: Path = IDEAS_LOG) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as f:
        for ideas, ctx in per_coin:
            for idea in ideas:
                row = {"ts": int(now), "status": "open", "context": {k: v for k, v in ctx.items() if k != "skip"}, **idea.to_dict()}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML; send and log nothing")
    ap.add_argument("--risk", type=float, default=100.0, help="max loss per idea, USD (default 100)")
    ap.add_argument("--db", type=Path, default=db.JOURNAL_DB, help="SQLite file of the forecast log")
    ap.add_argument("--topic", default="alerts")
    a = ap.parse_args(argv)
    os.environ.setdefault("TRADEDESK_DB_PATH", str(a.db))  # skew_percentile reads the collector's history there
    now = time.time()
    per_coin = asyncio.run(gather(a.risk, int(now * 1000)))
    text = build_message(per_coin, now, a.risk, forecast_line(a.db))
    if a.print_only:
        print(text)
        return 0
    ok = tg_send.send(text, topic=a.topic, buttons=fw.journal_buttons())
    if ok:
        log_ideas(per_coin, now)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
