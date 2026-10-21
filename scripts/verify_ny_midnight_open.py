"""Course setups, step 11 (IDEA-20261016-course-setups-crypto): NY Midnight Open (H2 of LIB-20261016-ict-concepts).

(b) primary: at 09:30 New York the side is taken against the 00:00 NY open (below -> long, above -> short, the ICT
"discount / premium" reading); the move to 16:00 NY in daily ATR is compared, day by day, with the same rule on the
00:00 UTC open (placebo). Primary assets BTCUSDT (Binance USD-M perp, 5m) and EURUSD (Dukascopy 5m bid), weekdays.
(a) secondary: "Judas swing" frequency of the midnight anchor against the 00:00 UTC and all whole-hour anchors.
Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 11". Stages:
  uv run python scripts/verify_ny_midnight_open.py --stage prereg --out ~/.cache/tradedesk-agents/ny_midnight_open/registration.txt
  uv run python scripts/verify_ny_midnight_open.py --stage count  --out ~/.cache/tradedesk-agents/ny_midnight_open/count.md
  uv run python scripts/verify_ny_midnight_open.py --stage report --out ~/.cache/tradedesk-agents/ny_midnight_open/report.md
  uv run python scripts/verify_ny_midnight_open.py --stage explore --out docs/verification/2026-10-21-ny-midnight-open.explore.raw.md
``explore`` is post-hoc (Judas against the window's range), labelled exploratory and kept out of the verdict.
Data (no new downloads): 5m perp klines of the Session Stop Hunt cache (--k5m-dir, filled from data.binance.vision
through rt.binance_vision) and forex 5m CSVs (--fx-dir, written by fx_history.py). ``count`` (stage 0) counts days
and runs the sanity checks using prices up to 09:30 NY only (no outcome). No keys, no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.ny_midnight_open import (  # noqa: E402
    HOUR_MS,
    causal_atr,
    daily_ny_bars,
    day_prices,
    judas,
    ny_days,
    ny_ms,
    nymo_verdict,
    paired_summary,
    price_at,
    thesis_move,
    utc_midnight_ms,
)
from tradedesk.analytics.session_stop_hunt import Bars  # noqa: E402
from tradedesk.analytics.smt_divergence import cochran_q, joint_cluster_diff  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
SEED, N_BOOT, N_BOOT_SEC = 20261017, 10_000, 2_000
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822, 1.0, 0.394, 0.563, 0.278, 1.0)  # raw primary p of steps 1-10 (Holm family)
MIN_DAYS, GATE, MIN_EFFECT = 500, 0.05, 0.05
KS = (0.0, 0.10, 0.25)
LAST_DAY = date(2026, 9, 30)
CRYPTO_RT = 0.0014  # 0.07 % per side
ASSETS: dict[str, dict[str, Any]] = {
    "BTC": {"fam": "crypto", "primary": True, "sym": "BTCUSDT", "first": date(2021, 1, 1), "oos": date(2025, 3, 1)},
    "EURUSD": {"fam": "fx", "primary": True, "pips": 1.3, "first": date(2014, 1, 1), "oos": date(2023, 7, 1)},
    "ETH": {"fam": "crypto", "primary": False, "sym": "ETHUSDT", "first": date(2021, 1, 1), "oos": date(2025, 3, 1)},
    "GBPUSD": {"fam": "fx", "primary": False, "pips": 1.6, "first": date(2014, 1, 1), "oos": date(2023, 7, 1)},
}
REFS = ("uo", "p19", "p20", "p21", "p22", "p23", "d00", "d01", "d02", "d03", "d04", "d05", "d06", "d07", "d08", "d0830")
REF_LABEL = (
    {"uo": "00:00 UTC (плацебо)", "d00": "00:00 NY (MO)", "d0830": "08:30 NY"}
    | {f"p{h}": f"{h}:00 NY (d−1)" for h in range(19, 24)}
    | {f"d{h:02d}": f"{h:02d}:00 NY" for h in range(1, 9)}
)


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def fmt_ci(ci: tuple[float, float] | None, f: str = "+.4f") -> str:
    return "—" if ci is None else f"[{ci[0]:{f}}; {ci[1]:{f}}]"


def fp(p: float | None) -> str:
    return "—" if p is None else f"{p:.4f}"


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 11")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- data


def load_crypto(k5m: Path, sym: str) -> Bars:
    rows: list[list[float]] = []
    for p in sorted((k5m / sym).glob("*.json")):
        rows.extend(json.loads(p.read_text()) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 5))
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    return Bars.from_rows(a[:, :5])


def load_fx(fx_dir: Path, pair: str) -> Bars:
    """5m bid bars from fx_history.py's CSV; zero-volume (flat) bars count as missing."""
    a = np.loadtxt(fx_dir / f"{pair}_5m.csv", delimiter=",", skiprows=1, dtype=float, ndmin=2)
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    a = a[a[:, 5] > 0]
    return Bars.from_rows(a[:, :5])


