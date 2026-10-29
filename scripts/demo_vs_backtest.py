"""Demo vs backtest: replay every closed demo trade on historical candles (BACKLOG 0.5, item a).

    uv run python scripts/demo_vs_backtest.py [--since YYYY-MM-DD] [--days 7] [--venue bybit] [--db PATH]
                                              [--out FILE] [--json FILE]

For each closed ``demo_trade`` row (read over a ``mode=ro`` SQLite connection: no migration, no write) the candles of
the signal timeframe (the live scan's window) and the 1h path are fetched through ``rt.exchanges`` (``core`` cache,
rate limits, health; paged with an upper bound per page), and the pure ``analytics/demo_vs_backtest.py`` decides:
match / no signal / mismatch (or pending / no data / unsupported). The summary is a running total from the first demo
trade (or ``--since``): a trade the backtest had not resolved last week is counted once it is, and the 30-trade
threshold applies to the total. A separate block lists the trades closed in the last ``--days``. A second block
(``analytics/demo_shadow.py``, no network) sets the signals the demo selected but did not execute (missed maker entries,
skipped by score or daily caps, refused by the guard) against their shadow plans, which the collector already
resolved: R by reason, next to the shadow plans of the filled trades. The running total is also split by the coin's
turnover on the entry day (the Bybit daily candle, UTC: >= $5M, $1-5M, < $1M, unknown; owner 29.10), so the thin coins
the rare setups scan from 29.10 are not mixed with the ones the backtests covered; each group keeps the 30-trade rule.
The text (Ukrainian, for the owner) is printed and, with ``--out``, written
to a file; ``--json`` writes the rows, both summaries, the turnover split and the not-executed block (schema ``JSON_SCHEMA``, replaced
atomically, so the dashboard «Демо» tab can show the numbers without network). Read-only public market data: nothing
here places, changes or cancels an order.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import demo_shadow
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.demo_vs_backtest import (
    LIVE_WINDOW,
    TURNOVER_GROUPS,
    DemoTrade,
    Replay,
    bybit_linear_id,
    day_turnover,
    entry_day_ms,
    replay,
    summary,
    summary_by_turnover,
    turnover_group,
)
from tradedesk.analytics.setup_signals import TF_MS
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.execution.demo_trader import DEFAULT_DB, MAX_HOLD_MS  # read-only constants
from tradedesk.execution.guard import BE_PATH  # read-only constant
from tradedesk.runtime import Runtime

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
PAGE = 1000  # Bybit caps klines at 1000 a request; the cursor follows the last bar, so smaller caps page too
MAX_PAGES = 12
# 2: + not_executed, schema, generated_ms, venue, notes (1 had summary, week, rows). Additive since (no bump, readers
# skip what they do not know): not_executed.groups.refused with .excluded (24.10); by_turnover and rows[].turnover_usd /
# .turnover_group (29.10)
JSON_SCHEMA = 2
MAX_ROWS = 100_000

Fetch = Callable[[str, str, int, int], Awaitable[list[list[float]]]]
DayFetch = Callable[[str, int, int], Awaitable[list[Any]]]  # (Bybit linear id, start ms, end ms) -> raw daily klines


def load_closed(db: str, since_ms: int, until_ms: int) -> tuple[list[dict[str, Any]], str | None]:
    """Closed demo trades with ``closed_ts`` in [since, until) over a read-only connection; (rows, error)."""
    path = Path(db)
    if not path.exists():
        return [], f"no database at {db}"
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return [], f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM demo_trade WHERE status = 'closed' AND closed_ts >= ? AND closed_ts < ? ORDER BY closed_ts",
            (since_ms, until_ms),
        )
        return [dict(r) for r in cur.fetchall()], None
    except sqlite3.Error as e:
        return [], f"query failed: {type(e).__name__}"
    finally:
        con.close()


def load_not_executed(db: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """(demo_trade rows, exec_audit 'skipped' and 'refused' rows, demo shadow trade_plan rows, error) over a read-only
    connection; a missing table gives an empty list (an older store). Never raises."""
    path = Path(db)
    if not path.exists():
        return [], [], [], f"no database at {db}"
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return [], [], [], f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        def q(table: str, sql: str) -> list[dict[str, Any]]:
            return [dict(r) for r in con.execute(sql, (MAX_ROWS,))] if table in names else []

        trades = q("demo_trade", "SELECT * FROM demo_trade ORDER BY signal_ts LIMIT ?")
        audit = q(
            "exec_audit",
            "SELECT ts, kind, link_id, symbol, setup, payload FROM exec_audit WHERE kind IN ('skipped', 'refused') ORDER BY id LIMIT ?",
        )
        plans = q("trade_plan", f"SELECT * FROM trade_plan WHERE tags LIKE '%{demo_shadow.SHADOW_TAG}%' ORDER BY id LIMIT ?")
        return trades, audit, plans, None
    except sqlite3.Error as e:
        return [], [], [], f"query failed: {type(e).__name__}"
    finally:
        con.close()


def to_bars(rows: list[list[float]], bar_ms: int) -> Bars:
    good: dict[int, list[float]] = {}
    for x in rows:
        try:
            ts, o, h, lo, c = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])
        except (TypeError, ValueError, IndexError):
            continue
        if min(o, h, lo, c) > 0 and h >= lo:
            good[ts] = [ts, o, h, lo, c]
    return Bars.from_rows([good[k] for k in sorted(good)], bar_ms)


def venue_fetch(rt: Runtime, b: Builder, venue: str) -> Fetch:
    """OHLCV rows of [since, until] paged forward with an explicit upper bound per page; failures go to ``b.missing``."""

    async def fetch(symbol: str, tf: str, since_ms: int, until_ms: int) -> list[list[float]]:
        tf_ms = TF_MS[tf]
        rows: list[list[float]] = []
        cursor = since_ms // tf_ms * tf_ms
        for page in range(MAX_PAGES):
            until = min(cursor + PAGE * tf_ms - 1, until_ms)
            got = await b.guard(f"ohlcv:{venue}:{symbol}:{tf}", f"ccxt:{venue}", rt.exchanges.ohlcv(venue, symbol, tf, PAGE, cursor, until))
            if got is None:
                if page:
                    b.warn(f"{venue} {symbol} {tf}: a later page failed; the replay may say no_data")
                break
            good = [x for x in got if isinstance(x, (list, tuple)) and len(x) >= 5 and x[0] is not None] if isinstance(got, list) else []
            rows += good
            last = max((int(x[0]) for x in good), default=None)
            nxt = last + tf_ms if last is not None and last + tf_ms <= until else until + 1
            if nxt > until_ms or nxt <= cursor:
                break
            cursor = nxt
        return rows

    return fetch


async def replay_all(rows: list[dict[str, Any]], fetch: Fetch, now_ms: int) -> list[Replay]:
    out: list[Replay] = []
    for row in rows:
        t = DemoTrade.from_row(row)
        tf_ms = TF_MS.get(t.timeframe)
        hold = MAX_HOLD_MS.get(t.timeframe, 7 * DAY_MS)
        if tf_ms is None:
            out.append(replay(t, Bars.from_rows([], HOUR_MS), Bars.from_rows([], HOUR_MS), now_ms=now_ms, hold_ms=hold))
            continue
        close = t.signal_ts + tf_ms
        bars = to_bars(await fetch(t.symbol, t.timeframe, t.signal_ts - (LIVE_WINDOW - 1) * tf_ms, close - 1), tf_ms)
        path = to_bars(await fetch(t.symbol, "1h", close, min(close + hold, now_ms) - 1), HOUR_MS)
        out.append(replay(t, bars, path, now_ms=now_ms, hold_ms=hold, be_frac=BE_PATH))
    return out


def bybit_day_fetch(rt: Runtime, b: Builder) -> DayFetch:
    """Raw Bybit v5 daily klines of a linear symbol in [start, end] (one request: the caller pages by ``PAGE`` days),
    through ``rt.exchanges.public_get`` (ccxt's unified OHLCV keeps the base volume only); failures go to ``b.missing``."""

    async def fetch(sym: str, start_ms: int, end_ms: int) -> list[Any]:
        params = {"category": "linear", "symbol": sym, "interval": "D", "start": start_ms, "end": end_ms, "limit": PAGE}
        got = await b.guard(f"kline:bybit:{sym}:D", "ccxt:bybit", rt.exchanges.public_get("bybit", "publicGetV5MarketKline", params))
        lst = (got.get("result") or {}).get("list") if isinstance(got, dict) else None
        return [x for x in lst if isinstance(x, (list, tuple))] if isinstance(lst, list) else []

    return fetch


async def add_turnover(trades: list[dict[str, Any]], rows: list[Replay], fetch_day: DayFetch, now_ms: int) -> list[Replay]:
    """``rows`` (the replays of ``trades``, same order) with the entry day's turnover and its group; one fetch per
    symbol and ``PAGE`` days. A trade without a Bybit USDT perpetual id, a closed entry day or a candle stays 'unknown'."""
    days = [entry_day_ms(DemoTrade.from_row(t)) for t in trades]
    need: dict[str, set[int]] = {}
    for x, d in zip(rows, days, strict=True):
        sid = bybit_linear_id(x.symbol)
        if sid is not None and d is not None and d + DAY_MS <= now_ms:
            need.setdefault(sid, set()).add(d)
    klines: dict[str, list[Any]] = {}
    for sid, ds in sorted(need.items()):
        lo, hi = min(ds), max(ds)
        got: list[Any] = []
        for start in range(lo, hi + 1, PAGE * DAY_MS):
            got += await fetch_day(sid, start, min(start + PAGE * DAY_MS, hi + DAY_MS) - 1)
        klines[sid] = got
    out = []
    for x, d in zip(rows, days, strict=True):
        sid = bybit_linear_id(x.symbol)
        t = None if sid is None or d is None else day_turnover(klines.get(sid, []), d, now_ms)
        out.append(dataclasses.replace(x, turnover_usd=t, turnover_group=turnover_group(t)))
    return out


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _r(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


VERDICT_UA = {
    "match": "збіг",
    "no_signal": "немає сигналу",
    "mismatch": "розбіжність",
    "pending": "бектест ще відкритий",
    "no_data": "немає даних",
    "unsupported": "не відтворюється",
}
REASON_UA = {
    "side": "сторона",
    "entry": "вхід",
    "stop": "стоп",
    "target": "ціль",
    "r": "R",
    "no demo R": "немає R демо",
    "be_rule": "лише зміна правила беззбитку",
}
TURNOVER_UA = {"ge5m": "≥ $5M", "1to5m": "$1–5M", "lt1m": "< $1M", "unknown": "оборот невідомий"}


def _counts(s: dict[str, Any]) -> str:
    c = s["counts"]
    return (
        f"закритих демо-угод {s['n']}. Звірено {s['decided']}: збіг {c['match']}, немає сигналу {c['no_signal']}, "
        f"розбіжність {c['mismatch']}. Не вирішено: бектест ще відкритий {c['pending']}, немає даних {c['no_data']}, "
        f"не відтворюється {c['unsupported']}."
    )


def _band(lo: float | None, hi: float | None) -> str:
    return "—" if lo is None or hi is None else f"[{_r(min(lo, hi))}; {_r(max(lo, hi))}]"


def render(
    rows: list[Replay],
    total: dict[str, Any],
    week: dict[str, Any],
    week_ms: int,
    until_ms: int,
    venue: str,
    notes: list[str],
    by_turnover: dict[str, dict[str, Any]] | None = None,
) -> str:
    """``rows``: the running total (all closed demo trades replayed); ``week``: the summary of those closed since
    ``week_ms``, whose rows get the table; ``by_turnover``: ``summary_by_turnover`` of the running total (a block per
    turnover group when given)."""
    first = min((x.closed_ts for x in rows if x.closed_ts is not None), default=until_ms)
    lines = [
        f"# Демо проти бектесту, наростаючим підсумком {_d(first)} — {_d(until_ms)}",
        "",
        f"Від першої демо-угоди: {_counts(total)}",
        f"**Висновок:** {total['conclusion']}.",
    ]
    for setup, c in sorted(total["per_setup"].items()):
        open_ = c["pending"] + c["no_data"] + c["unsupported"]
        lines.append(f"- {setup}: збіг {c['match']}, немає сигналу {c['no_signal']}, розбіжність {c['mismatch']}, не вирішено {open_}.")
    if total["n_r"]:
        lines.append(
            f"Середнє R в одиницях ризику бектесту ({total['n_r']} угод): демо {_r(total['mean_r_demo_bt'])}, "
            f"бектест {_r(total['mean_r_bt'])}, різниця {_r(total['mean_r_diff'])}."
        )
    if total["n_entry"]:
        lines.append(
            f"Вхід демо проти закриття сигнальної свічки ({total['n_entry']} угод): у середньому {_r(total['mean_entry_edge_r'])}R "
            "(+ означає, що демо зайшло краще)."
        )
    if total["mismatch_reasons"]:
        lines.append(
            "Причини розбіжностей: " + ", ".join(f"{REASON_UA.get(k, k)} {v}" for k, v in sorted(total["mismatch_reasons"].items())) + "."
        )
    if by_turnover is not None:
        lines += ["", "## За оборотом монети в день входу (денна свічка Bybit, UTC)", ""]
        for g in TURNOVER_GROUPS:
            s = by_turnover.get(g)
            if not isinstance(s, dict):
                continue
            tail = f" Різниця R (демо − бектест) {_r(s['mean_r_diff'])} на {s['n_r']} угодах." if s["n_r"] else ""
            lines.append(f"- {TURNOVER_UA[g]}: {_counts(s)}{tail} **Висновок:** {s['conclusion']}.")
        lines.append(
            "З 29.10 рідкі сетапи (S14, S16, S38, S39, S40) сканують монети від $1M обороту, а бектести сетапів робились на "
            "топ-30: звірку дрібних монет читати окремо. Оборот дня включає години після входу; «невідомий» — свічки немає, "
            "день ще не закрився або запит не вдався."
        )
    week_rows = [x for x in rows if x.closed_ts is not None and x.closed_ts >= week_ms]
    lines += ["", f"## Закриті {_d(week_ms)} — {_d(until_ms)}", "", f"За період: {_counts(week)}"]
    if week_rows:
        lines += [
            "",
            "| угода | сетап | сторона | вердикт | причини | вхід демо, R (план / заповнення) | Δ стопу, R | R демо (в од. бектесту) "
            "| R бектесту [від; до] | R бектесту від заповнення [від; до] | вихід демо / бектест |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for x in week_rows:
            ed = "—" if x.entry_edge_r is None else f"{_r(x.plan_edge_r)} / {_r(x.entry_edge_r)}"
            sd = "—" if x.stop_diff_r is None else f"{x.stop_diff_r:.2f}"
            bt = "—" if x.r_bt is None else f"{_r(x.r_bt)} {_band(x.r_bt, x.r_bt_fast)}"
            why = ", ".join(REASON_UA.get(k, k) for k in x.reasons) or (x.note if x.verdict != "match" else "")
            lines.append(
                f"| {x.symbol.split(':')[0]} {_d(x.signal_ts)} | {x.setup} {x.timeframe} | {'лонг' if x.side == 'long' else 'шорт'} | "
                f"{VERDICT_UA.get(x.verdict, x.verdict)} | {why} | {ed} | {sd} | {_r(x.r_demo)} ({_r(x.r_demo_bt)}) | {bt} | "
                f"{_band(x.r_bt_fill, x.r_bt_fill_fast)} | {x.demo_exit or '—'} / {x.bt_exit or '—'} |"
            )
    lines += [
        "",
        f"Бектест: той самий детектор на вікні {LIVE_WINDOW} закритих свічок, як у живому скані; вхід за закриттям сигнальної "
        f"свічки, шлях на 1h-свічках {venue}, беззбиток після {BE_PATH:.0%} шляху до цілі; R бектесту — смуга, бо порядок цін "
        "усередині години невідомий. R демо ділиться на ризик від його заповнення, тому шлях і вихід звіряються з бектестом, "
        "запущеним від ціни заповнення демо; «R демо в од. бектесту» — те саме P&L на ризик бектесту. Вхід: + означає, що демо "
        "зайшло краще за закриття свічки; «план» — різниця свічок, решта — виконання. Демо входить мейкером до 2 год після "
        "закриття і бачить свічки api-demo.bybit.com. Правило беззбитку — те, що діяло на момент відкриття угоди: до 22.10 "
        "(10:12 Київ) — +30 % ROE за плечем угоди, далі — половина шляху; «лише зміна правила беззбитку» означає, що R демо "
        "збігається з бектестом за старим правилом, а з сьогоднішнім — ні.",
    ]
    lines += [f"Примітка: {n}" for n in notes]
    return "\n".join(lines) + "\n"


GROUP_UA = {
    "missed": "вхід не заповнився",
    "skipped": "не відібрано",
    "refused": "відмова guard",
    "taken": "виконані (для порівняння)",
}


def _ci(ci: list[float] | None) -> str:
    return "—" if not ci else f"[{_r(ci[0])}; {_r(ci[1])}]"


def render_not_executed(ne: dict[str, Any], err: str | None = None) -> str:
    """The Ukrainian block of ``demo_shadow.not_executed``: one line per group, then its reasons."""
    lines = ["", "## Відібрані, але не виконані сигнали проти тіньових планів", ""]
    if err:
        return "\n".join([*lines, f"Звірки немає: {err}."]) + "\n"
    for g in demo_shadow.GROUPS:
        s = ne["groups"][g]
        lines.append(
            f"- {GROUP_UA[g]}: {s['n']} (за 7 днів {s['week']}); план закрито {s['resolved']}, ще відкритий {s['open']}, "
            f"без плану {s['no_plan']}; R тіньових планів: середній {_r(s['mean_r'])}, 95 % ДІ {_ci(s['ci95_r'])}, "
            f"сума {_r(s['sum_r'])} на {s['n_r']}; {s['conclusion']}."
        )
        for x in s["reasons"][:10]:
            lines.append(f"  - {x['reason']}: {x['n']}, з R {x['n_r']}, середній {_r(x['mean_r'])}, сума {_r(x['sum_r'])}.")
        ex = s.get("excluded")
        if isinstance(ex, dict) and ex.get("n"):
            lines.append(
                f"  - без тіньового плану за правилом (стоп далі від входу, ніж дозволяє guard, або помилка рівнів): {ex['n']} "
                f"(за 7 днів {ex['week']}), у R не входять."
            )
    lines += [
        "",
        "R тіньового плану — в одиницях ризику демо (як «R без беззбитку» бектесту): ринковий вхід за ціною рішення (до 2 год "
        "після закриття сигнальної свічки), 1h-свічки Bybit, стоп виграє свічку, що торкнулась обох рівнів, без беззбитку. "
        "Це простий сетап, а не те, що записало б демо. Відмова guard з кількома правилами рахується за першим.",
    ]
    return "\n".join(lines) + "\n"


def write_json(path: str, doc: dict[str, Any]) -> None:
    """``doc`` as JSON at ``path`` (parent created), replaced atomically: a reader never sees half a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


async def run(
    db: str, since_ms: int, days: float, venue: str, now_ms: int, turnover: bool = False
) -> tuple[list[Replay], dict[str, Any], dict[str, Any], int, list[str]]:
    """Replays every closed trade since ``since_ms`` (the running total) and summarises the last ``days`` apart. With
    ``turnover`` the rows also get the entry day's Bybit turnover and group (``add_turnover``; the weekly Telegram
    message does not need it)."""
    week_ms = now_ms - int(days * DAY_MS)
    trades, err = load_closed(db, since_ms, now_ms)
    notes = [err] if err else []
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()
    try:
        rows = await replay_all(trades, venue_fetch(rt, b, venue), now_ms)
        if turnover:
            rows = await add_turnover(trades, rows, bybit_day_fetch(rt, b), now_ms)
    finally:
        await rt.aclose()
    notes += [f"{m.field}: {m.reason}" for m in b.missing[:10]] + b.warnings[:5]
    week = summary([x for x in rows if x.closed_ts is not None and x.closed_ts >= week_ms])
    return rows, summary(rows), week, week_ms, notes


def _since(s: str | None) -> int:
    if not s:
        return 0  # every closed demo trade: the running total from the first one
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--since", help="running total from this UTC date, YYYY-MM-DD (default: the first demo trade)")
    ap.add_argument("--days", type=float, default=7.0, help="the separate block of trades closed in the last N days")
    ap.add_argument("--venue", default="bybit", help="ccxt id of the candles (default bybit; binanceusdm also works)")
    ap.add_argument("--out", help="also write the text to this file")
    ap.add_argument(
        "--json",
        help="write the rows, both summaries and the not-executed block as JSON to this file (replaced atomically; the "
        "dashboard reads /var/lib/tradedesk-mcp/demo-vs-backtest/latest.json)",
    )
    a = ap.parse_args(argv)
    now = int(time.time() * 1000)
    rows, total, week, week_ms, notes = asyncio.run(run(a.db, _since(a.since), a.days, a.venue, now, turnover=True))
    by_turnover = summary_by_turnover(rows)
    trades, audit, plans, ne_err = load_not_executed(a.db)
    ne = demo_shadow.not_executed(trades, audit, plans, now_ms=now)
    text = render(rows, total, week, week_ms, now, a.venue, notes, by_turnover) + render_not_executed(ne, ne_err)
    print(text, end="")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    if a.json:
        doc = {
            "schema": JSON_SCHEMA,
            "generated_ms": now,
            "venue": a.venue,
            "notes": notes,
            "summary": total,
            "week": week,
            "by_turnover": by_turnover,
            "rows": [x.as_dict() for x in rows],
            "not_executed": {**ne, "error": ne_err},
        }
        write_json(a.json, doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
