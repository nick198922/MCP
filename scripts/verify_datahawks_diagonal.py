"""Exploratory: DataHawks BTC call diagonal (LIB-20261115-datahawks-btc-diagonal, D2) on 2021-2026 history.

Buy a ~ATM call on the expiry nearest 40 d (30-60 d), sell a ~0.16-delta Friday call 4-8 d out (1:1 or 0.5:1), roll
the short every week while the long has >= 8 d, close the long at the 09:00 mark when it has < 8 d. Baseline: the
naked long call with the same entry and exit. Pure logic: `tradedesk.analytics.call_diagonal`.

No network: the put-skew-v2 cache (``--src-dir``, read only: ``snapshots_v2.json`` daily 09:00 surfaces, delivery
prices, DVOL) and the macro events CSV of the repo. Outputs (full text, trades CSV) go to ``--cache-dir``.
    uv run python scripts/verify_datahawks_diagonal.py --stage coverage
    uv run python scripts/verify_datahawks_diagonal.py --stage full --out ~/.cache/tradedesk-agents/datahawks_diagonal/full.md
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_p3_trend_spread as vp3  # noqa: E402  (scripts/ is not a package)
import verify_p10_calendar as vp10  # noqa: E402
from common_helpers import ymd_ms  # noqa: E402

from tradedesk.analytics.calendar_p10 import inversion_at  # noqa: E402
from tradedesk.analytics.call_diagonal import DEFAULTS, front_month_diff, plan_entry, run_position  # noqa: E402
from tradedesk.analytics.condor_dvol import cluster_bootstrap_diff  # noqa: E402
from tradedesk.analytics.put_skew import rr25  # noqa: E402
from tradedesk.analytics.trend_spread import DAY_MS, cluster_bootstrap, holm_adjust  # noqa: E402

OOS = vp10.OOS  # 2024-01-01
LAST_EXPIRY_MS = vp10.LAST_EXPIRY_MS
FIRST = vp3.FIRST
COVERAGE_MIN = 0.60
N_MIN = 20
BLOCK_WEEKS = 8  # cluster = 8-week calendar block (a position lives ~5 weeks)
INVERSION = 0.015
N_BOOT = 10_000
SEED = 20261115
VARIANTS = {"diag1": 1.0, "diag05": 0.5, "naked": 0.0}
# Strike rule: "fresh" (registered primary: only strikes with a fresh mark) or "grid" (variant B, registered after the
# coverage stage and before any P&L: listed 1000-USD grid strike priced from the smile between fresh strikes).
BASEP: dict[str, Any] = dict(DEFAULTS)
log = vp3.log


# ----------------------------------------------------------------------------- samples


def rr30(snap: dict[str, Any]) -> float | None:
    """RR25 at 30 d, linear in DTE between the bracketing expiries in [7, 60] d (as scripts/explore_rr25_bottom_btc.py)."""
    pts = []
    for x in snap["expiries"].values():
        dte = x["T"] * 365
        if 7 <= dte <= 60:
            v = rr25(x)
            if v is not None:
                pts.append((dte, v))
    lo = [p for p in pts if p[0] <= 30]
    hi = [p for p in pts if p[0] >= 30]
    if not lo or not hi:
        return None
    a, b = max(lo), min(hi)
    return a[1] if b[0] == a[0] else a[1] + (b[1] - a[1]) * (30 - a[0]) / (b[0] - a[0])


def signal_days(rr: dict[int, float], thr: float, turn: float, mode: str, cooldown: int = 14) -> list[int]:
    """Copy of `events` in scripts/explore_rr25_bottom_btc.py (that script runs at import)."""
    out, in_ep, mn, last = [], False, 0.0, -(10**15)
    for d in sorted(rr):
        v = rr[d]
        if not in_ep and v <= thr and d - last >= cooldown * DAY_MS:
            in_ep, mn = True, v
            if mode == "touch":
                out.append(d)
                last, in_ep = d, False
                continue
        if in_ep:
            mn = min(mn, v)
            if v >= mn + turn:
                out.append(d)
                last, in_ep = d, False
    return out


def samples(D: dict[str, Any], t0: float) -> tuple[dict[str, list[int]], dict[str, Any]]:
    rr = {d: v for d, s in D["days"].items() if (v := rr30(s)) is not None}
    log(f"rr30 on {len(rr)} days", t0)
    last_entry = LAST_EXPIRY_MS - 61 * DAY_MS
    S: dict[str, list[int]] = {
        "S1": [d for d in signal_days(rr, -0.06, 0.01, "turn") if FIRST <= d <= last_entry],
        "S2": [d for d in signal_days(rr, -0.06, 0.01, "touch") if FIRST <= d <= last_entry],
    }
    info: dict[str, Any] = {
        "S1_all": len(signal_days(rr, -0.06, 0.01, "turn")),
        "S2_all": len(signal_days(rr, -0.06, 0.01, "touch")),
    }
    sats = [d for d in vp3.entry_days(D, 5) if d <= last_entry]
    S["S3"] = sats
    ev = D["events"]["all"]
    S4, S5, S6, why = [], [], [], Counter()
    diffs: dict[int, float] = {}
    for d in sats:
        s = D["days"].get(d)
        if s is None:
            why["no surface"] += 1
            continue
        fm = front_month_diff(s)
        has_ev = any(0 <= e - s["t_ms"] < 8 * DAY_MS for e in ev)
        if inversion_at(s, ev)["met"] is True:
            S5.append(d)
        if fm["diff"] is None:
            why[fm["state"]] += 1
            continue
        diffs[d] = fm["diff"]
        if fm["diff"] >= INVERSION:
            if not has_ev:
                S4.append(d)
        else:
            S6.append(d)
    S["S4"], S["S5"], S["S6"] = S4, S5, S6
    info.update({"sat": len(sats), "unmeasured": dict(why), "measured": len(diffs), "diffs": diffs})
    return S, info


# ----------------------------------------------------------------------------- positions


def delivery_fn(D: dict[str, Any]):
    return lambda e: D["delivery"].get(ymd_ms(e))


def positions(D: dict[str, Any], days: list[int], qty: float, P: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], Counter[str]]:
    out, why = {}, Counter()
    dl = delivery_fn(D)
    for d in days:
        r, w = run_position(D["days"], dl, d, qty, P)
        if r is None:
            why[w] += 1
        else:
            out[d] = r
    return out, why


def coverage(L: list[str], D: dict[str, Any], S: dict[str, list[int]], info: dict[str, Any], t0: float) -> dict[str, float]:
    L.append("## Етап 0: покриття (без P&L)\n")
    L.append(f"- Сигнали RR30 (усі дні кешу): turn {info['S1_all']}, touch {info['S2_all']} (звірка з розвідкою 15.11: 40 / 44).")
    L.append(
        f"- Суботи до {ymd_ms(LAST_EXPIRY_MS - 61 * DAY_MS)}: {info['sat']}; інверсію виміряно на {info['measured']}; не виміряно: {info['unmeasured']}."
    )
    L.append("")
    L.append("| вибірка | днів | diag 1:1 | diag 0.5 | голий | покриття 1:1 | причини пропуску (1:1) |")
    L.append("|---|---|---|---|---|---|---|")
    cov = {}
    for k, days in S.items():
        got = {}
        for v, q in VARIANTS.items():
            got[v] = positions(D, days, q, BASEP)
        n = len(days)
        c = len(got["diag1"][0]) / n if n else 0.0
        cov[k] = c
        L.append(
            f"| {k} | {n} | {len(got['diag1'][0])} | {len(got['diag05'][0])} | {len(got['naked'][0])} | {c:.0%} | "
            + ", ".join(f"{w} {m}" for w, m in got["diag1"][1].most_common())
            + " |"
        )
        log(f"coverage {k}: {len(got['diag1'][0])}/{n}", t0)
    L.append("")
    return cov


def sanity(L: list[str], D: dict[str, Any], S: dict[str, list[int]]) -> None:
    """Entry-side checks only (no outcome): debit and credit levels, delivery vs 09:00 spot, strike distances."""
    L.append("## Перевірки даних (лише вхід, без P&L)\n")
    dbt, cr, kl, ks, ivl, ivs = [], [], [], [], [], []
    for d in S["S3"]:
        s = D["days"].get(d)
        p, _ = plan_entry(s, 1.0, BASEP) if s else (None, "")
        if p is None:
            continue
        long_cost = p["debit"] + p["credit0"]
        dbt.append(p["debit"] / p["spot"])
        cr.append(p["credit0"] / long_cost)
        kl.append(p["long"]["K"] / p["spot"] - 1)
        ks.append(p["short"]["K"] / p["spot"] - 1)
        ivl.append(p["long"]["iv"])
        ivs.append(p["short"]["iv"])
    q = vp10._q
    L.append(f"- S3, n {len(dbt)}: чистий дебет / спот {q(dbt, '{:.2%}')}; кредит першої короткої / ціна довгої {q(cr, '{:.1%}')}.")
    L.append(
        f"- Страйк довгої над спотом {q(kl, '{:+.2%}')}; короткої {q(ks, '{:+.2%}')}; IV довгої {q(ivl, '{:.1%}')}, короткої {q(ivs, '{:.1%}')}."
    )
    L.append("- Угода автора 15.11: дебет / спот 4.8 %, кредит / довга 7.9 %, довга +0.6 % над спотом, коротка +5.1 %.")
    dd = []
    for ds, dp in D["delivery"].items():
        day = int(datetime.fromisoformat(ds).replace(tzinfo=UTC).timestamp() * 1000)
        s = D["days"].get(day)
        if s is not None:
            dd.append(abs(s["spot"] / dp - 1))
    L.append(f"- |спот 09:00 / ціна поставки 08:00 − 1|: {q(dd, '{:.2%}')} (n {len(dd)}).")
    L.append("")


# ----------------------------------------------------------------------------- statistics


def blocks(days: list[int]) -> dict[int, int]:
    return {d: (d - FIRST) // (BLOCK_WEEKS * 7 * DAY_MS) for d in days}


def stats(tr: dict[int, dict[str, Any]], key: str = "r") -> dict[str, Any]:
    ds = sorted(tr)
    if not ds:
        return {"n": 0}
    xs = [tr[d][key] for d in ds]
    cb = cluster_bootstrap(xs, [blocks(ds)[d] for d in ds], n_boot=N_BOOT, seed=SEED)
    is_ = [tr[d][key] for d in ds if d < OOS]
    oos = [tr[d][key] for d in ds if d >= OOS]
    top = sorted(xs, reverse=True)
    return {
        "n": len(xs),
        "k": cb["n_clusters"],
        "mean": statistics.fmean(xs),
        "ci": cb["ci"],
        "p": cb["p"],
        "pos": sum(x > 0 for x in xs) / len(xs),
        "worst": min(xs),
        "best": max(xs),
        "is": (statistics.fmean(is_) if is_ else float("nan"), len(is_)),
        "oos": (statistics.fmean(oos) if oos else float("nan"), len(oos)),
        "no_top3": statistics.fmean(top[3:]) if len(top) > 3 else float("nan"),
    }


def row(label: str, st: dict[str, Any]) -> str:
    if st["n"] == 0:
        return f"| {label} | 0 | — | — | — | — | — | — | — | — |"
    rr, ci = vp3.rr, vp3.ci_r
    return (
        f"| {label} | {st['n']} ({st['k']}) | {rr(st['mean'])} | {ci(st['ci'])} | {st['p']:.3f} | {st['pos']:.0%} | {rr(st['worst'])} | "
        f"{rr(st['is'][0])} ({st['is'][1]}) | {rr(st['oos'][0])} ({st['oos'][1]}) | {rr(st['no_top3'])} |"
    )


HDR = [
    "| вибірка / варіант | n (кластерів) | E[R] | 95 % CI | p | частка > 0 | найгірша | 2021–23 (n) | 2024–26 (n) | без топ-3 |",
    "|---|---|---|---|---|---|---|---|---|---|",
]


def paired(a: dict[int, dict[str, Any]], b: dict[int, dict[str, Any]], key: str = "r") -> dict[int, dict[str, Any]]:
    return {d: {key: a[d][key] - b[d][key]} for d in a if d in b}


def verdict(st: dict[str, Any], cov: float, p_holm: float) -> str:
    if cov < COVERAGE_MIN or st["n"] < N_MIN:
        return "◯"
    lo = st["ci"][0] if st["ci"] else float("nan")
    if lo > 0 and p_holm < 0.05 and st["oos"][0] > 0:
        return "✓"
    if st["mean"] > 0 and st["is"][0] > 0 and st["oos"][0] > 0:
        return "≈"
    return "✗"


def full(L: list[str], D: dict[str, Any], S: dict[str, list[int]], cov: dict[str, float], cache: Path, t0: float) -> None:
    T: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for k, days in S.items():
        for v, q in VARIANTS.items():
            T[(k, v)] = positions(D, days, q, BASEP)[0]
    log("positions done", t0)
    with (cache / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sample", "variant", "day", "spot", "K_long", "debit", "pnl", "r", "rolls", "skipped", "itm", "exit_spot"])
        for (k, v), tr in T.items():
            for d, t in sorted(tr.items()):
                w.writerow(
                    [k, v, ymd_ms(d), round(t["spot"], 1), t["K_long"], round(t["debit"], 2), round(t["pnl"], 2), round(t["r"], 4)]
                    + [t["rolls"], t["skipped"], t["itm"], round(t["exit_spot"], 1)]
                )

    L.append("## Результати: R на початковий дебет (з витратами Bybit, hs 1.5 п.)\n")
    L += HDR
    for k in S:
        for v in VARIANTS:
            L.append(row(f"{k} {v}", stats(T[(k, v)])))
    L.append("")
    L.append("P&L у частках спота на вхід (той самий порядок, `pnl / spot`, щоб порівнювати без різних знаменників):\n")
    L += HDR
    for k in ("S1", "S3"):
        for v in VARIANTS:
            L.append(row(f"{k} {v} [pnl/spot]", stats(T[(k, v)], "pnl_spot")))
    L.append("")

    # hypotheses
    h = {
        "H1": stats(T[("S1", "diag1")]),
        "H2a": stats(paired(T[("S1", "diag1")], T[("S1", "naked")])),
        "H2b": stats(paired(T[("S3", "diag1")], T[("S3", "naked")])),
    }
    a, b = T[("S4", "diag1")], T[("S6", "diag1")]
    vals = [a[d]["r"] for d in sorted(a)] + [b[d]["r"] for d in sorted(b)]
    cl = [("S4", blocks(sorted(a))[d]) for d in sorted(a)] + [("S6", blocks(sorted(b))[d]) for d in sorted(b)]
    h3 = cluster_bootstrap_diff(vals, cl, [True] * len(a) + [False] * len(b), n_boot=N_BOOT, seed=SEED) if a and b else None
    raw = {k: v["p"] for k, v in h.items()}
    if h3 is not None:
        raw["H3"] = h3["p"]
    adj = holm_adjust(raw)
    L.append("## Гіпотези (Holm по H1, H2a, H2b, H3)\n")
    L.append("| гіпотеза | n | оцінка | 95 % CI | p | p Holm | 2021–23 | 2024–26 | покриття | вердикт |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    covs = {"H1": cov["S1"], "H2a": cov["S1"], "H2b": cov["S3"]}
    labels = {"H1": "H1 diag 1:1 на S1, E[R] > 0", "H2a": "H2a diag − голий, S1", "H2b": "H2b diag − голий, S3"}
    for k, st in h.items():
        L.append(
            f"| {labels[k]} | {st['n']} | {vp3.rr(st['mean'])} | {vp3.ci_r(st['ci'])} | {st['p']:.3f} | {adj[k]:.3f} | "
            f"{vp3.rr(st['is'][0])} | {vp3.rr(st['oos'][0])} | {covs[k]:.0%} | {verdict(st, covs[k], adj[k])} |"
        )
    if h3 is not None:
        s4, s6 = stats(a), stats(b)
        c3 = min(cov["S4"], cov["S6"])
        d_is, d_oos = s4["is"][0] - s6["is"][0], s4["oos"][0] - s6["oos"][0]
        st3 = {"n": min(len(a), len(b)), "ci": h3["ci"], "mean": h3["diff"], "is": (d_is, 0), "oos": (d_oos, 0)}
        L.append(
            f"| H3 S4 − S6 (diag 1:1) | {len(a)} / {len(b)} | {vp3.rr(h3['diff'])} | {vp3.ci_r(h3['ci'])} | {h3['p']:.3f} | {adj['H3']:.3f} | "
            f"{vp3.rr(d_is)} | {vp3.rr(d_oos)} | {c3:.0%} | {verdict(st3, c3, adj['H3'])} |"
        )
    L.append("")

    # S1 trade list
    L.append("## S1 (turn): усі угоди\n")
    L.append("| дата | спот | дебет / спот | R diag 1:1 | R diag 0.5 | R голий | роли (пропущено) | коротка ITM | спот на виході |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for d in sorted(set(T[("S1", "diag1")]) | set(T[("S1", "naked")])):
        t1, t5, t0_ = T[("S1", "diag1")].get(d), T[("S1", "diag05")].get(d), T[("S1", "naked")].get(d)
        base = t1 or t0_
        L.append(
            f"| {ymd_ms(d)} | {base['spot']:,.0f} | {base['debit'] / base['spot']:.2%} | {vp3.rr(t1['r'] if t1 else None)} | {vp3.rr(t5['r'] if t5 else None)} | "
            f"{vp3.rr(t0_['r'] if t0_ else None)} | {t1['rolls'] if t1 else '—'} ({t1['skipped'] if t1 else '—'}) | {t1['itm'] if t1 else '—'} | {base['exit_spot']:,.0f} |"
        )
    L.append("")

    # robustness (exploratory: chosen before results, but every variant is reported)
    L.append("## Стійкість (розвідка, усі варіанти)\n")
    L += HDR
    for lab, kw in (
        ("hs 0", {"hs": 0.0}),
        ("hs 3 п.", {"hs": 0.03}),
        ("коротка 0.10Δ", {"short_delta": 0.10}),
        ("коротка 0.25Δ", {"short_delta": 0.25}),
    ):
        P = {**BASEP, **kw}
        for k in ("S1", "S3"):
            for v in ("diag1", "naked"):
                if v == "naked" and "short_delta" in kw:
                    continue
                L.append(row(f"{k} {v} {lab}", stats(positions(D, S[k], VARIANTS[v], P)[0])))
        log(f"robustness {lab} done", t0)
    L.append("")
    L.append("Diag 1:1 по роках (S3):\n")
    L.append("| рік | n | E[R] diag 1:1 | E[R] голий | частка > 0 diag |")
    L.append("|---|---|---|---|---|")
    for y in range(2021, 2027):
        sel = [t for d, t in T[("S3", "diag1")].items() if ymd_ms(d).startswith(str(y))]
        nk = [t["r"] for d, t in T[("S3", "naked")].items() if ymd_ms(d).startswith(str(y))]
        if sel:
            L.append(
                f"| {y} | {len(sel)} | {statistics.fmean(t['r'] for t in sel):+.3f} | {statistics.fmean(nk) if nk else float('nan'):+.3f} | {sum(t['r'] > 0 for t in sel) / len(sel):.0%} |"
            )
    L.append("")
    itm = [t["itm"] for t in T[("S3", "diag1")].values()]
    rolls = [t["rolls"] for t in T[("S3", "diag1")].values()]
    skp = [t["skipped"] for t in T[("S3", "diag1")].values()]
    L.append(
        f"- S3 diag 1:1: роли на позицію {statistics.fmean(rolls):.2f}, пропущені {statistics.fmean(skp):.2f}, коротких ITM на позицію {statistics.fmean(itm):.2f}."
    )
    L.append("")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/datahawks_diagonal")
    ap.add_argument("--strikes", choices=("fresh", "grid"), default="fresh")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.strikes == "grid":
        BASEP["grid"] = 1000.0
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser() / a.strikes
    cache.mkdir(parents=True, exist_ok=True)
    D = vp10.load(Path(a.src_dir).expanduser(), t0)
    S, info = samples(D, t0)
    L = [f"# DataHawks діагональ BTC на історії (stage {a.stage}, страйки {a.strikes}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    cov = coverage(L, D, S, info, t0)
    sanity(L, D, S)
    if a.stage == "full":
        full(L, D, S, cov, cache, t0)
    text = "\n".join(L)
    print(text)
    (cache / f"{a.stage}.md").write_text(text + "\n")
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