def load(asset: str, k5m: Path, fx_dir: Path) -> Bars:
    cfg = ASSETS[asset]
    return load_crypto(k5m, cfg["sym"]) if cfg["fam"] == "crypto" else load_fx(fx_dir, asset)


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_rows(asset: str, b: Bars, why: Counter[str]) -> list[dict[str, Any]]:
    """One row per NY day (weekdays and weekends) with prices at the named times, ATR and Judas flags."""
    cfg = ASSETS[asset]
    first: date = cfg["first"]
    all_days = ny_days(first - timedelta(days=40), LAST_DAY)
    atr = causal_atr(daily_ny_bars(b, all_days))
    rows: list[dict[str, Any]] = []
    for d in ny_days(first, LAST_DAY):
        wk = d.weekday() < 5
        if cfg["fam"] == "fx" and not wk:
            continue
        px, r = day_prices(b, d)
        if px is None:
            why[f"{asset}:{'wd' if wk else 'we'}:{r}"] += 1
            continue
        a = atr.get(d, float("nan"))
        if not math.isfinite(a) or a <= 0:
            why[f"{asset}:{'wd' if wk else 'we'}:atr"] += 1
            continue
        anchors = {"mo": ny_ms(d, 0), "utc": utc_midnight_ms(d)} | {f"h{h:02d}": ny_ms(d, h) for h in range(1, 24)}
        jd: dict[str, Any] = {}
        for lab, t in anchors.items():
            f, _ = judas(b, t, a, KS)
            jd[lab] = None if f is None else [f[k] for k in KS]
        rows.append(
            {
                "asset": asset,
                "day": d.isoformat(),
                "week": iso_week(d),
                "wd": wk,
                "oos": d >= cfg["oos"],
                "atr": a,
                "px": px,
                "j": jd,
            }
        )
        why[f"{asset}:{'wd' if wk else 'we'}:ok"] += 1
    return rows


def cost(asset: str, price: float) -> float:
    cfg = ASSETS[asset]
    return CRYPTO_RT if cfg["fam"] == "crypto" else cfg["pips"] * 1e-4 / price


def outcomes(r: dict[str, Any], ref: str, dec: str = "d0930", ex: str = "d1600") -> tuple[int, float, float]:
    px = r["px"]
    return thesis_move(px[ref], px[dec], px[ex], r["atr"])


# ---------------------------------------------------------------- sanity


def sanity(bars: dict[str, Bars]) -> None:
    emit("## Санітарні перевірки")
    for a, b in bars.items():
        emit(
            f"- {a}: {b.t.size} 5m-барів {datetime.fromtimestamp(b.t[0] / 1000, UTC):%Y-%m-%d %H:%M} … {datetime.fromtimestamp(b.t[-1] / 1000, UTC):%Y-%m-%d %H:%M} UTC"
        )
    btc = bars["BTC"]
    t = int(datetime(2024, 12, 31, 23, 55, tzinfo=UTC).timestamp() * 1000)
    i = int(np.searchsorted(btc.t, t))
    emit(f"- BTCUSDT 5m-бар 2024-12-31 23:55 UTC: close {btc.c[i]:g} (4h-еталон кроків 1–10: 93548.9)")
    eu = bars["EURUSD"]
    for s, ref in (("2014-01-02T12:00", "≈ 1.369"), ("2022-09-28T12:00", "≈ 0.957")):
        tt = int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)
        emit(f"- EURUSD ціна о {s} UTC: {price_at(eu, tt):.5f} (еталон стадії 0 кроку 10 {ref})")
    for d in (date(2024, 3, 8), date(2024, 3, 11), date(2024, 11, 1), date(2024, 11, 4)):
        mo = ny_ms(d, 0)
        emit(
            f"- {d} ({d:%a}): MO = {datetime.fromtimestamp(mo / 1000, UTC):%Y-%m-%d %H:%M} UTC, 09:30 NY = {datetime.fromtimestamp(ny_ms(d, 9, 30) / 1000, UTC):%H:%M} UTC, UO на {(utc_midnight_ms(d) - mo) / HOUR_MS:+.0f} год від MO"
        )
    emit(f"- пікова RSS {rss_mb():.0f} MB")


