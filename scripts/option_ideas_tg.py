"""Weekly option ideas for BTC and ETH at Bybit prices, to Telegram (owner 14.11: instead of the dry forecast-log
numbers — «short, actionable: expiry, strikes, premium, why and R:R»; Bybit, because he trades there).

    uv run python scripts/option_ideas_tg.py             # send to the alerts topic and log the ideas
    uv run python scripts/option_ideas_tg.py --print     # print the Telegram HTML, send and log nothing (settles in memory only)
    uv run python scripts/option_ideas_tg.py --risk 100  # max loss per idea, USD (owner's rule: $100 of $10k)

Quotes: Bybit v5 option tickers (bid/ask, mark IV, delta), daily closes: Binance spot, both through the project's
providers (rt.exchanges, so limits, retries and health apply). Rules of the ideas: the options playbook
(docs/knowledge/options-playbook.md) as ``option_ideas.RULES``; their conditions are measured by our MCP tools, called
in this process (deribit_vrp, deribit_atm_term, macro_calendar, skew_percentile — the last one reads the collector's
history in TRADEDESK_DB_PATH, defaulting to --db), so the numbers are the ones the chat sees. Up to 3 ideas per coin,
strongest evidence first, each with its rule and evidence status; ✗ and unmeasured rules are never sent.
Every sent idea is appended to IDEAS_LOG (JSON lines, with all legs and prices). Each run first settles the open
records whose expiry has passed: Bybit's delivery price of one of the idea's legs (v5 market/delivery-price, through
rt.exchanges) → ``option_ideas.settle_idea`` (USD and R), written back in place of the open record (once: settled
records are never touched again; malformed lines are kept as they are; no delivery price → the record stays open
until the next run). The message then carries a per-rule line of the settled record (n, sum of R, share in plus;
a conclusion only from ``option_ideas.MIN_SETTLED`` ideas over ``MIN_CLUSTERS`` expiries, its standard error per
expiry cluster, since the ideas of one expiry are not independent). The forecast-log line under the ideas comes from
scripts/forecast_weekly_tg.py's own summary functions (offline, read-only).

The ideas are hypotheses, not advice: risk-neutral probabilities, no tested edge of the structures after spreads.
Scheduled by deploy/user-units/tradedesk-forecast-weekly.* (Saturday 12:00 Europe/Kyiv, while its flag file exists).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
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


VERDICT = {"plus": "плюс", "minus": "мінус", "flat": "від нуля не відрізняється"}


def journal_lines(records: list[dict]) -> list[str]:
    """The ideas journal under the ideas: one line per rule with settled ideas (n, sum of R, share in plus; a
    conclusion only from oi.MIN_SETTLED ideas over oi.MIN_CLUSTERS expiries of that rule, as the mean R per expiry
    ± 2 cluster standard errors), or a count of the open ones; [] for an empty journal."""
    recs = [r for r in records if isinstance(r, dict)]
    n_open = sum(1 for r in recs if r.get("status") == "open")
    summ = oi.rule_summary(recs)
    if not summ:
        return [_h(f"Журнал ідей: відкритих {n_open}, розрахованих на експірації ще нема.")] if n_open else []
    out = [_h(f"Журнал ідей (ціна поставки Bybit; R = результат / макс. збиток; відкритих {n_open}):")]
    for code, s in summ.items():
        name = f"{code} {oi.RULES[code].name}" if code in oi.RULES else code
        line = f"{name}: n {s['n']}, сума R {s['sum_r']:+.2f}, у плюсі {s['win_share']:.0%}"
        if s["verdict"] is not None:
            line += (
                f" · висновок: {VERDICT[s['verdict']]} (середній R по експіраціях {s['cluster_mean_r']:+.2f}"
                f" ± {2 * s['se_r']:.2f}, експірацій {s['n_clusters']})"
            )
        elif s["n"] < oi.MIN_SETTLED:
            line += f" · n<{oi.MIN_SETTLED}, ще без висновку"
        else:
            line += f" · експірацій {s['n_clusters']}<{oi.MIN_CLUSTERS}, ще без висновку"
        out.append(_h(line))
    return out


def build_message(
    per_coin: list[tuple[list[oi.Idea], dict]], now: float, risk: float, fline: str = "", journal: list[str] | None = None
) -> str:
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
    tail = [""] + list(journal or []) + ([fline] if fline else []) + [f"<i>{not_sent_line()}</i>", f"<i>{_h(CAVEAT)}</i>"]
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


Row = tuple[str, dict | None]  # a journal line as read, and its record (None when the line is not a JSON object)


def read_log(path: Path = IDEAS_LOG) -> list[Row]:
    """The journal's lines with their records; blank lines dropped, malformed ones kept as None (never raises).
    Lines are split on "\\n" only: ``str.splitlines`` would also cut at U+2028 / U+2029 / U+0085, which an older
    record may hold unescaped inside a JSON string (external text in its context)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[Row] = []
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        rows.append((line, rec if isinstance(rec, dict) else None))
    return rows


