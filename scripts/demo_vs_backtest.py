"""Demo vs backtest: replay every closed demo trade on historical candles (BACKLOG 0.5, item a).

    uv run python scripts/demo_vs_backtest.py [--since YYYY-MM-DD] [--days 7] [--venue bybit] [--db PATH]
                                              [--out FILE] [--json FILE]

For each closed ``demo_trade`` row (read over a ``mode=ro`` SQLite connection: no migration, no write) the candles of
the signal timeframe (the live scan's window) and the 1h path are fetched through ``rt.exchanges`` (``core`` cache,
rate limits, health; paged with an upper bound per page), and the pure ``analytics/demo_vs_backtest.py`` decides:
match / no signal / mismatch (or pending / no data / unsupported). The summary is a running total from the first demo
trade (or ``--since``): a trade the backtest had not resolved last week is counted once it is, and the 30-trade
threshold applies to the total. A separate block lists the trades closed in the last ``--days``. The text (Ukrainian,
for the owner) is printed and, with ``--out``, written to a file; ``--json`` writes the rows and both summaries.
Read-only public market data: nothing here places, changes or cancels an order.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sqlite3
import sys
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.demo_vs_backtest import LIVE_WINDOW, DemoTrade, Replay, replay, summary
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

Fetch = Callable[[str, str, int, int], Awaitable[list[list[float]]]]


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
REASON_UA = {"side": "сторона", "entry": "вхід", "stop": "стоп", "target": "ціль", "r": "R", "no demo R": "немає R демо"}


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
) -> str:
    """``rows``: the running total (all closed demo trades replayed); ``week``: the summary of those closed since
    ``week_ms``, whose rows get the table."""
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
        "закриття і бачить свічки api-demo.bybit.com. Угоди до 22.10 переносили стоп за +30 % ROE, а не за половиною шляху.",
    ]
    lines += [f"Примітка: {n}" for n in notes]
    return "\n".join(lines) + "\n"


async def run(
    db: str, since_ms: int, days: float, venue: str, now_ms: int
) -> tuple[list[Replay], dict[str, Any], dict[str, Any], int, list[str]]:
    """Replays every closed trade since ``since_ms`` (the running total) and summarises the last ``days`` apart."""
    week_ms = now_ms - int(days * DAY_MS)
    trades, err = load_closed(db, since_ms, now_ms)
    notes = [err] if err else []
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()
    try:
        rows = await replay_all(trades, venue_fetch(rt, b, venue), now_ms)
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
    ap.add_argument("--json", help="write the rows and both summaries as JSON to this file")
    a = ap.parse_args(argv)
    now = int(time.time() * 1000)
    rows, total, week, week_ms, notes = asyncio.run(run(a.db, _since(a.since), a.days, a.venue, now))
    text = render(rows, total, week, week_ms, now, a.venue, notes)
    print(text, end="")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    if a.json:
        Path(a.json).write_text(
            json.dumps({"summary": total, "week": week, "rows": [x.as_dict() for x in rows]}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
