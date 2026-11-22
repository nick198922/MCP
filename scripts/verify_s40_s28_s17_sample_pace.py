#!/usr/bin/env python3
"""S40, S28, S17 sample pace (IDEA-20261030-demo-setups-depth, item «S40, S28, S17: темп вибірки (облік; аудит 17.11)»;
registration «Попередня реєстрація — S40, S28, S17, темп вибірки»): the demo's own rate of the counted events (S40
closed trades; S28 / S17 filled trades) with an exact Poisson interval, the date / probability of the 30th, the
passport's rate of the same events under the demo's selection (t interval over weeks), the realisation ratio, and the
demo's decisions on its evaluated signals by reason (``exec_audit``). Accounting only: no R or P&L is read or printed.

    uv run python scripts/verify_s40_s28_s17_sample_pace.py --stage smoke   # files, rows, categories, timing (no counts)
    uv run python scripts/verify_s40_s28_s17_sample_pace.py --stage run     # tables (also <out-dir>/run.txt)

No network: the passport caches (``setup_passport/<S>/cands``), the turnover lists of ``course_s14_ema`` /
``funding_crowding`` (filled earlier through the runtime providers by ``verify_setup_passport.py``) and the demo DB
read-only (``mode=ro``; columns without outcomes only).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402
from common_helpers import connect_ro, rss_mb  # noqa: E402

from tradedesk.analytics import sample_pace as pace  # noqa: E402

W = pace.WEEK_MS
DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
# Registered selection of each setup as on the demo (demo_trader: WIDE_SETUPS, MIN_SCORE_BY_SETUP, SETUP_TIMEFRAMES).
CONF: dict[str, dict[str, Any]] = {
    "S40": {"tfs": ("4h",), "top": 250, "floor": 1e6, "score": 2, "limit": False, "event": "закрита"},
    "S28": {"tfs": ("4h",), "top": 100, "floor": 5e6, "score": 1, "limit": True, "event": "заповнена"},
    "S17": {"tfs": ("1d",), "top": 100, "floor": 5e6, "score": 2, "limit": True, "event": "заповнена"},
}
S28_SCORE1_MS = s1.ms("2026-11-10T00:00")  # registered main start of the S28 demo rate (score >= 1, owner 10.11)
WINDOWS = (  # (label, first Monday, weeks) — as for S16; all end 2026-08-31 (the 1d passport end)
    ("основне 2026-03-02 … 08-31", s1.ms("2026-03-02T00:00"), 26),
    ("OOS 2025-03-03 … 2026-08-31", s1.ms("2025-03-03T00:00"), 78),
    ("2021-01-04 … 2026-08-31", s1.ms("2021-01-04T00:00"), (s1.ms("2026-08-31T00:00") - s1.ms("2021-01-04T00:00")) // W),
)
CHECK_DATES = ("2026-11-30", "2026-12-30", "2027-03-31")
DEADLINE = datetime(2027, 3, 31, tzinfo=UTC)
TARGET = 30
CAT_UA = {
    "entered": "вхід (sent)",
    "stop_wide": "стоп > 10 %",
    "rr_low": "R:R < 1.5",
    "score_low": "бал < порогу",
    "at_level": "ціна вже на рівні лімітки",
    "levels": "рівні некоректні",
    "positions_max": "ліміт позицій (25)",
    "setup_slots": "власні слоти сетапу",
    "bar_cap": "ліміт на свічку",
    "day_caps": "денні ліміти входів",
    "risk_caps": "ліміти ризику / маржі",
    "already_in": "уже в монеті",
    "decided": "уже вирішено",
    "universe": "поза списком дня",
    "size_zero": "нульовий розмір",
    "rank": "нижчий ранг",
    "no_decision": "без рішення в журналі",
    "other": "інше",
}
LINES: list[str] = []


def emit(s: str = "") -> None:
    print(s, flush=True)
    LINES.append(s)


def fnum(x: Any, fmt: str = "{:.2f}") -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def ms_of(x: Any) -> int | None:
    """Epoch ms of a DB timestamp (seconds are scaled up)."""
    if x is None:
        return None
    v = int(x)
    return v * 1000 if v < 100_000_000_000 else v


# ---------------------------------------------------------------- passport


def passport(setup: str, a: argparse.Namespace, qv: dict[str, Any]) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    c = CONF[setup]
    T = vsp.load_rows(a.passport_root / setup)
    keep = np.isin(T["tf"], list(c["tfs"]))
    T = {k: v[keep] for k, v in T.items()}
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= c["score"])
    turn = np.nan_to_num(T["turn"], nan=-1.0)
    period = T["t_in"] >= vsp.PASSPORT_MS
    f = period & sel & (turn >= c["floor"]) & vsp.in_prev_day_top(T, vsp.top_n_days(qv, c["top"]))
    ch = vsp.chain(T, f)
    M = {"F": f, "ch": ch}
    if c["limit"]:
        T["t_fill"] = np.array([-1 if x is None else int(x) for x in T["t_fill"]], dtype=np.int64)
        M["fill"] = ch & T["filled"]
    return T, M


def passport_table(setup: str, T: dict[str, np.ndarray], M: dict[str, np.ndarray]) -> dict[str, Any]:
    c = CONF[setup]
    lim = c["limit"]
    emit(f"\n## {setup}: паспорт під відбором демо (топ-{c['top']}, ≥ ${c['floor'] / 1e6:g}M, бал ≥ {c['score']}, R:R ≥ 1.5, стоп ≤ 10 %)")
    head = "| вікно | ТФ | кандидати | пройшли відбір | частка | " + (
        "розміщення (ланцюг) | заповнені | заповнених / тиждень | 95 % CI (t за тижнями) | тижнів з 0 |"
        if lim
        else "угоди (ланцюг) | угод / тиждень | 95 % CI (t за тижнями) | тижнів з 0 |"
    )
    emit(head)
    emit("|" + "---|" * (head.count("|") - 1))
    res: dict[str, Any] = {}
    tfs = list(c["tfs"]) + (["обидва"] if len(c["tfs"]) > 1 else [])
    for label, start, n_w in WINDOWS:
        end = start + n_w * W
        for tf in tfs:
            m_tf = np.ones(T["t_in"].size, dtype=bool) if tf == "обидва" else T["tf"] == tf
            inw = m_tf & (T["t_in"] >= start) & (T["t_in"] < end)
            n_c, n_f = int(inw.sum()), int((inw & M["F"]).sum())
            share = n_f / n_c if n_c else math.nan
            n_ch = int((inw & M["ch"]).sum())
            if lim:
                ev = T["t_fill"][m_tf & M["fill"]]  # fills inside the window by the fill time
                ev = ev[(ev >= start) & (ev < end)]
            else:
                ev = T["t_in"][inw & M["ch"]]
            counts = pace.weekly_counts(ev.tolist(), start, n_w)
            r = pace.weekly_rate(counts)
            zero = sum(1 for x in counts if x == 0)
            mid = f"{n_ch} | {int(r['total'])} | " if lim else f"{n_ch} | "
            emit(
                f"| {label} | {tf} | {n_c} | {n_f} | {fnum(share, '{:.3f}')} | {mid}{fnum(r['mean'])} | "
                f"[{fnum(r['lo'])}; {fnum(r['hi'])}] | {zero} / {n_w} |"
            )
            res[f"{label}|{tf}"] = r
    return res


# ---------------------------------------------------------------- demo


def demo_rows(db: str, setup: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(demo_trade rows: link_id, timeframe, status, signal_ts, opened_ts, closed_ts; exec_audit decision rows)."""
    con = connect_ro(db)
    try:
        trades = [
            dict(zip(("link_id", "timeframe", "status", "signal_ts", "opened_ts", "closed_ts", "venue"), r, strict=True))
            for r in con.execute(
                "SELECT link_id, timeframe, status, signal_ts, opened_ts, closed_ts, venue FROM demo_trade WHERE setup = ?",
                (setup,),
            )
        ]
        audit = []
        for ts, kind, lid, payload in con.execute(
            "SELECT ts, kind, link_id, payload FROM exec_audit WHERE setup = ? AND kind IN ('signal', 'sent', 'refused', 'skipped')",
            (setup,),
        ):
            p: dict[str, Any] = {}
            if kind in ("refused", "skipped"):
                try:
                    p = json.loads(payload or "{}")
                except json.JSONDecodeError:
                    p = {}
            audit.append({"ts": ms_of(ts), "kind": kind, "link_id": lid, "payload": p})
        return trades, audit
    finally:
        con.close()