def collect(k5m: Path, fx_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Bars], Counter[str]]:
    why: Counter[str] = Counter()
    rows: dict[str, list[dict[str, Any]]] = {}
    bars: dict[str, Bars] = {}
    t0 = time.time()
    for a in ASSETS:
        bars[a] = load(a, k5m, fx_dir)
        rows[a] = build_rows(a, bars[a], why)
        emit(f"  [rows] {a}: {len(rows[a])} days in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    return rows, bars, why


def stage_count(k5m: Path, fx_dir: Path) -> None:
    emit("# Крок 11 NY Midnight Open, стадія 0: кількість днів і санітарні перевірки (без результатів)")
    rows, bars, why = collect(k5m, fx_dir)
    sanity(bars)
    emit(
        "\n| актив | днів буднів IS | OOS | вихідних (крипта) | частка s_MO ≠ s_UO (IS, буд.) | MO-години UTC (буд.) | очікуване MDE (SD y 0.35) |\n|---|---|---|---|---|---|---|"
    )
    for a, rr in rows.items():
        wd = [r for r in rr if r["wd"]]
        i_ = [r for r in wd if not r["oos"]]
        o_ = [r for r in wd if r["oos"]]
        dis = [outcomes(r, "d00")[0] != outcomes(r, "uo")[0] for r in i_]  # sides use prices up to 09:30 only
        f = statistics.fmean(dis) if dis else float("nan")
        hrs = Counter(datetime.fromtimestamp(ny_ms(date.fromisoformat(r["day"]), 0) / 1000, UTC).hour for r in wd)
        mde = 2.8 * 2 * 0.35 * math.sqrt(f) / math.sqrt(max(1, len(i_)))
        emit(f"| {a} | {len(i_)} | {len(o_)} | {sum(not r['wd'] for r in rr)} | {f:.1%} | {dict(sorted(hrs.items()))} | {mde:.3f} |")
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    for a, rr in rows.items():
        cov = Counter(lab for r in rr if r["wd"] for lab, v in r["j"].items() if v is None)
        emit(f"- {a}: якорів Judas без даних (буд.): {dict(sorted(cov.items()))}")


# ---------------------------------------------------------------- report helpers


def prim(
    rows: list[dict[str, Any]], asset: str, ref: str = "d00", plc: str = "uo", dec: str = "d0930", ex: str = "d1600"
) -> list[dict[str, Any]]:
    """Paired per-day records: y (thesis, ATR) and r (gross, notional) of the reference and the placebo."""
    out = []
    for r in rows:
        s1, y1, r1 = outcomes(r, ref, dec, ex)
        s0, y0, r0 = outcomes(r, plc, dec, ex)
        if s1 == 0 or s0 == 0 or not (math.isfinite(y1) and math.isfinite(y0)):
            continue
        c = cost(asset, r["px"][dec])
        out.append(
            {
                "day": r["day"],
                "week": r["week"],
                "oos": r["oos"],
                "y1": y1,
                "y0": y0,
                "d": y1 - y0,
                "n1": r1 - c,
                "n0": r0 - c,
                "g1": r1,
                "cost": c,
                "dis": s1 != s0,
                "dist1": abs(r["px"][dec] - r["px"][ref]) / r["atr"],
                "dist0": abs(r["px"][dec] - r["px"][plc]) / r["atr"],
            }
        )
    return out


def ps(rows: list[dict[str, Any]], key: str, cl: str = "day", n_boot: int = N_BOOT) -> dict[str, Any]:
    return paired_summary([r[key] for r in rows], [r[cl] for r in rows], n_boot=n_boot, seed=SEED)


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if not r["oos"]], [r for r in rows if r["oos"]]


