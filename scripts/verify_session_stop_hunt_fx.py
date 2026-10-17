"""Session stop hunt on forex (IDEA-20261016-session-stop-hunt-fx, setup S25): EURUSD and GBPUSD 5m bid bars from
Dukascopy, 2014-01 .. 2026-09, the crypto card's algorithm with a bid/ask model and ECN costs.

The algorithm is the crypto one (``tradedesk.analytics.session_stop_hunt``); only the execution differs
(``fx_execution``: constant spread, 1 pip stop buffer, 0.7 pip commission, 0.2 pip slippage). Report helpers are
imported from ``verify_session_stop_hunt.py`` so both reports read the same. Stages:
  uv run python scripts/fx_history.py EURUSD --start 2013-12-01 --end 2026-09-30 --tf 5m \
      --cache ~/.cache/tradedesk-agents/fx --out ~/.cache/tradedesk-agents/fx/EURUSD_5m.csv     (and GBPUSD)
  uv run python scripts/verify_session_stop_hunt_fx.py --stage count  --out stage0.md
  uv run python scripts/verify_session_stop_hunt_fx.py --stage report --out report.md
``count`` (stage 0) counts setups and trades per pair without computing any trade outcome. Data are read from the
CSVs that fx_history.py writes (the Dukascopy fetch lives there, not here). No keys, no orders.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_session_stop_hunt as base  # noqa: E402
from verify_session_stop_hunt import HDR, emit, fmt_ci, line, q, rss_mb, sel  # noqa: E402

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, without_top_days  # noqa: E402
from tradedesk.analytics.session_stop_hunt import (  # noqa: E402
    BAR_MS,
    Bars,
    Costs,
    Execution,
    Session,
    cluster_bootstrap_diff,
    cost_r,
    design_effect,
    find_setup,
    fx_execution,
    fx_skip_reason,
    london_session,
    new_york_session,
    simulate,
    target_for,
)

CORE = ["EURUSD", "GBPUSD"]
RESERVE = ["USDJPY", "AUDUSD"]
PIP = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "USDJPY": 1e-2, "AUDUSD": 1e-4}
SPREAD_PIPS = {"EURUSD": 0.2, "GBPUSD": 0.5, "USDJPY": 0.3, "AUDUSD": 0.3}  # constant ECN spread, London hours
BINGX = Costs(entry_market_pct=0.0007, entry_limit_pct=0.0007, exit_pct=0.0007)  # report only: 0.07 % per side
FIRST_DAY = date(2014, 1, 1)
LAST_DAY = date(2026, 9, 30)
SPLIT_DAY = date(2020, 1, 1)
RHO = 0.6  # EURUSD-GBPUSD correlation assumed by the card's power section
PLACEBO_SHIFTS = (6, 12, 18)
COMBOS = base.COMBOS


def load(fx_dir: Path, pair: str) -> tuple[Bars, np.ndarray]:
    """5m bid bars and volume from fx_history.py's CSV (ts_ms, open, high, low, close, volume)."""
    p = fx_dir / f"{pair}_5m.csv"
    a = np.loadtxt(p, delimiter=",", skiprows=1, dtype=float, ndmin=2)
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    return Bars.from_rows(a[:, :5]), a[:, 5].copy()


def days() -> list[date]:
    """Anchor days: weekdays only (the forex week opens Sunday ~22:00 UTC and closes Friday ~22:00 UTC)."""
    n = (LAST_DAY - FIRST_DAY).days + 1
    return [d for d in (FIRST_DAY + timedelta(days=i) for i in range(n)) if d.weekday() < 5]


def anchors() -> dict[str, list[Session]]:
    ds = days()
    out = {"london": [london_session(d) for d in ds]}
    for sh in PLACEBO_SHIFTS:
        out[f"placebo+{sh}h"] = [london_session(d, sh) for d in ds]
    out["ny"] = [new_york_session(d) for d in ds]
    return out