def demo_start(audit: list[dict[str, Any]]) -> int | None:
    ts = [r["ts"] for r in audit if r["kind"] == "signal"]
    return min(ts) if ts else None


def demo_events(trades: list[dict[str, Any]], sent: dict[str, int], start: int, now: int) -> dict[str, int]:
    """Counts as of ``now`` from ``start``: placements (a trade belongs to the period by the time of its ``sent`` audit
    row, else its signal bar time), filled (status open / closed), closed."""

    def t_of(t: dict[str, Any]) -> int:
        return sent.get(t["link_id"]) or ms_of(t["signal_ts"]) or 0

    rows = [t for t in trades if start <= t_of(t) < now]
    filled = [t for t in rows if t["status"] in ("open", "closed")]
    closed = [t for t in rows if t["status"] == "closed" and (ms_of(t["closed_ts"]) or 0) < now]
    return {
        "placements": len(rows),
        "filled": len(filled),
        "closed": len(closed),
        "open": sum(t["status"] == "open" for t in rows),
        "pending": sum(t["status"] == "pending" for t in rows),
        "missed": sum(t["status"] == "missed" for t in rows),
    }


def projection(setup: str, lam_day: dict[str, float], have: int, today: datetime) -> dict[str, datetime | None]:
    need = TARGET - have
    emit(f"\n## {setup}: прогноз {have} → {TARGET} (потрібно ще {need}), від {today:%Y-%m-%d}")
    emit("| темп λ | подій / тиждень | днів до 30 | дата 30 | " + " | ".join(f"P(≥ {need}) до {d}" for d in CHECK_DATES) + " |")
    emit("|---|---|---|---|" + "---|" * len(CHECK_DATES))
    out: dict[str, datetime | None] = {}
    for k, lam in lam_day.items():
        d = pace.days_to_reach(TARGET, have, lam)
        date = None if math.isinf(d) else today + timedelta(days=d)
        out[k] = date
        ps = []
        for cd in CHECK_DATES:
            days = (datetime.fromisoformat(cd).replace(tzinfo=UTC) - today).total_seconds() / 86400
            ps.append(f"{pace.prob_reach(need, lam, days):.3f}")
        emit(f"| {k} | {lam * 7:.2f} | {fnum(d, '{:.0f}')} | {'ніколи' if date is None else f'{date:%Y-%m-%d}'} | " + " | ".join(ps) + " |")
    return out