def _key(rec: dict) -> tuple[str, int] | None:
    exp = oi._num(rec.get("expiry_ms"))
    return (rec["coin"], int(exp)) if isinstance(rec.get("coin"), str) and exp is not None else None


def due(rows: list[Row], now_ms: int) -> dict[tuple[str, int], list[str]]:
    """(coin, expiry_ms) of the open records past expiry → Bybit symbols of their legs to ask the delivery price for."""
    out: dict[tuple[str, int], list[str]] = {}
    for _, rec in rows:
        if rec is None or rec.get("status") != "open" or (k := _key(rec)) is None or k[1] > now_ms:
            continue
        legs = rec.get("legs")
        for leg in legs if isinstance(legs, list) else []:
            if isinstance(leg, dict) and oi._num(leg.get("strike")) is not None and isinstance(leg.get("is_call"), bool):
                try:
                    sym = oi.bybit_option_symbol(k[0], k[1], float(leg["strike"]), leg["is_call"])
                except (ValueError, OverflowError, OSError):  # an expiry_ms no calendar holds
                    break
                if sym not in out.setdefault(k, []):
                    out[k].append(sym)
    return out


async def fetch_deliveries(rt, wanted: dict[tuple[str, int], list[str]]) -> dict[tuple[str, int], float]:
    """Bybit delivery price per (coin, expiry_ms): the first leg symbol that has one (one price per expiry); a failed
    or empty answer leaves the expiry out, so its records stay open. Never raises."""
    out: dict[tuple[str, int], float] = {}
    for key, symbols in wanted.items():
        for sym in symbols:
            try:
                body = (
                    await rt.exchanges.public_get("bybit", "publicGetV5MarketDeliveryPrice", {"category": "option", "symbol": sym})
                ).data
            except Exception:  # noqa: BLE001 - e.g. "The contract doesn't exist": try the next leg
                continue
            px = oi.parse_bybit_delivery(body, key[1])
            if px is not None:
                out[key] = px
                break
    return out


def settle_rows(rows: list[Row], prices: dict[tuple[str, int], float], now_ms: int) -> tuple[list[Row], int]:
    """Open records with a delivery price become settled (``oi.settle_idea``); every other line is kept as it was.
    Idempotent: a settled record is never settled again."""
    out: list[Row] = []
    n = 0
    for line, rec in rows:
        k = _key(rec) if rec is not None and rec.get("status") == "open" else None
        s = oi.settle_idea(rec, prices[k], now_ms) if k in prices else None
        if s is None:
            out.append((line, rec))
            continue
        new = {**rec, **s}
        out.append((json.dumps(new, ensure_ascii=True), new))  # ASCII: no line separator can end up raw in the file
        n += 1
    return out, n


def write_log(rows: list[Row], path: Path = IDEAS_LOG) -> bool:
    """Replace the journal with ``rows`` atomically (a uniquely named temp file beside it, then rename); False on an
    OS error (the temp file is then removed)."""
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(line + "\n" for line, _ in rows))
        with contextlib.suppress(OSError):  # mkstemp makes it 0600: keep the journal's own mode
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except OSError as e:
        print(f"option ideas journal not updated: {e}", file=sys.stderr)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


async def gather(
    risk: float, now_ms: int, wanted: dict[tuple[str, int], list[str]] | None = None
) -> tuple[list[tuple[list[oi.Idea], dict]], dict[tuple[str, int], float]]:
    """The ideas per coin, and the Bybit delivery prices of the journal's ``wanted`` expiries."""
    from mcp import Client

    from tradedesk.server import build_server

    server, rt = build_server()
    out = []
    prices: dict[tuple[str, int], float] = {}
    try:
        prices = await fetch_deliveries(rt, wanted or {})
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
    return out, prices


def log_ideas(per_coin: list[tuple[list[oi.Idea], dict]], now: float, path: Path = IDEAS_LOG) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as f:
        for ideas, ctx in per_coin:
            for idea in ideas:
                row = {"ts": int(now), "status": "open", "context": {k: v for k, v in ctx.items() if k != "skip"}, **idea.to_dict()}
                f.write(json.dumps(row, ensure_ascii=True) + "\n")  # ASCII: U+2028 etc. never raw in a line
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
    now_ms = int(now * 1000)
    rows = read_log(IDEAS_LOG)
    per_coin, prices = asyncio.run(gather(a.risk, now_ms, due(rows, now_ms)))
    rows, n_settled = settle_rows(rows, prices, now_ms)
    journal = journal_lines([rec for _, rec in rows if rec is not None])
    text = build_message(per_coin, now, a.risk, forecast_line(a.db), journal)
    if a.print_only:
        print(text)
        return 0
    if n_settled:
        write_log(rows, IDEAS_LOG)  # before sending: a settlement is a fact, whether the message goes out or not
    ok = tg_send.send(text, topic=a.topic, buttons=fw.journal_buttons())
    if ok:
        log_ideas(per_coin, now, IDEAS_LOG)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