def executions(pair: str) -> dict[str, Execution]:
    """ecn: the registered model; spread0 / spread2x: robustness of the bid/ask model (London only)."""
    pip, s = PIP[pair], SPREAD_PIPS[pair]
    return {"ecn": fx_execution(pip, s), "spread0": fx_execution(pip, 0.0), "spread2x": fx_execution(pip, 2 * s)}


def run_pair(
    bars: Bars, vol: np.ndarray, pair: str, anchor: str, sessions: list[Session], ex: Execution, ex_name: str, *, outcomes: bool
) -> tuple[list[dict[str, Any]], Counter[str]]:
    reasons: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    pip = PIP[pair]
    for s in sessions:
        skip = fx_skip_reason(bars, vol, s)
        if skip is not None:
            reasons[skip] += 1
            continue
        st, b, why = find_setup(bars, s, ex)
        reasons[why] += 1
        if st is None or b is None:
            continue
        for ek, tk in COMBOS:
            base_row = {
                "sym": pair,
                "anchor": anchor,
                "exec": ex_name,
                "day": s.day,
                "side": "long" if st.side > 0 else "short",
                "entry": ek,
                "target": tk,
            }
            if not outcomes:
                lvl = st.entry if ek == "E1" else 0.5 * (st.manip_start + st.extreme)
                ok = target_for(tk, st, lvl) is not None
                reasons[f"{ek}{tk}:{'eligible' if ok else 't1_lt_1r'}"] += 1
                if ok and ek == "E1" and tk == "T1":
                    rows.append({**base_row, "stop_pct": (st.stop - st.entry) / abs(st.entry), "stop_pips": (st.stop - st.entry) / pip})
                continue
            tr, why2 = simulate(st, b, ek, tk, ex)
            reasons[f"{ek}{tk}:{why2}"] += 1
            if tr is None:
                continue
            row = {**base_row, **base.trade_row(tr)}
            row["stop_pips"] = (tr.stop - tr.entry) / pip
            row["cost_bingx_r"] = cost_r(tr, BINGX)
            row["r_net_bingx"] = tr.r_gross - row["cost_bingx_r"]
            rows.append(row)
    return rows, reasons


# ---------------------------------------------------------------- sanity

KNOWN = [  # (pair, UTC time, rough bid level from public charts) for a price-level check
    ("EURUSD", "2014-01-02T12:00", 1.37),
    ("EURUSD", "2015-03-16T12:00", 1.05),
    ("EURUSD", "2022-09-28T12:00", 0.96),
    ("EURUSD", "2026-09-30T12:00", None),
    ("GBPUSD", "2014-01-02T12:00", 1.65),
    ("GBPUSD", "2016-06-27T12:00", 1.33),
    ("GBPUSD", "2022-09-26T12:00", 1.07),
    ("GBPUSD", "2026-09-30T12:00", None),
]