def decisions_table(setup: str, dec: dict[str, str]) -> None:
    n = len(dec)
    cnt = Counter(dec.values())
    emit(f"\n## {setup}: рішення демо щодо оцінених сигналів (унікальні link_id; перше рішення, вхід має пріоритет)")
    emit("| рішення | n | частка |")
    emit("|---|---|---|")
    for cat, k in cnt.most_common():
        emit(f"| {CAT_UA.get(cat, cat)} | {k} | {k / n:.2f} |")
    emit(f"| усього | {n} | 1.00 |")


def run_setup(setup: str, a: argparse.Namespace, qv: dict[str, Any], now: int, today: datetime) -> None:
    c = CONF[setup]
    t0 = time.time()
    T, M = passport(setup, a, qv)
    res = passport_table(setup, T, M)
    del T, M
    trades, audit = demo_rows(a.db, setup)
    first = demo_start(audit)
    if first is None:
        emit(f"\n{setup}: на демо оцінок сигналів немає")
        return
    starts = [("весь час на демо", first)]
    if setup == "S28":
        starts = [("з 10.11 (бал ≥ 1) — основне", S28_SCORE1_MS), ("весь час на демо (описово)", first)]
    key = "closed" if not c["limit"] else "filled"
    sent = {r["link_id"]: r["ts"] for r in sorted(audit, key=lambda r: -r["ts"]) if r["kind"] == "sent"}  # first sent
    main_lam = None
    main_k = 0
    emit(f"\n## {setup}: демо (подія — {c['event']} угода), станом на {datetime.fromtimestamp(now / 1000, UTC):%Y-%m-%d %H:%M} UTC")
    emit("| період | від | днів | розміщення | заповнені | закриті | відкриті | pending | missed | подій / тиждень | точний 95 % CI |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, st in starts:
        ev = demo_events(trades, sent, st, now)
        days = (now - st) / pace.DAY_MS
        k = ev[key]
        lo, hi = pace.poisson_ci(k)
        emit(
            f"| {label} | {datetime.fromtimestamp(st / 1000, UTC):%Y-%m-%d} | {days:.1f} | {ev['placements']} | {ev['filled']} | "
            f"{ev['closed']} | {ev['open']} | {ev['pending']} | {ev['missed']} | {k / days * 7:.2f} | [{lo / days * 7:.2f}; {hi / days * 7:.2f}] |"
        )
        if main_lam is None:
            main_lam, main_hi, main_k, main_days = k / days, hi / days, k, days
    # the count toward 30 is over the whole time on the demo (the S28 rate is from 10.11, its count from the start)
    have = demo_events(trades, sent, 0, now)[key]
    tf_key = "обидва" if len(c["tfs"]) > 1 else c["tfs"][0]
    pr = res[f"{WINDOWS[0][0]}|{tf_key}"]
    emit(
        f"паспорт (основне вікно, {tf_key}): {fnum(pr['mean'])} / тиждень; коефіцієнт реалізації демо / паспорт: "
        f"{(main_lam * 7 / pr['mean']) if pr['mean'] else math.nan:.2f} (періоди не перекриваються — описово)"
    )
    dates = projection(
        setup,
        {"(а) демо, точка": main_lam, "(б) демо, верхня 95 %": main_hi, "(в) паспорт, основне вікно": float(pr["mean"]) / 7},
        have,
        today,
    )
    da = dates["(а) демо, точка"]
    late = da is None or da > DEADLINE
    emit(
        f"правило: дата (а) {'ніколи' if da is None else f'{da:%Y-%m-%d}'} {'>' if late else '≤'} 2027-03-31 → {'рядок «вердикту не буде» + питання' if late else 'рядка немає'}"
    )
    dec = pace.signal_decisions(audit, now)
    decisions_table(setup, dec)
    if setup == "S28":  # registered second table: the signals evaluated since the score-1 floor
        decisions_table("S28 з 10.11", pace.signal_decisions([r for r in audit if r["ts"] >= S28_SCORE1_MS], now))
    emit(f"({setup}: {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB; демо: {main_k} подій за {main_days:.1f} днів основного періоду)")


def smoke(a: argparse.Namespace, qv: dict[str, Any], now: int) -> None:
    for setup in CONF:
        t0 = time.time()
        T, M = passport(setup, a, qv)
        emit(
            f"{setup}: рядків паспорта {T['t_in'].size}; ТФ {sorted(set(T['tf'].tolist()))}; маски {sorted(M)}; {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB"
        )
        del T, M
        trades, audit = demo_rows(a.db, setup)
        first = demo_start(audit)
        dec = pace.signal_decisions(audit, now)
        lids = Counter(r["link_id"] for r in audit if r["kind"] in ("refused", "skipped"))
        emit(
            f"{setup}: демо читається; перша оцінка {datetime.fromtimestamp(first / 1000, UTC):%Y-%m-%d %H:%M} UTC; "
            f"venue {sorted({str(t['venue']) for t in trades})}; статуси {sorted({str(t['status']) for t in trades})}; "
            f"категорії рішень {sorted(set(dec.values()))}; link_id з > 1 рядком рішення: {'так' if any(v > 1 for v in lids.values()) else 'ні'}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--passport-root", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s40-s28-s17-sample-pace")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--now-ms", type=int, help="as-of clock, unix ms (default: now); rerun a past report with its value")
    a = ap.parse_args()
    t0 = time.time()
    now = a.now_ms or int(time.time() * 1000)
    today = datetime.fromtimestamp(now / 1000, UTC)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    emit(f"verify_s40_s28_s17_sample_pace --stage {a.stage}; as-of {today:%Y-%m-%d %H:%M} UTC (now_ms {now})")
    elig = s1.eligible_syms(a.data_dir)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    emit(f"обороти: {len(qv)} монет; {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB")
    if a.stage == "smoke":
        smoke(a, qv, now)
        emit(f"smoke: дані читаються; чисел результату не друкую; {time.time() - t0:.1f} с; peak rss {rss_mb():.0f} MB")
        (a.out_dir / "smoke.txt").write_text("\n".join(LINES) + "\n")
        return 0
    for setup in CONF:
        run_setup(setup, a, qv, now, today)
    emit(f"\n{time.time() - t0:.1f} с; peak rss {rss_mb():.0f} MB")
    (a.out_dir / "run.txt").write_text("\n".join(LINES) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