def block(asset: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pr = prim([r for r in rows if r["wd"]], asset)
    i_, o_ = split(pr)
    d, dw, do = ps(i_, "d"), ps(i_, "d", "week"), ps(o_, "d")
    return {
        "i": i_,
        "o": o_,
        "d": d,
        "dw": dw,
        "do": do,
        "y1": ps(i_, "y1", n_boot=N_BOOT_SEC),
        "y0": ps(i_, "y0", n_boot=N_BOOT_SEC),
        "y1o": ps(o_, "y1", n_boot=N_BOOT_SEC),
        "y0o": ps(o_, "y0", n_boot=N_BOOT_SEC),
        "tr": ps(i_, "n1"),
        "tro": ps(o_, "n1", n_boot=N_BOOT_SEC),
        "tr0": ps(i_, "n0", n_boot=N_BOOT_SEC),
        "mde": 2.8 * d["se"] if math.isfinite(d["se"]) else float("inf"),
    }


def emit_block(asset: str, b: dict[str, Any]) -> None:
    emit(f"\n### {asset} (будні, рішення 09:30 NY → вихід 16:00 NY)")
    emit("| величина | IS | OOS |\n|---|---|---|")
    emit(f"| днів (s_MO ≠ s_UO) | {len(b['i'])} ({sum(r['dis'] for r in b['i'])}) | {len(b['o'])} ({sum(r['dis'] for r in b['o'])}) |")
    emit(f"| E[y_MO], ATR [CI] | {b['y1']['mean']:+.4f} {fmt_ci(b['y1']['ci'])} | {b['y1o']['mean']:+.4f} {fmt_ci(b['y1o']['ci'])} |")
    emit(f"| E[y_UO], ATR [CI] | {b['y0']['mean']:+.4f} {fmt_ci(b['y0']['ci'])} | {b['y0o']['mean']:+.4f} {fmt_ci(b['y0o']['ci'])} |")
    emit(
        f"| **Δ = E[y_MO] − E[y_UO]**, ATR [CI дати] | **{b['d']['mean']:+.4f} {fmt_ci(b['d']['ci'])}**, p {fp(b['d']['p'])} (двобічне {fp(b['d']['p2'])}) | {b['do']['mean']:+.4f} {fmt_ci(b['do']['ci'])}, p {fp(b['do']['p'])} |"
    )
    emit(f"| Δ [CI ISO-тижні] | {fmt_ci(b['dw']['ci'])} | — |")
    emit(f"| SE, MDE = 2.8·SE | {b['d']['se']:.4f}, {b['mde']:.4f} | — |")
    emit(
        f"| hit rate y_MO > 0 / y_UO > 0 | {statistics.fmean(r['y1'] > 0 for r in b['i']):.1%} / {statistics.fmean(r['y0'] > 0 for r in b['i']):.1%} | {statistics.fmean(r['y1'] > 0 for r in b['o']):.1%} / {statistics.fmean(r['y0'] > 0 for r in b['o']):.1%} |"
    )
    emit(
        f"| угода MO, E[net] (на номінал) [CI] | {b['tr']['mean']:+.4%} {fmt_ci(b['tr']['ci'], '+.4%')} | {b['tro']['mean']:+.4%} {fmt_ci(b['tro']['ci'], '+.4%')} |"
    )
    emit(f"| угода UO, E[net] | {b['tr0']['mean']:+.4%} {fmt_ci(b['tr0']['ci'], '+.4%')} | — |")
    emit(f"| середнє коло | {statistics.fmean(r['cost'] for r in b['i']):.4%} | — |")


def jflags(rows: list[dict[str, Any]], lab: str, ki: int) -> dict[str, int]:
    return {r["day"]: r["j"][lab][ki] for r in rows if r["j"].get(lab) is not None}


# ---------------------------------------------------------------- report


def stage_report(k5m: Path, fx_dir: Path, own: Path) -> None:
    emit("# Крок 11: NY Midnight Open — результати")
    emit(
        "Дані: BTCUSDT / ETHUSDT перпи Binance USD-M 5m (дні 2021-01-01 … 2026-09-30, IS < 2025-03-01 ≤ OOS), EURUSD / GBPUSD Dukascopy 5m bid (2014-01-01 … 2026-09-30, IS < 2023-07-01 ≤ OOS). Час — America/New_York. y — рух 09:30 → 16:00 NY у бік тези (нижче рівня — лонг) у денних ATR(14); угода — на номінал, net після кола (крипта 0.14 %, форекс 1.3 / 1.6 піпса).\n"
    )
    rows, bars, why = collect(k5m, fx_dir)
    del bars
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    (own / "rows.json").write_text(json.dumps(rows, separators=(",", ":")))

    # ---- primary
    emit("\n## Основне (б): MO проти плацебо 00:00 UTC, будні")
    blocks = {a: block(a, rows[a]) for a in ASSETS}
    for a in ASSETS:
        emit_block(a + ("" if ASSETS[a]["primary"] else " (другорядний)"), blocks[a])
    pb, pe = blocks["BTC"]["d"]["p"], blocks["EURUSD"]["d"]["p"]
    p_step = min(1.0, 2 * min(pb, pe))
    p_adj = holm([*STEP_P, p_step])[-1]
    emit("\n## Ворота, Holm і вердикт (за реєстрацією)")
    emit(f"- сирі p: BTC {fp(pb)}, EURUSD {fp(pe)}; p кроку = min(1, 2·min) = {fp(p_step)}; Holm (m = 11) {fp(p_adj)}")
    verdicts = {}
    for a in ("BTC", "EURUSD", "ETH", "GBPUSD"):
        b = blocks[a]
        v = nymo_verdict(
            n_days=len(b["i"]),
            mde=b["mde"],
            diff=b["d"]["mean"],
            ci_day=b["d"]["ci"],
            ci_week=b["dw"]["ci"],
            p_holm=p_adj if ASSETS[a]["primary"] else None,
            oos_diff=b["do"]["mean"],
            oos_p=b["do"]["p"],
            trade_ci_is=b["tr"]["ci"],
            trade_oos_mean=b["tro"]["mean"],
            min_days=MIN_DAYS,
            gate=GATE,
            min_effect=MIN_EFFECT,
        )
        verdicts[a] = v
        emit(
            f"- {a}{'' if ASSETS[a]['primary'] else ' (другорядний; без Holm картки)'}: днів IS {len(b['i'])} (поріг {MIN_DAYS}), MDE {b['mde']:.4f} (ворота {GATE}), Δ {b['d']['mean']:+.4f} (мін. ефект {MIN_EFFECT}) → **{v}**"
        )
    vs = {verdicts["BTC"], verdicts["EURUSD"]}
    step_v = "confirmed" if "confirmed" in vs else ("rejected" if vs == {"rejected"} else "inconclusive")
    emit(f"- **вердикт кроку: {step_v}**")

    # ---- references
    emit("\n## Інші рівні відліку (рішення 09:30, вихід 16:00, будні, IS): E[y_ref] і Δ = MO − ref")
    emit("| актив | " + " | ".join(REF_LABEL[x] for x in REFS) + " |\n|---|" + "---|" * len(REFS))
    sec_p: list[tuple[str, float]] = []
    for a in ASSETS:
        wd = [r for r in rows[a] if r["wd"]]
        cells = []
        for ref in REFS:
            pr = split(prim(wd, a, ref="d00", plc=ref))[0] if ref != "d00" else split(prim(wd, a))[0]
            m0 = statistics.fmean(x["y0"] for x in pr) if ref != "d00" else statistics.fmean(x["y1"] for x in pr)
            if ref == "d00":
                cells.append(f"**{m0:+.4f}**")
                continue
            dd = ps(pr, "d", n_boot=N_BOOT_SEC)
            cells.append(f"{m0:+.4f} (Δ {dd['mean']:+.4f})")
            if ref != "uo":
                sec_p.append((f"{a}/ref {ref}", dd["p"]))
        emit(f"| {a} | " + " | ".join(cells) + " |")
    for a in ASSETS:
        wd = split([r for r in rows[a] if r["wd"]])[0]
        means = {}
        for ref in REFS:
            pr = prim(wd, a, ref=ref, plc="uo") if ref != "uo" else prim(wd, a)
            means[ref] = statistics.fmean(x["y1"] if ref != "uo" else x["y0"] for x in pr)
        rank = 1 + sum(v > means["d00"] for k, v in means.items() if k != "d00")
        emit(f"- {a}: ранг MO серед {len(REFS)} рівнів (1 — найбільше E[y]): {rank}")

    # ---- time grid
    emit("\n## Сітка часу рішення / виходу (Δ = MO − UO, будні)")
    emit("| актив | рішення | вихід | n IS | Δ IS [CI] | p | Δ OOS | E[y_MO] IS | E[net] MO IS |\n|---|---|---|---|---|---|---|---|---|")
    for a in ASSETS:
        wd = [r for r in rows[a] if r["wd"]]
        for dec in ("d05", "d0830", "d0930"):
            for ex in ("d1200", "d1600"):
                pr = prim(wd, a, dec=dec, ex=ex)
                i_, o_ = split(pr)
                dd, do = ps(i_, "d", n_boot=N_BOOT_SEC), ps(o_, "d", n_boot=N_BOOT_SEC)
                emit(
                    f"| {a} | {dec[1:]} | {ex[1:]} | {len(i_)} | {dd['mean']:+.4f} {fmt_ci(dd['ci'])} | {fp(dd['p'])} | {do['mean']:+.4f} | {statistics.fmean(x['y1'] for x in i_):+.4f} | {statistics.fmean(x['n1'] for x in i_):+.4%} |"
                )
                if not (dec == "d0930" and ex == "d1600"):
                    sec_p.append((f"{a}/{dec}->{ex}", dd["p"]))

    # ---- distance filter
    emit("\n## Фільтр відстані |P_09:30 − рівень| ≥ 0.25 ATR (будні, IS; групи непарні)")
    emit(
        "| актив | n MO / UO | E[y_MO] | E[y_UO] | Δ [CI] | p | E[net] MO [CI] | Δ OOS | E[net] MO OOS (n) |\n|---|---|---|---|---|---|---|---|---|"
    )
    for a in ASSETS:
        pr = prim([r for r in rows[a] if r["wd"]], a)
        for part, lab in ((split(pr)[0], "IS"), (split(pr)[1], "OOS")):
            g1 = [x for x in part if x["dist1"] >= 0.25]
            g0 = [x for x in part if x["dist0"] >= 0.25]
            dd = joint_cluster_diff(
                [x["y1"] for x in g1], [x["day"] for x in g1], [x["y0"] for x in g0], [x["day"] for x in g0], n_boot=N_BOOT_SEC, seed=SEED
            )
            if lab == "IS":
                keep = (g1, g0, dd)
            else:
                g1i, g0i, di = keep
                tn = ps(g1i, "n1", n_boot=N_BOOT_SEC)
                emit(
                    f"| {a} | {len(g1i)} / {len(g0i)} | {statistics.fmean(x['y1'] for x in g1i):+.4f} | {statistics.fmean(x['y0'] for x in g0i):+.4f} | {di['diff']:+.4f} {fmt_ci(di['ci'])} | {fp(di['p'])} | {tn['mean']:+.4%} {fmt_ci(tn['ci'], '+.4%')} | {dd['diff']:+.4f} | {statistics.fmean(x['n1'] for x in g1):+.4%} ({len(g1)}) |"
                )
                sec_p.append((f"{a}/dist0.25", di["p"]))

    # ---- costs
    emit("\n## Угода MO: витрати (будні, IS, на номінал)")
    emit("| актив | gross | net ×0.5 | net ×1 | net ×2 | net ×3 | OOS net |\n|---|---|---|---|---|---|---|")
    for a in ASSETS:
        i_, o_ = blocks[a]["i"], blocks[a]["o"]
        g = statistics.fmean(x["g1"] for x in i_)
        cs = [statistics.fmean(x["g1"] - m * x["cost"] for x in i_) for m in (0.5, 1, 2, 3)]
        emit(f"| {a} | {g:+.4%} | " + " | ".join(f"{c:+.4%}" for c in cs) + f" | {statistics.fmean(x['n1'] for x in o_):+.4%} |")
    for a in ("EURUSD", "GBPUSD"):
        i_ = blocks[a]["i"]
        extra = "; ".join(
            f"{pp} піпса: {statistics.fmean(x['g1'] - pp * x['cost'] / ASSETS[a]['pips'] for x in i_):+.4%}" for pp in (1.0, 2.0)
        )
        emit(f"- {a} з колом {extra}")

    # ---- weekends
    emit("\n## Вихідні (крипта, Сб–Нд NY; той самий час)")
    for a in ("BTC", "ETH"):
        pr = prim([r for r in rows[a] if not r["wd"]], a)
        i_, o_ = split(pr)
        dd = ps(i_, "d", n_boot=N_BOOT_SEC)
        emit(
            f"- {a}: n IS {len(i_)}, Δ {dd['mean']:+.4f} {fmt_ci(dd['ci'])}, p {fp(dd['p'])}; E[y_MO] {statistics.fmean(x['y1'] for x in i_):+.4f}; Δ OOS {statistics.fmean(x['d'] for x in o_):+.4f}"
        )
        sec_p.append((f"{a}/weekend", dd["p"]))

    adj = holm([p for _, p in sec_p])
    emit("\n### Другорядні рядки (б): сирі p і Holm усередині кроку (15 найменших)")
    emit("| рядок | сире p | Holm у кроці |\n|---|---|---|")
    for (lab, p), pa in sorted(zip(sec_p, adj, strict=True), key=lambda x: x[0][1])[:15]:
        emit(f"| {lab} | {p:.4f} | {pa:.4f} |")
    emit(f"(з {len(sec_p)} рядків)")

    # ---- per asset
    emit("\n## Перевірка по активах (рішення власника 2026-10-17)")
    emit("| актив | n IS | Δ IS [CI] | SE | Δ OOS [CI] | E[net] MO IS | E[net] MO OOS |\n|---|---|---|---|---|---|---|")
    est, ses = [], []
    for a in ASSETS:
        b = blocks[a]
        est.append(b["d"]["mean"])
        ses.append(b["d"]["se"])
        emit(
            f"| {a} | {len(b['i'])} | {b['d']['mean']:+.4f} {fmt_ci(b['d']['ci'])} | {b['d']['se']:.4f} | {b['do']['mean']:+.4f} {fmt_ci(b['do']['ci'])} | {b['tr']['mean']:+.4%} | {b['tro']['mean']:+.4%} |"
        )
    q = cochran_q(est, ses)
    emit(f"- Cochran Q {q['q']:.2f}, df {q['df']}, p {fp(q['p'])}, I² {q['i2']:.0%} (Δ у ATR — порівнянні між активами)")
    emit(
        "- сталість: знак Δ IS → OOS: "
        + "; ".join(f"{a} {'+' if blocks[a]['d']['mean'] > 0 else '−'} → {'+' if blocks[a]['do']['mean'] > 0 else '−'}" for a in ASSETS)
        + " (топ-чверть із 4 активів — 1 актив, тож лише знаки)"
    )

    # ---- robustness
    emit("\n## Стійкість основної метрики (будні, IS)")
    for a in ASSETS:
        b = blocks[a]
        i_ = b["i"]
        top = sorted(i_, key=lambda x: -x["d"])
        parts = []
        for k in (5, 10, 20):
            drop = {x["day"] for x in top[:k]}
            dd = ps([x for x in i_ if x["day"] not in drop], "d", n_boot=N_BOOT_SEC)
            parts.append(f"без {k} найкращих {dd['mean']:+.4f} {fmt_ci(dd['ci'])}")
        emit(f"- {a}: " + "; ".join(parts))
        yrs = defaultdict(list)
        for x in i_ + b["o"]:
            yrs[x["day"][:4]].append(x["d"])
        emit("  - за роками (Δ, n): " + "; ".join(f"{y}: {statistics.fmean(v):+.4f} ({len(v)})" for y, v in sorted(yrs.items())))
        days = sorted(x["day"] for x in i_)
        mid = days[len(days) // 2]
        h1 = ps([x for x in i_ if x["day"] < mid], "d", n_boot=N_BOOT_SEC)
        h2 = ps([x for x in i_ if x["day"] >= mid], "d", n_boot=N_BOOT_SEC)
        emit(f"  - половини IS (до / з {mid}): {h1['mean']:+.4f} {fmt_ci(h1['ci'])} / {h2['mean']:+.4f} {fmt_ci(h2['ci'])}")
        sd = statistics.pstdev(x["y1"] for x in i_)
        emit(f"  - SD y_MO {sd:.3f} ATR; частка днів s_MO ≠ s_UO {statistics.fmean(x['dis'] for x in i_):.1%}")

    # ---- Judas
    emit("\n## (а) Judas swing (будні; описове, поза Holm картки)")
    emit(
        "| актив | k, ATR | J_MO IS | J_UTC IS | ΔJ IS [CI] | p | J_MO − середнє 23 інших годин | ранг MO з 24 | ΔJ OOS | позначка |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ASSETS:
        wd = [r for r in rows[a] if r["wd"]]
        for ki, k in enumerate(KS):
            marks = {}
            res = {}
            for part, lab in ((split(wd)[0], "IS"), (split(wd)[1], "OOS")):
                mo, ut = jflags(part, "mo", ki), jflags(part, "utc", ki)
                common = sorted(set(mo) & set(ut))
                dd = paired_summary([mo[d] - ut[d] for d in common], common, n_boot=N_BOOT_SEC, seed=SEED)
                # hourly profile on the days where all 24 hourly anchors exist (forex: windows over the Friday close drop)
                hj = {f"h{h:02d}": (mo if h == 0 else jflags(part, f"h{h:02d}", ki)) for h in range(24)}
                full = set.intersection(*(set(v) for v in hj.values()))
                hours = {h: statistics.fmean(v[d] for d in full) for h, v in hj.items()} if full else {"h00": float("nan")}
                res[lab] = (statistics.fmean(mo[d] for d in common), statistics.fmean(ut[d] for d in common), dd, hours)
            jm, ju, dd, hours = res["IS"]
            other = [v for h, v in hours.items() if h != "h00"]
            rank = 1 + sum(v > hours["h00"] for v in other)
            ddo = res["OOS"][2]
            if dd["ci"] is not None and dd["ci"][0] > 0:
                mark = "✓" if rank == 1 and ddo["mean"] > 0 else "≈"
            else:
                mark = "✗"
            marks[k] = mark
            emit(
                f"| {a} | {k:.2f} | {jm:.1%} | {ju:.1%} | {dd['mean']:+.1%} {fmt_ci(dd['ci'], '+.1%')} | {fp(dd['p'])} | {hours['h00'] - statistics.fmean(other):+.1%} | {rank} | {ddo['mean']:+.1%} | {mark} |"
            )
    emit("\nJ за годиною якоря NY (IS, дні з усіма 24 погодинними якорями):")
    for a in ASSETS:
        wd_is = split([r for r in rows[a] if r["wd"]])[0]
        for ki, k in enumerate(KS):
            hj = {h: jflags(wd_is, "mo" if h == 0 else f"h{h:02d}", ki) for h in range(24)}
            full = set.intersection(*(set(v) for v in hj.values()))
            prof = "; ".join(f"{h}: {statistics.fmean(v[d] for d in full):.1%}" for h, v in hj.items())
            emit(f"- {a}, k = {k:.2f} ({len(full)} днів): {prof}")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def stage_explore(k5m: Path, fx_dir: Path) -> None:
    """Exploratory, after the registered run: is the Judas excess of the midnight anchor (forex, k >= 0.10) explained by
    how much of the day's range falls into the 9.5 h window (session volatility), not by a manipulation pattern?"""
    from tradedesk.analytics.ny_midnight_open import CLOSE_MS, WINDOW_MS, extremes

    emit("# Крок 11, дослідницьке (після прогону; на вердикт не впливає): Judas і волатильність вікна")
    emit(
        "Для якорів MO і 00:00 UTC: W = (хай − лоу вікна [A, A + 9.5 год)) / ATR; J_k — як у реєстрації; «прямий» хід O_k — вікно пройшло ≥ k·ATR у бік закриття дня. ΔJ усередині децилів W (межі — по обох якорях разом, IS), зважене кількістю днів MO у дециді. Будні, IS.\n"
    )
    emit(
        "| актив | E[W] MO | E[W] UTC | k | J_MO | J_UTC | O_MO | O_UTC | ΔJ сирий | ΔJ у децилях W | ΔO у децилях W |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ASSETS:
        b = load(a, k5m, fx_dir)
        cfg = ASSETS[a]
        atr = causal_atr(daily_ny_bars(b, ny_days(cfg["first"] - timedelta(days=40), LAST_DAY)))
        per: dict[str, list[tuple[float, list[int], list[int]]]] = {"mo": [], "utc": []}
        for d in ny_days(cfg["first"], cfg["oos"] - timedelta(days=1)):
            if d.weekday() >= 5:
                continue
            at = atr.get(d, float("nan"))
            if not math.isfinite(at) or at <= 0:
                continue
            for lab, t in (("mo", ny_ms(d, 0)), ("utc", utc_midnight_ms(d))):
                f, _ = judas(b, t, at, KS)
                if f is None:
                    continue
                p0, p1 = price_at(b, t), price_at(b, t + CLOSE_MS)
                hi, lo = extremes(b, t, t + WINDOW_MS)
                up = p1 > p0
                o = [int(hi > p0 + k * at) if up else int(lo < p0 - k * at) for k in KS]
                per[lab].append(((hi - lo) / at, [f[k] for k in KS], o))
        allw = [x[0] for lab in per for x in per[lab]]
        cuts = np.quantile(allw, np.linspace(0.1, 0.9, 9))
        bins = {lab: np.searchsorted(cuts, [x[0] for x in per[lab]]) for lab in per}
        for ki, k in enumerate(KS):
            jm = np.array([x[1][ki] for x in per["mo"]])
            ju = np.array([x[1][ki] for x in per["utc"]])
            om = np.array([x[2][ki] for x in per["mo"]])
            ou = np.array([x[2][ki] for x in per["utc"]])
            dj = do = wsum = 0.0
            for q in range(10):
                m1, m0 = bins["mo"] == q, bins["utc"] == q
                if m1.sum() and m0.sum():
                    dj += m1.sum() * (jm[m1].mean() - ju[m0].mean())
                    do += m1.sum() * (om[m1].mean() - ou[m0].mean())
                    wsum += m1.sum()
            emit(
                f"| {a} | {statistics.fmean(x[0] for x in per['mo']):.3f} | {statistics.fmean(x[0] for x in per['utc']):.3f} | {k:.2f} | {jm.mean():.1%} | {ju.mean():.1%} | {om.mean():.1%} | {ou.mean():.1%} | {jm.mean() - ju.mean():+.1%} | {dj / wsum:+.1%} | {do / wsum:+.1%} |"
            )
        del b
        emit(f"  [explore] {a} done, rss {rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/ny_midnight_open")
    ap.add_argument("--k5m-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/session_stop_hunt/k5m")
    ap.add_argument("--fx-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/fx")
    ap.add_argument("--stage", choices=("prereg", "count", "report", "explore"), required=True)
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    own = a.cache_dir.expanduser()
    own.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("a" if a.stage == "prereg" else "w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "count":
        stage_count(a.k5m_dir.expanduser(), a.fx_dir.expanduser())
    elif a.stage == "explore":
        stage_explore(a.k5m_dir.expanduser(), a.fx_dir.expanduser())
    else:
        stage_report(a.k5m_dir.expanduser(), a.fx_dir.expanduser(), own)
    if a.stage != "prereg":
        emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