def sanity(fx_dir: Path, pairs: list[str]) -> None:
    emit("## Санітарні перевірки")
    for p in pairs:
        b, vol = load(fx_dir, p)
        dt = np.diff(b.t)
        gaps = int(np.sum(dt != BAR_MS))
        first = datetime.fromtimestamp(b.t[0] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        last = datetime.fromtimestamp(b.t[-1] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        bad = int(np.sum((b.h < b.low) | (b.h < np.maximum(b.o, b.c)) | (b.low > np.minimum(b.o, b.c))))
        flat = (b.o == b.h) & (b.h == b.low) & (b.low == b.c)
        wd = np.array([datetime.fromtimestamp(x / 1000, tz=UTC).weekday() for x in b.t[::288]])
        emit(f"- {p}: {b.t.size} барів {first} … {last}; розривів {gaps}; некоректних OHLC {bad}; пласких барів {flat.mean():.1%}")
        dflat = flat[: wd.size * 288].reshape(-1, 288).mean(1) if b.t.size >= 288 else np.zeros(0)
        for k, name in enumerate(["пн", "вт", "ср", "чт", "пт", "сб", "нд"]):
            m = wd[: dflat.size] == k
            if m.any():
                emit(f"  - {name}: частка пласких барів за добу {dflat[m].mean():.1%}")
        for pp, ts_s, ref in KNOWN:
            if pp != p:
                continue
            ts = int(datetime.fromisoformat(ts_s).replace(tzinfo=UTC).timestamp() * 1000)
            i = int(np.searchsorted(b.t, ts))
            if i < b.t.size and b.t[i] == ts:
                emit(f"  - {ts_s} UTC: bid close {b.c[i]:.5f}" + (f" (орієнтир ≈ {ref})" if ref else ""))
        rng = (b.h - b.low) / PIP[p]
        lon = np.array([(x // 3_600_000) % 24 for x in b.t])
        emit(
            f"  - середній діапазон 5m-бару, піпсів: 00–06 UTC {rng[(lon < 6)].mean():.1f}, 07–11 UTC {rng[(lon >= 7) & (lon < 12)].mean():.1f}"
        )
        del b, vol
    emit(f"- пікова RSS {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stages


def stage_count(fx_dir: Path, pairs: list[str]) -> None:
    emit("# Стадія 0: частота подій (без результатів угод)")
    sanity(fx_dir, pairs)
    sess = anchors()["london"]
    per_day: dict[str, int] = defaultdict(int)
    total = 0
    emit(
        "\n| пара | днів | gap | flat_range | flat_window | zero_volume | no_sweep | both_first | no_fractal | no_bos_60m | "
        "new_extreme_after_window | сетапів (BOS) | E1×T1 угод | T1<1R |"
    )
    emit("|---" * 14 + "|")
    stops_pips: list[float] = []
    stops_pct: list[float] = []
    for p in pairs:
        b, vol = load(fx_dir, p)
        rows, rs = run_pair(b, vol, p, "london", sess, executions(p)["ecn"], "ecn", outcomes=False)
        del b, vol
        for r in rows:
            per_day[r["day"]] += 1
            stops_pips.append(r["stop_pips"])
            stops_pct.append(r["stop_pct"])
        total += len(rows)
        keys = ("gap", "flat_range", "flat_window", "zero_volume", "no_sweep", "both_first", "no_fractal", "no_bos_60m")
        emit(
            f"| {p} | {len(sess)} | " + " | ".join(str(rs[k]) for k in keys) + f" | {rs['new_extreme_after_window']} | {rs['ok']} | "
            f"{len(rows)} | {rs['E1T1:t1_lt_1r']} |"
        )
        emit(f"  (rss {rss_mb():.0f} MB)")
    sizes = list(per_day.values())
    deff = design_effect(sizes, RHO)
    emit(
        f"\n- угод E1×T1 разом: {total}; днів з угодою: {len(sizes)}; середня кількість угод на такий день {total / max(1, len(sizes)):.2f}"
    )
    emit(f"- ефект дизайну 1+(m−1)·ρ, ρ={RHO} (з картки), m зважене: {deff:.2f}; ефективне n ≈ {total / deff:.0f}")
    if stops_pips:
        emit(
            f"- стоп E1, піпсів: p10 {q(stops_pips, 0.1):.1f}, p25 {q(stops_pips, 0.25):.1f}, медіана {q(stops_pips, 0.5):.1f}, "
            f"p75 {q(stops_pips, 0.75):.1f}, p90 {q(stops_pips, 0.9):.1f}"
        )
        emit(f"- стоп E1, % ціни: p10 {q(stops_pct, 0.1):.3%}, медіана {q(stops_pct, 0.5):.3%}, p90 {q(stops_pct, 0.9):.3%}")


def collect(fx_dir: Path, pairs: list[str]) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], Counter[str]]]:
    rows: list[dict[str, Any]] = []
    reasons: dict[tuple[str, str, str], Counter[str]] = {}
    an = anchors()
    for p in pairs:
        t0 = time.time()
        b, vol = load(fx_dir, p)
        exs = executions(p)
        for name, sess in an.items():
            for ex_name, ex in exs.items():
                if ex_name != "ecn" and name != "london":
                    continue
                r, rs = run_pair(b, vol, p, name, sess, ex, ex_name, outcomes=True)
                rows.extend(r)
                reasons[(p, name, ex_name)] = rs
        del b, vol
        emit(f"  [run] {p} in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    return rows, reasons


def bingx_line(label: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — |"
    v = [r["r_net_bingx"] for r in rows]
    ci = cluster_bootstrap_ci(v, [r["day"] for r in rows], n_boot=5000)
    c = [r["cost_bingx_r"] for r in rows]
    return f"| {label} | {len(v)} | {statistics.fmean(v):+.3f} | {fmt_ci(ci)} | {q(c, 0.5):.2f} |"


def stage_report(fx_dir: Path, cache: Path, pairs: list[str]) -> None:
    rows, reasons = collect(fx_dir, pairs)
    (cache / "trades.json").write_text(json.dumps(rows))
    ecn = sel(rows, exec="ecn")
    lon = sel(ecn, anchor="london")
    main = sel(lon, entry="E1", target="T1")
    split = SPLIT_DAY.isoformat()
    emit("# Session stop hunt (forex): результати")
    emit(
        f"Пари: {', '.join(pairs)}; робочі дні {FIRST_DAY} … {LAST_DAY}; R у частках ризику (стоп − вхід за цінами bid/ask), net = після витрат ECN."
    )

    emit("\n## Основна комбінація E1 × T1 (Лондон, ECN)")
    emit(HDR)
    emit(line("усі пари", main, 10000))
    if set(pairs) != set(CORE):
        emit(line("EURUSD+GBPUSD", sel(main, sym=CORE)))
    for p in pairs:
        emit(line(p, sel(main, sym=p)))
    emit(line("2014–2019", [r for r in main if r["day"] < split]))
    emit(line("2020–2026", [r for r in main if r["day"] >= split]))
    emit(line("лонг", sel(main, side="long")))
    emit(line("шорт", sel(main, side="short")))
    for k in (5, 10, 20):
        v, d, _ = without_top_days([r["r_net"] for r in main], [r["day"] for r in main], k)
        ci = cluster_bootstrap_ci(v, d, n_boot=5000)
        emit(f"| без {k} найкращих днів (net) | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(ci)} |")
    for mult in (2.0,):
        v = [r["r_gross"] - mult * r["cost_r"] for r in main]
        ci = cluster_bootstrap_ci(v, [r["day"] for r in main], n_boot=5000)
        emit(f"| комісія і проковзування ×{mult:g} (стрес) | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(ci)} |")
    for ex_name, label in (("spread0", "спред 0 (чистий bid, як gross на крипті)"), ("spread2x", "спред ×2")):
        emit(line(label, sel(rows, exec=ex_name, anchor="london", entry="E1", target="T1")))

    if main:
        sp = [r["stop_pips"] for r in main]
        st = [r["stop_pct"] for r in main]
        rn = [r["r_net"] for r in main]
        cr = [r["cost_r"] for r in main]
        cb = [r["cost_bingx_r"] for r in main]
        ek = Counter(r["exit_kind"] for r in main)
        emit(
            f"\n- стоп, піпсів: p10 {q(sp, 0.1):.1f}, p25 {q(sp, 0.25):.1f}, медіана {q(sp, 0.5):.1f}, p75 {q(sp, 0.75):.1f}, p90 {q(sp, 0.9):.1f}"
        )
        emit(f"- стоп, % ціни: p10 {q(st, 0.1):.3%}, медіана {q(st, 0.5):.3%}, p90 {q(st, 0.9):.3%}")
        emit(f"- витрати ECN у R (без спреду): медіана {q(cr, 0.5):.3f}, середнє {statistics.fmean(cr):.3f}, p90 {q(cr, 0.9):.3f}")
        emit(f"- витрати BingX у R: медіана {q(cb, 0.5):.3f}, середнє {statistics.fmean(cb):.3f}, p90 {q(cb, 0.9):.3f}")
        emit(
            f"- розподіл R net: p5 {q(rn, 0.05):+.2f}, p25 {q(rn, 0.25):+.2f}, медіана {q(rn, 0.5):+.2f}, p75 {q(rn, 0.75):+.2f}, "
            f"p95 {q(rn, 0.95):+.2f}, SD {statistics.pstdev(rn):.2f}"
        )
        emit(f"- виходи: {dict(ek)}")
        both = sum(1 for r in main if r["both"])
        emit(f"- свічки, що зачепили і стоп, і ціль (рахуються стопом): {both} з {len(main)} = {both / len(main):.1%}")
        per_day = Counter(r["day"] for r in main)
        deff = design_effect(list(per_day.values()), RHO)
        sd = statistics.pstdev(rn)
        emit(f"- ефективне n (ρ={RHO}): {len(main) / deff:.0f}; MDE ≈ 2.8·SD/√n_eff = {2.8 * sd / (len(main) / deff) ** 0.5:.3f}R")
        nom_g = [r["r_gross"] * r["stop_pct"] for r in main]
        nom_n = [r["r_net"] * r["stop_pct"] for r in main]
        emit(f"- на номінал (описово): gross {statistics.fmean(nom_g):+.4%}, net {statistics.fmean(nom_n):+.4%} на угоду")
        bins = [-10, -1.5, -1.0, -0.5, 0, 0.5, 1, 2, 3, 100]
        h, _ = np.histogram(rn, bins=bins)
        emit("- гістограма R net: " + ", ".join(f"[{bins[i]:g}, {bins[i + 1]:g}): {int(h[i])}" for i in range(len(h))))

    emit("\n## H1-BingX: ті самі угоди з витратами BingX (0.07 % на сторону; лише звіт)")
    emit("| вибірка | n | E[R] net BingX | 95 % CI | медіана витрат, R |\n|---|---|---|---|---|")
    emit(bingx_line("E1×T1 усі пари", main))
    for p in pairs:
        emit(bingx_line(f"E1×T1 {p}", sel(main, sym=p)))
    for ek_, tk_ in COMBOS[1:]:
        emit(bingx_line(f"{ek_}×{tk_}", sel(lon, entry=ek_, target=tk_)))

    emit("\n## Усі комбінації входу й цілі (Лондон, ECN, для стійкості)")
    emit(HDR)
    for ek_, tk_ in COMBOS:
        sub = sel(lon, entry=ek_, target=tk_)
        emit(line(f"{ek_}×{tk_}", sub))
        for p in pairs:
            emit(line(f"  {ek_}×{tk_} {p}", sel(sub, sym=p)))
        emit(line(f"  {ek_}×{tk_} 2014–2019", [r for r in sub if r["day"] < split]))
        emit(line(f"  {ek_}×{tk_} 2020–2026", [r for r in sub if r["day"] >= split]))
        emit(line(f"  {ek_}×{tk_} лонг", sel(sub, side="long")))
        emit(line(f"  {ek_}×{tk_} шорт", sel(sub, side="short")))
        both = sum(1 for r in sub if r["both"])
        emit(f"|  {ek_}×{tk_}: обидва торкання {both}/{len(sub)} | | | | | | |")

    emit("\n## Лондон проти плацебо (E1 × T1, net ECN)")
    emit(HDR)
    emit(line("Лондон", main))
    pl_all: list[dict[str, Any]] = []
    for sh in PLACEBO_SHIFTS:
        p_ = sel(ecn, anchor=f"placebo+{sh}h", entry="E1", target="T1")
        pl_all.extend(p_)
        emit(line(f"плацебо +{sh} год", p_))
    emit(line("плацебо разом", pl_all))
    emit("\n| порівняння | різниця E[R] net | 95 % CI | p (різниця>0) |\n|---|---|---|---|")

    def diff(a: list[dict[str, Any]], b: list[dict[str, Any]], key: str = "r_net", n_boot: int = 10_000) -> dict[str, Any]:
        return cluster_bootstrap_diff([r[key] for r in a], [r["day"] for r in a], [r[key] for r in b], [r["day"] for r in b], n_boot=n_boot)

    d = diff(main, pl_all)
    emit(f"| Лондон − середнє плацебо | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    d = diff(main, pl_all, "r_gross")
    emit(f"| Лондон − середнє плацебо (gross) | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    for sh in PLACEBO_SHIFTS:
        d = diff(main, sel(ecn, anchor=f"placebo+{sh}h", entry="E1", target="T1"), n_boot=5000)
        emit(f"| Лондон − плацебо +{sh} год | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    emit("\n| комбінація | Лондон net | плацебо разом net | різниця | 95 % CI |\n|---|---|---|---|---|")
    for ek_, tk_ in COMBOS:
        a = sel(lon, entry=ek_, target=tk_)
        p_ = [r for r in ecn if r["anchor"].startswith("placebo") and r["entry"] == ek_ and r["target"] == tk_]
        if a and p_:
            d = diff(a, p_, n_boot=5000)
            emit(
                f"| {ek_}×{tk_} | {statistics.fmean(r['r_net'] for r in a):+.3f} | {statistics.fmean(r['r_net'] for r in p_):+.3f} | "
                f"{d['diff']:+.3f} | {fmt_ci(d['ci'])} |"
            )

    emit("\n## H1b: лондонський рейндж, свип на відкритті Нью-Йорка (другорядна, лише звіт)")
    emit(HDR)
    ny = sel(ecn, anchor="ny")
    for ek_, tk_ in COMBOS:
        emit(line(f"{ek_}×{tk_}", sel(ny, entry=ek_, target=tk_)))
    sub = sel(ny, entry="E1", target="T1")
    for p in pairs:
        emit(line(f"  E1×T1 {p}", sel(sub, sym=p)))
    emit(line("  E1×T1 2014–2019", [r for r in sub if r["day"] < split]))
    emit(line("  E1×T1 2020–2026", [r for r in sub if r["day"] >= split]))

    emit("\n## Лічильники відсіву (Лондон, ECN)")
    keys = (
        "gap",
        "flat_range",
        "flat_window",
        "zero_volume",
        "no_sweep",
        "both_first",
        "no_fractal",
        "no_bos_60m",
        "new_extreme_after_window",
        "ok",
        "E1T1:t1_lt_1r",
        "E2T1:e2_unfilled",
    )
    emit("| пара | " + " | ".join(keys) + " |")
    emit("|---" * (len(keys) + 1) + "|")
    for p in pairs:
        rs = reasons[(p, "london", "ecn")]
        emit(f"| {p} | " + " | ".join(str(rs[k]) for k in keys) + " |")
    emit("\n| пара | якір | днів пропущено (gap/flat/zero) | сетапів |\n|---|---|---|---|")
    for (p, a, e), rs in sorted(reasons.items()):
        if e == "ecn":
            emit(f"| {p} | {a} | {rs['gap'] + rs['flat_range'] + rs['flat_window'] + rs['zero_volume']} | {rs['ok']} |")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fx-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/fx", help="where <PAIR>_5m.csv live")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/session_stop_hunt_fx")
    ap.add_argument("--stage", choices=("count", "report"), required=True)
    ap.add_argument("--pairs", default=",".join(CORE), help="comma-separated Dukascopy pairs")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    fx_dir = a.fx_dir.expanduser()
    pairs = [s.strip().upper() for s in a.pairs.split(",") if s.strip()]
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        base.OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "count":
        stage_count(fx_dir, pairs)
    else:
        stage_report(fx_dir, cache, pairs)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")


if __name__ == "__main__":
    sys.exit(main())
