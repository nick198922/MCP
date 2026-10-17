"""Pre-event DVOL ramp before FOMC / CPI / NFP (IDEA-20261017-pre-event-vol-ramp).

Rules and thresholds are fixed in the card's section "Попередня реєстрація". Stages:
  uv run python scripts/verify_pre_event_vol.py --stage prereg
  uv run python scripts/verify_pre_event_vol.py --stage gates  --out ~/.cache/tradedesk-agents/pre_event_vol/gates.md
  uv run python scripts/verify_pre_event_vol.py --stage report --out ~/.cache/tradedesk-agents/pre_event_vol/report.md
``gates`` (stage 0) prints coverage, the control-count distribution, SD / half-width / MDE of the event-minus-control
differences and the sanity checks, without any mean. ``report`` runs everything. Data come from caches only (no network):
  * daily DVOL ``~/.cache/tradedesk-agents/put_skew/dvol_BTC.json`` (built by ``scripts/verify_put_skew.py`` via
    ``rt.deribit.volatility_index_data``);
  * hourly DVOL ``~/.cache/tradedesk-agents/polymarket_threshold/dvol_BTC_1h.json`` (2025-06 onwards; robustness);
  * 09:00 UTC surfaces ``~/.cache/tradedesk-agents/put_skew/snapshots_v2.json`` (H2, exploratory);
  * ``docs/verification/2026-10-15-us-macro-events.csv`` and ``src/tradedesk/data/events_seed.json`` (blocking only).
No keys, no orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.event_straddle import cluster_bootstrap, drop_top, iso_week  # noqa: E402
from tradedesk.analytics.pre_event_vol import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    control_ok,
    daily_closes,
    date_of,
    event_minus_control,
    hourly_window,
    level_profile,
    merge_events,
    ms_of,
    pick_controls,
    precision_gate,
    straddle_vega_frac,
    thesis_verdict,
    trading_verdict,
    window_change,
)
from tradedesk.analytics.weekly_straddle import constant_maturity_atm_iv  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-pre-event-vol-ramp.md"
EVENTS_CSV = ROOT / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
PRIMARY_STATUS = ("actual", "scheduled_2026")
FIRST_EVENT, LAST_EVENT = date(2021, 3, 28), date(2026, 5, 31)
EXTRA_BLOCK = ("2025-10-03", "2025-10-15", "2025-12-05", "2025-12-10", "2026-02-06", "2026-02-11", "2026-05-01")
N_CTRL, MAX_WEEKS, MIN_CTRL = 6, 8, 3
THRESHOLD, THRESHOLD_H3, NULL_MAX, DROP_TOP_MIN = 0.015, 0.024, 0.005, 0.005
HW_MAX, MDE_MAX, MDE_CARD, COV_MIN = 0.010, 0.015, 0.010, 0.80
SEED, N_BOOT = 20261017, 10_000
PROFILE = (-5, -4, -3, -2, -1, 0, 1, 2, 3)
H2_THRESHOLD = 0.048
OUT: dict[str, Any] = {"fh": None}
PT = 100.0


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(ms: int) -> str:
    return date_of(ms).isoformat()


def p(x: float | None, sign: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x * PT:+.2f}" if sign else f"{x * PT:.2f}"


# ---------------------------------------------------------------- prereg


def stage_prereg(cache: Path) -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index("## Попередня реєстрація")
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}"
    emit(line)
    with (cache / "registration.txt").open("a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- data


def load_events() -> tuple[list[dict[str, Any]], set[int], list[dict[str, str]], dict[int, set[str]]]:
    rows = list(csv.DictReader(EVENTS_CSV.open()))
    prim = [
        (ms_of(date.fromisoformat(r["date"])), r["kind"])
        for r in rows
        if r["status"] in PRIMARY_STATUS and FIRST_EVENT <= date.fromisoformat(r["date"]) <= LAST_EVENT
    ]
    by_day: dict[int, set[str]] = {}
    for d, k in prim:
        by_day.setdefault(d, set()).add(k)
    block = {ms_of(date.fromisoformat(r["date"])) for r in rows}
    with resources.files("tradedesk.data").joinpath("events_seed.json").open("r", encoding="utf-8") as f:
        block |= {(int(e["ts"]) * 1000 // DAY_MS) * DAY_MS for e in json.load(f)["events"]}
    block |= {ms_of(date.fromisoformat(s)) for s in EXTRA_BLOCK}
    return merge_events(prim), block, rows, by_day


def load_hourly(path: Path) -> dict[int, float]:
    """Hourly cache rows ``[close time s, close]`` (key = open + 1 h, verify_pm_threshold.py) -> {close time ms: close}."""
    return {int(t) * 1000: float(v) for t, v in json.loads(path.read_text())}


def load_cm7(path: Path) -> dict[int, float]:
    """day ms -> 7-day constant-maturity ATM IV at the 09:00 UTC surface (decoded one day at a time)."""
    blob = json.loads(path.read_text())
    out: dict[int, float] = {}
    for d, s in blob["days"].items():
        if not s:
            continue
        snap = dict(s, expiries={int(e): dict(x, strikes={float(k): v for k, v in x["strikes"]}) for e, x in s["expiries"]})
        v = constant_maturity_atm_iv(snap, 7.0)
        if v is not None:
            out[int(d)] = v
    del blob
    return out


# ---------------------------------------------------------------- core build


def build(closes: dict[int, float], events: list[dict[str, Any]], block: set[int]) -> list[dict[str, Any]]:
    def has(d: int) -> bool:
        return window_change(closes, d) is not None

    def ok(d: int) -> bool:
        return control_ok(d, block, has)

    out = []
    for g in events:
        t0 = g["day"]
        ctrls = pick_controls(t0, ok, n=N_CTRL, max_weeks=MAX_WEEKS)
        ev = window_change(closes, t0)
        cv = [window_change(closes, c) for c in ctrls]
        out.append(
            dict(
                g,
                ev=ev,
                ctrls=ctrls,
                cv=cv,
                dx=event_minus_control(ev, cv, min_controls=MIN_CTRL),
                dx_med=(ev - statistics.median(cv)) if ev is not None and len(cv) >= MIN_CTRL else None,
                year=date_of(t0).year,
                week=iso_week(t0),
            )
        )
    return out


def rows_of(
    recs: list[dict[str, Any]], key: str = "dx", cond: Callable[[dict[str, Any]], bool] = lambda r: True
) -> list[tuple[str, float]]:
    return [(r["week"], r[key]) for r in recs if r[key] is not None and cond(r)]


def stat_line(label: str, rows: list[tuple[str, float]]) -> tuple[str, dict[str, Any]]:
    if len(rows) < 2:
        return f"| {label} | {len(rows)} | — | — | — | — |", {"mean": None, "ci95": None, "n": len(rows)}
    b = cluster_bootstrap(rows, n_boot=N_BOOT, seed=SEED)
    v = [x for _, x in rows]
    ci = b["ci95"]
    return (
        f"| {label} | {b['n']} | {p(b['mean'])} | [{p(ci[0])}; {p(ci[1])}] | {p(statistics.median(v))} | {sum(1 for x in v if x > 0) / len(v):.0%} |",
        b,
    )


HDR = "| Вибірка | n | середнє, п. | 95 % CI (кластер ISO-тиждень) | медіана, п. | частка > 0 |\n|---|---|---|---|---|---|"


# ---------------------------------------------------------------- stages


def sanity(closes: dict[int, float], events: list[dict[str, Any]], by_day: dict[int, set[str]], rows: list[dict[str, str]]) -> None:
    ds = sorted(closes)
    gaps = [d for d in range(ds[0], ds[-1] + 1, DAY_MS) if d not in closes]
    emit("## Санітарні перевірки\n")
    emit(
        f"- Денний DVOL: {ymd(ds[0])} … {ymd(ds[-1])}, {len(ds)} свічок, пропусків {len(gaps)}"
        + (f" ({', '.join(ymd(d) for d in gaps[:10])})" if gaps else "")
    )
    vals = list(closes.values())
    emit(
        f"- Одиниці: мін {min(vals) * PT:.1f}, медіана {statistics.median(vals) * PT:.1f}, макс {max(vals) * PT:.1f} вол-пунктів (десяткові ×100)"
    )
    ch3 = [closes[d] - closes[d - 3 * DAY_MS] for d in ds if d - 3 * DAY_MS in closes]
    emit(f"- Безумовна SD 3-добової зміни DVOL: {statistics.stdev(ch3) * PT:.2f} п. (критик: 5.26), n = {len(ch3)}")
    by_y: dict[int, list[float]] = {}
    for d in ds:
        if d - 3 * DAY_MS in closes:
            by_y.setdefault(date_of(d).year, []).append(closes[d] - closes[d - 3 * DAY_MS])
    emit("  - за роками: " + ", ".join(f"{y}: {statistics.stdev(v) * PT:.2f}" for y, v in sorted(by_y.items())))
    d0 = ms_of(date(2025, 9, 17))
    a, b = closes.get(d0 - 4 * DAY_MS), closes.get(d0 - DAY_MS)
    emit(
        f"- Приклад курсу FOMC 2025-09-17: наш DVOL о 09-14 00:00 = {p(a, False)}, о 09-17 00:00 = {p(b, False)}, зміна {p(b - a if a and b else None)} п. "
        "(курс: 33.8 → 37.25, +3.45; точний час курсу невідомий)"
    )
    cnt = Counter((date_of(d).year, k) for d, ks in by_day.items() for k in ks)
    years = sorted({y for y, _ in cnt})
    emit(
        "- Подій у вибірці за роками (до об'єднання): "
        + "; ".join(f"{y}: " + ", ".join(f"{k} {cnt[(y, k)]}" for k in ("fomc", "cpi", "nfp")) for y in years)
    )
    st = Counter((r["status"], r["verification"]) for r in rows)
    emit("- Таблиця подій (статус, перевірка): " + ", ".join(f"{s}/{v} {n}" for (s, v), n in sorted(st.items())))
    emit("")


def gates(recs: list[dict[str, Any]]) -> dict[str, Any]:
    emit("## Стадія 0: покриття, контролі, ворота (без середніх)\n")
    kinds = Counter(r["kind"] for r in recs)
    emit(
        f"- Вікон після об'єднання: {len(recs)} ({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}); об'єднано подій: {sum(len(r['days']) for r in recs)}"
    )
    nc = Counter(len([v for v in r["cv"] if v is not None]) for r in recs)
    emit("- Кількість контролів на вікно: " + ", ".join(f"{k}: {nc[k]}" for k in sorted(nc)))
    no_ev = [ymd(r["day"]) for r in recs if r["ev"] is None]
    few = [ymd(r["day"]) for r in recs if r["ev"] is not None and r["dx"] is None]
    emit(f"- Без DVOL у вікні: {len(no_ev)} {no_ev[:10]}; виключено (< {MIN_CTRL} контролів): {len(few)} {few[:20]}")
    dx = [r["dx"] for r in recs if r["dx"] is not None]
    cov = len(dx) / len(recs) if recs else 0.0
    g = precision_gate(dx, hw_max=HW_MAX, mde_max=MDE_MAX)
    emit(f"- Покриття: {len(dx)}/{len(recs)} = {cov:.1%} (поріг ≥ {COV_MIN:.0%})")
    emit(
        f"- SD(ΔDVOL) = {g['sd'] * PT:.2f} п.; n = {g['n']}; кластерів (ISO-тижнів) = {len({r['week'] for r in recs if r['dx'] is not None})}"
    )
    emit(f"- Півширина 95 % CI = 1.96·SD/√n = {g['half_width'] * PT:.2f} п. (поріг ≤ {HW_MAX * PT:.1f})")
    emit(
        f"- MDE = 2.8·SD/√n = {g['mde'] * PT:.2f} п. (поріг ≤ {MDE_MAX * PT:.1f}; довідково ворота картки ≤ {MDE_CARD * PT:.1f}: {'так' if g['mde'] <= MDE_CARD else 'ні'})"
    )
    passed = g["passed"] and cov >= COV_MIN
    emit(f"- **Ворота: {'пройдено' if passed else 'НЕ пройдено'}**\n")
    return dict(g, coverage=cov, passed_all=passed)


def report(
    recs: list[dict[str, Any]],
    gate: dict[str, Any],
    closes: dict[int, float],
    hourly: dict[int, float] | None,
    cm7: dict[int, float] | None,
    by_day: dict[int, set[str]],
) -> None:
    emit("## H1: ΔDVOL = зміна у вікні T0−3 00:00 → T0 00:00 UTC мінус середнє контролів\n")
    emit(HDR)
    main_rows = rows_of(recs)
    line, b = stat_line("усі вікна", main_rows)
    emit(line)
    mean, ci = b["mean"], b["ci95"]
    sub: dict[str, dict[str, Any]] = {}
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]] = [
        ("fomc", "FOMC", lambda r: r["kind"] == "fomc"),
        ("cpi", "CPI", lambda r: r["kind"] == "cpi"),
        ("nfp", "NFP", lambda r: r["kind"] == "nfp"),
        ("multi", "multi (об'єднані)", lambda r: r["kind"] == "multi"),
        ("early", "2021–2022", lambda r: r["year"] <= 2022),
        ("late", "2023–2026", lambda r: r["year"] >= 2023),
        ("a", "2021–2023", lambda r: r["year"] <= 2023),
        ("b", "2024–2026", lambda r: r["year"] >= 2024),
        ("no22", "без 2022", lambda r: r["year"] != 2022),
    ]
    for key, label, cond in specs:
        line, sb = stat_line(label, rows_of(recs, cond=cond))
        sub[key] = sb
        emit(line)
    dt = drop_top(main_rows, 3, direction=1)
    line, bdt = stat_line("без 3 найбільших (додатних)", dt)
    emit(line)
    top = sorted((r for r in recs if r["dx"] is not None), key=lambda r: -r["dx"])[:3]
    emit(stat_line("ev без контролю (сира зміна у вікні подій)", rows_of(recs, "ev", lambda r: r["dx"] is not None))[0])
    ctrl_means = [(r["week"], statistics.fmean([v for v in r["cv"] if v is not None])) for r in recs if r["dx"] is not None]
    emit(stat_line("середня зміна в контролях", ctrl_means)[0])
    emit(stat_line("контроль = медіана контролів", rows_of(recs, "dx_med", lambda r: r["dx"] is not None))[0])
    emit("\nТоп-3 вікна за ΔDVOL: " + "; ".join(f"{ymd(r['day'])} {r['kind']} {p(r['dx'])}" for r in top))
    by_y: dict[int, list[tuple[str, float]]] = {}
    for r in recs:
        if r["dx"] is not None:
            by_y.setdefault(r["year"], []).append((r["week"], r["dx"]))
    emit("\nЗа роками:\n")
    emit(HDR)
    for y, rr in sorted(by_y.items()):
        emit(stat_line(str(y), rr)[0])

    # verdicts
    emit("\n## Вердикти за попередньою реєстрацією\n")
    tv = trading_verdict(
        mean,
        ci,
        threshold=THRESHOLD,
        sign_fomc=sub["fomc"]["mean"],
        sign_cpi=sub["cpi"]["mean"],
        mean_drop_top3=bdt["mean"],
        mean_late=sub["late"]["mean"],
        drop_top_min=DROP_TOP_MIN,
    )
    th = thesis_verdict(mean, ci, null_max=NULL_MAX)
    emit(
        f"- Ворота: {'пройдено' if gate['passed_all'] else 'НЕ пройдено'} (півширина {gate['half_width'] * PT:.2f} п., MDE {gate['mde'] * PT:.2f} п., покриття {gate['coverage']:.1%})"
    )
    emit(
        f"- Торговий H1 (середнє ≥ {THRESHOLD * PT:.1f} п., нижня межа > 0, без умов «вбити»): {'пройдено' if tv['passed'] else 'не пройдено'}; причини: {', '.join(tv['reasons']) or '—'}"
    )
    emit(
        f"  - умови «вбити»: FOMC {p(sub['fomc']['mean'])} / CPI {p(sub['cpi']['mean'])}; без топ-3 {p(bdt['mean'])} (мін {DROP_TOP_MIN * PT:.1f}); "
        f"2023–2026 {p(sub['late']['mean'])}; верхня межа CI {p(ci[1])} проти {THRESHOLD * PT:.1f}"
    )
    emit(f"- Теза курсу «IV росте перед подією»: {th} (✓ якщо нижня межа > 0; ✗ якщо верхня < {NULL_MAX * PT:.1f} п.)")
    status = "inconclusive" if not gate["passed_all"] else ("confirmed" if tv["passed"] else "rejected")
    emit(f"- **Статус картки за правилом: {status}**")
    vg = straddle_vega_frac(30.0)
    emit(
        f"\nВ економіці стреддла (на нотіонал, % спота; вега 30д ATM {vg * 100:.3f} % спота на 1 п.): "
        f"валовий результат {mean * PT * vg * 100:+.3f} %; після бази 1.5 п. {(mean * PT - 1.5) * vg * 100:+.3f} %; "
        f"після бази + дрейф VRP (2.4 п.) {(mean * PT - 2.4) * vg * 100:+.3f} %; стрес 3.4 п. {(mean * PT - 3.4) * vg * 100:+.3f} %."
    )
    emit("H3 (хеджований стреддл): " + ("потрібно запускати (H1 пройдено)" if tv["passed"] else "не запускається — H1 не пройдено"))

    # profile
    emit("\n## Описово: профіль DVOL від T0−5 до T0+3 (рівень о 00:00 UTC дня T0+k мінус рівень о T0−5 00:00), подія мінус контроль, п.\n")
    groups: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("усі", lambda r: True),
        ("FOMC", lambda r: r["kind"] == "fomc"),
        ("CPI", lambda r: r["kind"] == "cpi"),
        ("NFP", lambda r: r["kind"] == "nfp"),
    ]
    emit("| Група | n | " + " | ".join(f"T{k:+d}" for k in PROFILE) + " |\n|---|---|" + "---|" * len(PROFILE))
    for label, cond in groups:
        acc: dict[int, list[float]] = {k: [] for k in PROFILE}
        n = 0
        for r in recs:
            if r["dx"] is None or not cond(r):
                continue
            pe = level_profile(closes, r["day"], PROFILE)
            pcs = [level_profile(closes, c, PROFILE) for c in r["ctrls"]]
            used = False
            for k in PROFILE:
                cvals = [pc[k] for pc in pcs if pc[k] is not None]
                if pe[k] is not None and len(cvals) >= MIN_CTRL:
                    acc[k].append(pe[k] - statistics.fmean(cvals))
                    used = True
            n += used
        emit(f"| {label} | {n} | " + " | ".join(p(statistics.fmean(acc[k])) if acc[k] else "—" for k in PROFILE) + " |")
    emit(
        "\nT+0 = рівень о 00:00 дня події (кінець головного вікна); T+1 = 00:00 наступного дня, тобто вже після релізу. Контрольні профілі поза [Tc−3, Tc] можуть зачіпати інші події."
    )

    # hourly robustness
    if hourly:
        h0 = min(hourly)
        emit(f"\n## Стійкість: годинне вікно T0−3 09:00 → T0 11:00 (CPI, NFP, multi) / 16:00 (FOMC), годинний кеш з {ymd(h0)}\n")
        hrows, inc = [], 0
        for r in recs:
            t0 = r["day"]
            if t0 - 3 * DAY_MS + 9 * HOUR_MS < h0 or r["dx"] is None:
                continue
            first = by_day.get(t0, set())
            hh = 16 if first == {"fomc"} else 11

            def hw(d: int, hh: int = hh) -> dict[str, Any]:
                return hourly_window(hourly, d - 3 * DAY_MS + 9 * HOUR_MS, d + hh * HOUR_MS)

            e = hw(t0)
            cs = [hw(c) for c in r["ctrls"]]
            cvals = [c["change"] for c in cs if c["complete"] and c["change"] is not None]
            if not e["complete"] or e["change"] is None or len(cvals) < MIN_CTRL:
                inc += 1
                continue
            hrows.append((r["week"], e["change"] - statistics.fmean(cvals)))
        emit(f"Вікон із повним набором годин і ≥ {MIN_CTRL} повними контролями: {len(hrows)}; неповних/виключених: {inc}.\n")
        emit(HDR)
        emit(stat_line("годинне вікно, подія − контроль", hrows)[0])
        emit(stat_line("денне вікно, ті самі роки (2025-06+)", rows_of(recs, cond=lambda r: r["day"] - 3 * DAY_MS + 9 * HOUR_MS >= h0))[0])

    # H2
    if cm7 is not None:
        emit("\n## H2 (розвідка): ATM IV 7д постійного терміну, 09:00 T0−3 → 09:00 T0, подія мінус контроль\n")
        hrows = []
        for r in recs:
            t0 = r["day"]
            if t0 not in cm7 or t0 - 3 * DAY_MS not in cm7:
                continue
            ev = cm7[t0] - cm7[t0 - 3 * DAY_MS]
            cvals = [cm7[c] - cm7[c - 3 * DAY_MS] for c in r["ctrls"] if c in cm7 and c - 3 * DAY_MS in cm7]
            dxx = event_minus_control(ev, cvals, min_controls=MIN_CTRL)
            if dxx is not None:
                hrows.append((r["week"], dxx, r["kind"]))
        emit(f"Поверхонь із 7д ATM IV: {len(cm7)} днів; вікон: {len(hrows)}.\n")
        emit(HDR)
        emit(stat_line("усі", [(w, x) for w, x, _ in hrows])[0])
        for k in ("fomc", "cpi", "nfp"):
            emit(stat_line(k.upper(), [(w, x) for w, x, kk in hrows if kk == k])[0])
        if len(hrows) > 1:
            sd = statistics.stdev(x for _, x, _ in hrows)
            emit(
                f"\nSD = {sd * PT:.2f} п., MDE = {2.8 * sd / len(hrows) ** 0.5 * PT:.2f} п.; поріг торгівлі {H2_THRESHOLD * PT:.1f} п. (лише довідка, без вердикту)."
            )
    emit(f"\nПік RSS: {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("prereg", "gates", "report"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/pre_event_vol")
    ap.add_argument("--dvol", default="~/.cache/tradedesk-agents/put_skew/dvol_BTC.json")
    ap.add_argument("--dvol-1h", default="~/.cache/tradedesk-agents/polymarket_threshold/dvol_BTC_1h.json")
    ap.add_argument("--snapshots", default="~/.cache/tradedesk-agents/put_skew/snapshots_v2.json")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        OUT["fh"] = Path(a.out).expanduser().open("w")
    if a.stage == "prereg":
        stage_prereg(cache)
        return
    emit(
        f"# Pre-event DVOL ramp: вивід `scripts/verify_pre_event_vol.py --stage {a.stage}` ({datetime.now(UTC).isoformat(timespec='seconds')})\n"
    )
    closes = daily_closes(json.loads(Path(a.dvol).expanduser().read_text()))
    events, block, rows, by_day = load_events()
    sanity(closes, events, by_day, rows)
    recs = build(closes, events, block)
    gate = gates(recs)
    emit(f"Пік RSS: {rss_mb():.0f} MB\n")
    if a.stage == "gates":
        return
    hp = Path(a.dvol_1h).expanduser()
    sp = Path(a.snapshots).expanduser()
    hourly = load_hourly(hp) if hp.exists() else None
    emit(f"Поверхні: читаю {sp.name} … (RSS {rss_mb():.0f} MB)")
    cm7 = load_cm7(sp) if sp.exists() else None
    emit(f"Поверхні прочитано (RSS {rss_mb():.0f} MB)\n")
    report(recs, gate, closes, hourly, cm7, by_day)


if __name__ == "__main__":
    main()
