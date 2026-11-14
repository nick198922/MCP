#!/usr/bin/env python3
"""S16 sample pace (IDEA-20261030-demo-setups-depth, item «S16, темп вибірки (облік)»; registration «Попередня
реєстрація — S16, темп вибірки»): the share of the passport's S16 candidates that pass the demo's selection, the
passport's trades per week after the one-trade-per-coin chain with a t interval over weeks, the demo's own rate with an
exact Poisson interval, and the date / probability of reaching 30 closed demo trades. Accounting only: no R is
computed or printed.

    uv run python scripts/verify_s16_sample_pace.py --stage smoke   # files, rows, timing, RSS (no outcome numbers)
    uv run python scripts/verify_s16_sample_pace.py --stage run     # tables (also <out-dir>/run.txt)

No network: the S16 passport cache (``setup_passport/S16/cands``) and the turnover lists of ``course_s14_ema`` /
``funding_crowding``, both filled earlier through the runtime providers by ``verify_setup_passport.py``.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402
from common_helpers import rss_mb  # noqa: E402

from tradedesk.analytics import sample_pace as pace  # noqa: E402

W = pace.WEEK_MS
WINDOWS = (  # (label, first Monday, weeks) — registered; all end 2026-08-31 (the 1d passport end)
    ("основне 2026-03-02 … 08-31", s1.ms("2026-03-02T00:00"), 26),
    ("OOS 2025-03-03 … 2026-08-31", s1.ms("2025-03-03T00:00"), 78),
    ("2021-01-04 … 2026-08-31", s1.ms("2021-01-04T00:00"), (s1.ms("2026-08-31T00:00") - s1.ms("2021-01-04T00:00")) // W),
)
CHECK_DATES = ("2026-11-30", "2026-12-30", "2027-02-01")
LINES: list[str] = []


def emit(s: str = "") -> None:
    print(s, flush=True)
    LINES.append(s)


def masks(T: dict[str, np.ndarray], s1_cache: Path, data_dir: Path) -> dict[str, np.ndarray]:
    """Registered selections: F (the demo's WIDE list: top-250 by the previous day's turnover and >= $1M) and F100
    (top-100 and >= $5M, the item's wording), both with R:R >= 1.5, stop <= 10 %, score >= 2."""
    elig = s1.eligible_syms(data_dir)
    qv = s1.daily_qv(s1_cache, data_dir, elig)
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1.0)
    period = T["t_in"] >= vsp.PASSPORT_MS
    f = period & sel & (turn >= 1e6) & vsp.in_prev_day_top(T, vsp.top_n_days(qv, 250))
    f100 = period & sel & (turn >= 5e6) & vsp.in_prev_day_top(T, vsp.top_n_days(qv, 100))
    return {"F": f, "F100": f100, "F_ch": vsp.chain(T, f), "F100_ch": vsp.chain(T, f100)}


def fnum(x: Any, fmt: str = "{:.2f}") -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def window_table(T: dict[str, np.ndarray], M: dict[str, np.ndarray], name: str) -> dict[str, Any]:
    emit(f"\n## Відбір {name}: частка кандидатів і угоди на тиждень (після ланцюга «одна угода на монету»)")
    emit("| вікно | ТФ | кандидати | пройшли відбір | частка | угоди (ланцюг) | угод / тиждень | 95 % CI (t за тижнями) | тижнів з 0 |")
    emit("|---|---|---|---|---|---|---|---|---|")
    res: dict[str, Any] = {}
    for label, start, n_w in WINDOWS:
        end = start + n_w * W
        inw = (T["t_in"] >= start) & (T["t_in"] < end)
        for tf in ("4h", "1d", "обидва"):
            m_tf = inw if tf == "обидва" else inw & (T["tf"] == tf)
            n_c, n_f = int(m_tf.sum()), int((m_tf & M[name]).sum())
            ch = m_tf & M[f"{name}_ch"]
            counts = pace.weekly_counts(T["t_in"][ch].tolist(), start, n_w)
            r = pace.weekly_rate(counts)
            share = n_f / n_c if n_c else math.nan
            zero = sum(1 for c in counts if c == 0)
            emit(
                f"| {label} | {tf} | {n_c} | {n_f} | {fnum(share, '{:.3f}')} | {int(ch.sum())} | {fnum(r['mean'])} | "
                f"[{fnum(r['lo'])}; {fnum(r['hi'])}] | {zero} / {n_w} |"
            )
            res[f"{label}|{tf}"] = r
    return res


def projection(lam_day: dict[str, float], have: int, target: int, today: datetime) -> None:
    need = target - have
    emit(f"\n## Прогноз: {have} закритих → {target} (потрібно ще {need}), від {today:%Y-%m-%d}")
    emit("| темп λ | угод / тиждень | днів до 30 | дата 30 | " + " | ".join(f"P(≥ {need}) до {d}" for d in CHECK_DATES) + " |")
    emit("|---|---|---|---|" + "---|" * len(CHECK_DATES))
    for k, lam in lam_day.items():
        d = pace.days_to_reach(target, have, lam)
        date = "ніколи" if math.isinf(d) else f"{today + timedelta(days=d):%Y-%m-%d}"
        ps = []
        for cd in CHECK_DATES:
            days = (datetime.fromisoformat(cd).replace(tzinfo=UTC) - today).total_seconds() / 86400
            ps.append(f"{pace.prob_reach(need, lam, days):.3f}")
        emit(f"| {k} | {lam * 7:.2f} | {fnum(d, '{:.0f}')} | {date} | " + " | ".join(ps) + " |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--passport-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport/S16")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s16-sample-pace")
    ap.add_argument("--demo-closed", type=int, default=6, help="closed S16 demo trades so far")
    ap.add_argument("--demo-days", type=float, default=22.0, help="days the demo has traded S16")
    ap.add_argument("--target", type=int, default=30)
    ap.add_argument("--today", default="2026-11-14")
    a = ap.parse_args()
    t0 = time.time()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((a.passport_dir / "cands").glob("*.json"))
    emit(f"verify_s16_sample_pace --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC; файлів кандидатів {len(files)}")
    if not files:
        emit("кешу паспорта S16 немає — стоп")
        return 1
    T = vsp.load_rows(a.passport_dir)
    emit(f"рядків {T['t_in'].size}; ТФ {sorted(set(T['tf'].tolist()))}; {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB")
    M = masks(T, a.s1_cache, a.data_dir)
    emit(f"списки топ-N і ланцюги: {time.time() - t0:.1f} с; rss {rss_mb():.0f} MB")
    if a.stage == "smoke":
        emit("smoke: дані читаються, маски будуються; чисел результату не друкую")
        return 0
    res = window_table(T, M, "F")
    window_table(T, M, "F100")
    k, days = a.demo_closed, a.demo_days
    lo, hi = pace.poisson_ci(k)
    emit(f"\n## Демо S16: {k} закритих за {days:g} днів")
    emit(f"темп {k / days * 7:.2f} / тиждень, точний 95 % CI [{lo / days * 7:.2f}; {hi / days * 7:.2f}]")
    main_r = res[f"{WINDOWS[0][0]}|обидва"]
    main_4h = res[f"{WINDOWS[0][0]}|4h"]
    emit(
        f"паспорт F (основне вікно): обидва ТФ {fnum(main_r['mean'])} / тиждень, 4h {fnum(main_4h['mean'])}; "
        f"коефіцієнт реалізації демо / паспорт: {k / days * 7 / main_r['mean']:.2f} (4h: {k / days * 7 / main_4h['mean']:.2f}); "
        "періоди не перекриваються — описово"
    )
    today = datetime.fromisoformat(a.today).replace(tzinfo=UTC)
    projection(
        {
            "(а) демо, точка": k / days,
            "(б) демо, верхня 95 %": hi / days,
            "(в) паспорт F, основне вікно": float(main_r["mean"]) / 7,
        },
        k,
        a.target,
        today,
    )
    emit(f"\n{time.time() - t0:.1f} с; peak rss {rss_mb():.0f} MB")
    (a.out_dir / "run.txt").write_text("\n".join(LINES) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
