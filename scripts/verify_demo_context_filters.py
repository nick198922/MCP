#!/usr/bin/env python3
"""IDEA-20261023-demo-context-filters: context filters for the demo trader's setups on history.

The entries are the trades of ``verify_demo_stop_management.py`` (cache ``--trades-cache``/trades, exit B = break-even
at half way, as live). Every filter is applied to the same entries (paired), causally (bars closed at or before the
signal bar close only), see ``analytics.demo_context_filters``:

- H1: skip an alt long (short) when BTC made a new N-bar low (high) on the same timeframe on the signal bar or the bar
  before it.
- H2: at most CAP same-direction entries decided at one bar close, ranked by the live score, ties by reward/risk.
- H3: an S3 sweep preceded by another S3 sweep event of the same side and timeframe within M bars ("repeat") vs first.
- H4: the live score (``setup_signals.score``) >= 2 vs < 2, and E[R] by score 0..4.

No network: 1h bars from the step-1 cache (``--s1-cache``), listing from ``--data-dir``. Stages: ``features`` (per-
symbol feature files, no outcomes), ``count`` (group sizes and the MDE before any outcome is read), ``report``.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders)

from tradedesk.analytics.course_basics import range_events  # noqa: E402
from tradedesk.analytics.course_s14_ema import atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.demo_context_filters import (  # noqa: E402
    bar_at_close,
    boot_p_two_sided,
    btc_block_flags,
    cap_per_group,
    cluster_boot_group_sums,
    cluster_sizes,
    daily_sums,
    last_closed,
    repeat_flags,
    score_points,
    trend_dirs,
)
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q, max_drawdown  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only pure function, the guard's R:R)

SEED = 20261024
N_BOOT = 10_000
DAY_MS = 86_400_000
TFS = {"4h": 4, "1d": 24}
# pre-registered (card, "Попередня реєстрація")
N_H1, LAG_H1, CAP_H2, M_H3, MIN_SCORE = 6, 1, 2, 30, 2
N_GRID, LAG_GRID, CAP_GRID, M_GRID = (3, 6, 12, 24), (0, 1), (1, 2, 3, 5), (10, 20, 30, 60)
EXIT = "B"
BTC, ETH = "BTCUSDT", "ETHUSDT"
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ---------------------------------------------------------------- features (no outcomes)

KEYS = ("sym", "setup", "tf", "side", "entry_ms", "day", "oos", "entry", "stop_pct", "target_r")


def real_levels(side: str, entry: float, stop_pct: float, target_r: float) -> tuple[float, float]:
    if side == "long":
        st = entry * (1 - stop_pct)
        return st, entry + target_r * (entry - st)
    st = entry * (1 + stop_pct)
    return st, entry - target_r * (st - entry)


def sym_features(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, trades_cache = args
    t0 = time.time()
    rows = json.loads((Path(trades_cache) / "trades" / f"{s}.json").read_text())["rows"]
    h1 = s1.load_1h(Path(s1_cache), Path(data_dir), s)
    daily = resample(h1, 24)
    td = trend_dirs(daily)
    # S3 sweep events per (tf, side): close time -> repeat flags; dev events for the exploratory variant
    rep: dict[tuple[str, str], dict[int, dict[str, bool]]] = {}
    for tf, hours in TFS.items():
        tb = resample(h1, hours)
        for side in ("long", "short"):
            bb = tb if side == "long" else tb.mirrored()
            pos = segment_pos(bb.t)
            evs = range_events(bb, pos, atr(bb, 14, pos), w=30, width_atr=6.0)
            sw = sorted({e.r for e in evs if e.kind == "sweep"})
            allr = sorted({e.r for e in evs})
            d: dict[int, dict[str, bool]] = {}
            flags = {m: repeat_flags(sw, m) for m in M_GRID}
            for k, r in enumerate(sw):
                ct = bb.close_time(r)
                f = {f"rep{m}": bool(flags[m][k]) for m in M_GRID}
                j = int(np.searchsorted(allr, r))  # any detector event (sweep or deviation) in [r - M, r - 1]
                f["rep_any"] = bool(j > 0 and allr[j - 1] >= r - M_H3)
                d[ct] = f
            rep[(tf, side)] = d
    out = []
    miss = 0
    for r in rows:
        x = {k: r[k] for k in KEYS}
        st, tg = real_levels(r["side"], r["entry"], r["stop_pct"], r["target_r"])
        x["rr"] = reward_risk_at(r["side"], r["entry"], st, tg)
        i = last_closed(daily, r["entry_ms"])
        x["coin_trend"] = int(td[i]) if i >= 0 else 0
        if r["setup"] == "S3":
            f = rep[(r["tf"], r["side"])].get(r["entry_ms"])
            if f is None:
                miss += 1
            else:
                x.update(f)
        out.append(x)
    return {"sym": s, "rows": out, "miss": miss, "sec": time.time() - t0, "rss": rss_mb()}


def btc_features(s1_cache: Path, data_dir: Path) -> dict[str, Any]:
    h1 = s1.load_1h(s1_cache, data_dir, BTC)
    out: dict[str, Any] = {"bars": {}, "daily_trend": None}
    for tf, hours in TFS.items():
        b = resample(h1, hours)
        fl = {f"{n}_{lag}": btc_block_flags(b, n, lag) for n in N_GRID for lag in LAG_GRID}
        out["bars"][tf] = (b, fl)
    daily = out["bars"]["1d"][0]
    out["daily"] = daily
    out["daily_trend"] = trend_dirs(daily)
    return out


def stage_features(a: argparse.Namespace) -> None:
    syms = sorted(p.stem for p in (a.trades_cache / "trades").glob("*.json"))
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(a.trades_cache)) for s in syms]
    t0 = time.time()
    allrows: list[dict[str, Any]] = []
    miss = 0
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(sym_features, jobs), 1):
            allrows += r["rows"]
            miss += r["miss"]
            if k % 50 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, rows {len(allrows)}, worker rss {r['rss']:.0f} MB")
    bt = btc_features(a.s1_cache, a.data_dir)
    nomatch: Counter[str] = Counter()
    for x in allrows:
        b, fl = bt["bars"][x["tf"]]
        i = bar_at_close(b, x["entry_ms"])
        if i < 0:
            nomatch[x["tf"]] += 1
        for key, (bl, bs) in fl.items():
            arr = bl if x["side"] == "long" else bs
            x[f"btc_{key}"] = bool(arr[i]) if i >= 0 else False
        j = last_closed(bt["daily"], x["entry_ms"])
        x["btc_trend"] = int(bt["daily_trend"][j]) if j >= 0 else 0
        x["score"] = score_points(x["coin_trend"], x["btc_trend"], x["side"], x["rr"], x["sym"] == BTC)
    allrows.sort(key=lambda x: (x["entry_ms"], x["sym"], x["setup"], x["tf"], x["side"]))
    (a.cache_dir / "features.json").write_text(json.dumps(allrows))
    emit(f"features: {len(allrows)} rows; S3 without a matched sweep event {miss}; BTC bar missing {dict(nomatch)}; rss {rss_mb():.0f} MB")
    b4 = bt["bars"]["4h"][0]
    for d in ("2021-11-09T00:00", "2022-11-21T00:00", "2024-03-14T00:00"):
        i = last_closed(bt["daily"], s1.ms(d))
        emit(f"BTC 1d close before {d}: {bt['daily'].c[i]:.0f}; 4h bars {len(b4)}")


# ---------------------------------------------------------------- loading and the filter groups


def load(a: argparse.Namespace, outcomes: bool) -> dict[str, np.ndarray]:
    F = json.loads((a.cache_dir / "features.json").read_text())
    cols: dict[str, list[Any]] = {}
    for x in F:
        for k, v in x.items():
            cols.setdefault(k, []).append(v)
    n = len(F)
    T = {k: np.asarray(v) for k, v in cols.items() if len(v) == n}
    for k in [f"rep{m}" for m in M_GRID] + ["rep_any"]:  # only S3 rows carry them
        T[k] = np.asarray([bool(x.get(k, False)) for x in F])
    T["s3ok"] = np.asarray([("rep30" in x) for x in F])
    if outcomes:
        R: dict[tuple[Any, ...], tuple[float, float]] = {}
        for p in sorted((a.trades_cache / "trades").glob("*.json")):
            for r in json.loads(p.read_text())["rows"]:
                R[(r["sym"], r["setup"], r["tf"], r["side"], r["entry_ms"])] = (r[f"{EXIT}_r"], r[f"{EXIT}_c"])
        key = zip(T["sym"], T["setup"], T["tf"], T["side"], T["entry_ms"].tolist(), strict=True)
        rc = np.asarray([R[k] for k in key])
        T["r"], T["c"] = rc[:, 0], rc[:, 1]
    T["year"] = (T["entry_ms"] // DAY_MS).astype("datetime64[D]").astype("datetime64[Y]").astype(int) + 1970
    return T


def h1_groups(T: dict[str, np.ndarray], n: int = N_H1, lag: int = LAG_H1) -> np.ndarray:
    alt = ~np.isin(T["sym"], [BTC, ETH])
    blocked = T[f"btc_{n}_{lag}"].astype(bool)
    return np.where(alt, np.where(blocked, 1, 0), -1)


def h3_groups(T: dict[str, np.ndarray], key: str = f"rep{M_H3}") -> np.ndarray:
    ok = (T["setup"] == "S3") & T["s3ok"]
    return np.where(ok, np.where(T[key], 1, 0), -1)


def h4_groups(T: dict[str, np.ndarray]) -> np.ndarray:
    return np.where(T["score"] >= MIN_SCORE, 0, 1)


def h2_keep(T: dict[str, np.ndarray], cap: int = CAP_H2) -> np.ndarray:
    keys = list(zip(T["entry_ms"].tolist(), T["side"].tolist(), strict=True))
    return cap_per_group(keys, T["score"].tolist(), T["rr"].tolist(), cap)


def h2_groups(T: dict[str, np.ndarray], cap: int = CAP_H2) -> np.ndarray:
    return np.where(h2_keep(T, cap), 0, 1)


# ---------------------------------------------------------------- stats


def contrast(v: np.ndarray, g: np.ndarray, day: np.ndarray, m: np.ndarray, seed: int = SEED) -> dict[str, Any]:
    """Kept (0) vs rejected (1) inside mask m: means, the kept set vs all, the difference with a date bootstrap."""
    v, g, day = v[m], g[m], day[m]
    sel = g >= 0
    n0, n1 = int(np.sum(g == 0)), int(np.sum(g == 1))
    res: dict[str, Any] = {"n0": n0, "n1": n1, "d": int(np.unique(day[sel]).size)}
    res["m0"] = float(v[g == 0].mean()) if n0 else math.nan
    res["m1"] = float(v[g == 1].mean()) if n1 else math.nan
    res["mall"] = float(v[sel].mean()) if sel.any() else math.nan
    res["diff"] = res["m0"] - res["m1"]
    if n0 < 2 or n1 < 2:
        res.update(lo=math.nan, hi=math.nan, p=math.nan, se=math.nan, k_lo=math.nan, k_hi=math.nan, ka=math.nan)
        return res
    s, c = cluster_boot_group_sums(v, g, day, 2, n_boot=N_BOOT, seed=seed)
    with np.errstate(invalid="ignore", divide="ignore"):
        b0, b1 = s[:, 0] / c[:, 0], s[:, 1] / c[:, 1]
        ball = (s[:, 0] + s[:, 1]) / (c[:, 0] + c[:, 1])
    bd = b0 - b1
    ok = np.isfinite(bd)
    res["lo"], res["hi"] = (float(x) for x in np.quantile(bd[ok], [0.025, 0.975]))
    res["p"] = boot_p_two_sided(bd[ok])
    res["se"] = float(bd[ok].std())
    res["k_lo"], res["k_hi"] = (float(x) for x in np.quantile(b0[np.isfinite(b0)], [0.025, 0.975]))
    ka = b0 - ball
    res["ka"] = float(res["m0"] - res["mall"])
    res["ka_lo"], res["ka_hi"] = (float(x) for x in np.quantile(ka[np.isfinite(ka)], [0.025, 0.975]))
    return res


def fd(x: dict[str, Any]) -> str:
    return f"{x['diff']:+.3f} [{x['lo']:+.3f}; {x['hi']:+.3f}]"


def mean_ci(v: np.ndarray, day: np.ndarray, seed: int = SEED) -> str:
    if v.size < 2:
        return f"{v.mean() if v.size else math.nan:+.3f} (n={v.size})"
    s, c = cluster_boot_group_sums(v, np.zeros(v.size, dtype=np.int64), day, 1, n_boot=N_BOOT, seed=seed)
    b = s[:, 0] / c[:, 0]
    b = b[np.isfinite(b)]
    lo, hi = np.quantile(b, [0.025, 0.975])
    return f"{v.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]"


def dd_daily(v: np.ndarray, day: np.ndarray) -> float:
    _, s = daily_sums(v, day)
    return max_drawdown(s)


# ---------------------------------------------------------------- count stage (no outcomes)


def stage_count(a: argparse.Namespace) -> None:
    T = load(a, outcomes=False)
    IS = ~T["oos"].astype(bool)
    emit(f"rows {T['sym'].size}; IS {int(IS.sum())} / OOS {int((~IS).sum())}; dates {np.unique(T['day']).size}")
    emit("Assumed SD of R per trade 1.2 (card); design effect 1 + (m - 1) rho, m = sum n_d^2 / sum n_d of the dates in the test")
    emit(
        "| гіпотеза | IS залишені | IS відкинуті | частка відкинутих | дат | m | MDE ρ=0 | MDE ρ=0.05 | MDE ρ=0.15 | OOS залишені / відкинуті |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|")
    for name, g in (
        ("H1 BTC N=6", h1_groups(T)),
        ("H2 кап 2", h2_groups(T)),
        ("H3 повтор M=30", h3_groups(T)),
        ("H4 бал ≥ 2", h4_groups(T)),
    ):
        gi = g[IS]
        n0, n1 = int(np.sum(gi == 0)), int(np.sum(gi == 1))
        days = T["day"][IS][gi >= 0]
        _, cnt = np.unique(days, return_counts=True)
        mbar = float(np.sum(cnt**2) / np.sum(cnt))
        base = 2.8 * 1.2 * math.sqrt(1 / max(n0, 1) + 1 / max(n1, 1))
        mde = [base * math.sqrt(1 + (mbar - 1) * rho) for rho in (0.0, 0.05, 0.15)]
        go = g[~IS]
        emit(
            f"| {name} | {n0} | {n1} | {n1 / max(n0 + n1, 1):.1%} | {cnt.size} | {mbar:.1f} | "
            + " | ".join(f"{x:.3f}" for x in mde)
            + f" | {int(np.sum(go == 0))} / {int(np.sum(go == 1))} |"
        )
    sz = cluster_sizes(list(zip(T["entry_ms"].tolist(), T["side"].tolist(), strict=True)))
    emit("cluster size (same side, same bar close), trades: " + ", ".join(f"{lab} {int(np.sum(m))}" for lab, m in size_buckets(sz)))
    emit("score 0..4, trades: " + ", ".join(f"{k}: {int(np.sum(T['score'] == k))}" for k in range(5)))
    emit(
        "H1 grid, share of alt trades blocked: "
        + ", ".join(
            f"N={n} lag={lag}: {np.mean(T[f'btc_{n}_{lag}'][~np.isin(T['sym'], [BTC, ETH])]):.1%}" for n in N_GRID for lag in LAG_GRID
        )
    )


def size_buckets(sz: np.ndarray) -> list[tuple[str, np.ndarray]]:
    return [("1", sz == 1), ("2–3", (sz >= 2) & (sz <= 3)), ("4–9", (sz >= 4) & (sz <= 9)), ("≥10", sz >= 10)]


# ---------------------------------------------------------------- report


def stage_report(a: argparse.Namespace) -> None:
    T = load(a, outcomes=True)
    r, day = T["r"], T["day"]
    IS = ~T["oos"].astype(bool)
    OOS = ~IS
    emit(f"trades {r.size}; symbols {np.unique(T['sym']).size}; dates {np.unique(day).size}; rss {rss_mb():.0f} MB")
    emit(f"sanity: E[R] {EXIT} IS {mean_ci(r[IS], day[IS])} (stop study: -0.046 [-0.077; -0.016]); OOS {mean_ci(r[OOS], day[OOS])}")
    emit(f"SD of R per trade (IS): {r[IS].std():.3f}")

    G = {"H1": h1_groups(T), "H2": h2_groups(T), "H3": h3_groups(T), "H4": h4_groups(T)}
    names = {
        "H1": "H1 BTC: альт-угоди, BTC не оновив N=6 екстремум проти угоди (залишені) vs оновив (відкинуті)",
        "H2": "H2 кап 2 на бар: залишені капом vs відкинуті капом",
        "H3": "H3 S3: перший свип (залишені) vs повторний за M=30 (відкинуті)",
        "H4": "H4 бал ≥ 2 (залишені) vs < 2 (відкинуті)",
    }
    res = {h: (contrast(r, g, day, IS, SEED + i), contrast(r, g, day, OOS, SEED + 10 + i)) for i, (h, g) in enumerate(G.items())}
    adj = holm([res[h][0]["p"] for h in G])
    emit("\n## Основна таблиця (IS для рішення, OOS для знаку), сім'я Holm {H1..H4}")
    emit(
        "| гіпотеза | IS n зал./відк. | дат | E[R] зал. | E[R] відк. | Δ зал.−відк. [95 % CI] | p | Holm p | MDE 80 % | OOS n зал./відк. | OOS Δ [CI] |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    for (h, (x, y)), pa in zip(res.items(), adj, strict=True):
        emit(
            f"| {h} | {x['n0']}/{x['n1']} | {x['d']} | {x['m0']:+.3f} | {x['m1']:+.3f} | {fd(x)} | {x['p']:.4f} | {pa:.4f} | {2.8 * x['se']:.3f} | "
            f"{y['n0']}/{y['n1']} | {fd(y)} |"
        )
    for h in G:
        emit(f"- {names[h]}")
    emit("\n### Набір після фільтра: E[R] залишених [CI] і різниця з усім набором (у межах області фільтра)")
    emit("| гіпотеза | IS усе | IS залишені | IS зал. − усе | OOS усе | OOS залишені | OOS зал. − усе |")
    emit("|---|---|---|---|---|---|---|")
    for h, (x, y) in res.items():
        emit(
            f"| {h} | {x['mall']:+.3f} | {x['m0']:+.3f} [{x['k_lo']:+.3f}; {x['k_hi']:+.3f}] | {x['ka']:+.3f} [{x['ka_lo']:+.3f}; {x['ka_hi']:+.3f}] | "
            f"{y['mall']:+.3f} | {y['m0']:+.3f} [{y['k_lo']:+.3f}; {y['k_hi']:+.3f}] | {y['ka']:+.3f} [{y['ka_lo']:+.3f}; {y['ka_hi']:+.3f}] |"
        )
    emit("\n### Правила рішення (попередня реєстрація)")
    for (h, (x, y)), pa in zip(res.items(), adj, strict=True):
        if h == "H2":
            continue
        conf = x["diff"] >= 0.05 and pa < 0.05 and y["diff"] > 0
        if conf:
            st = "confirmed"
        elif x["diff"] >= 0.05 and pa >= 0.05:
            st = "inconclusive (Δ ≥ 0.05, але не значуще після Holm)"
        else:
            st = "rejected"
        clean = "практичний ефект +0.05R виключено (CI hi < 0.05)" if x["hi"] < 0.05 else "CI ще містить +0.05R"
        emit(f"- {h}: {st}; IS Δ {x['diff']:+.3f}, Holm p {pa:.4f}, OOS Δ {y['diff']:+.3f}; {clean}")
    h2_report(T, IS, OOS)
    h4_levels(T, IS, OOS)
    slices(T, G, IS, OOS)
    sensitivity(T, IS, OOS)
    robustness(T, G, IS, OOS)
    for h in ("H1", "H3", "H4"):
        per_asset(T, G[h], IS, OOS, h)
    emit(f"\nrss {rss_mb():.0f} MB")


def h2_report(T: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    r, day = T["r"], T["day"]
    emit("\n## H2: кап однонапрямлених входів на бар")
    rng = np.random.default_rng(SEED)
    emit(
        "| кап | вибірка | n усе / залишено | E[R] усе | E[R] з капом | Δ на угоду (кап − усе) [CI] | макс. просадка усе, R | з капом, R | зменшення | випадкове проріджування: 5 % / медіана, R | частка проріджувань з меншою просадкою |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    decision = {}
    for cap in CAP_GRID:
        keep = h2_keep(T, cap)
        g = np.where(keep, 0, 1)
        for lab, m in (("IS", IS), ("OOS", OOS)):
            x = contrast(r, g, day, m, SEED + cap)
            dd_all = dd_daily(r[m], day[m])
            dd_cap = dd_daily(r[m & keep], day[m & keep])
            n_drop = int(np.sum(m & ~keep))
            idx = np.flatnonzero(m)
            null = []
            for _ in range(200):
                drop = rng.choice(idx, size=n_drop, replace=False)
                k2 = m.copy()
                k2[drop] = False
                null.append(dd_daily(r[k2], day[k2]))
            null_a = np.asarray(null)
            share = float(np.mean(null_a <= dd_cap))
            red = 1 - dd_cap / dd_all if dd_all > 0 else math.nan
            emit(
                f"| {cap} | {lab} | {int(m.sum())} / {int((m & keep).sum())} | {x['mall']:+.3f} | {x['m0']:+.3f} | {x['ka']:+.3f} [{x['ka_lo']:+.3f}; {x['ka_hi']:+.3f}] | "
                f"{dd_all:.1f} | {dd_cap:.1f} | {red:.1%} | {np.quantile(null_a, 0.05):.1f} / {np.median(null_a):.1f} | {share:.1%} |"
            )
            decision[(cap, lab)] = (red, x["ka"], dd_cap, float(np.quantile(null_a, 0.05)))
    red, ka, ddc, q5 = decision[(CAP_H2, "IS")]
    red_o = decision[(CAP_H2, "OOS")][0]
    conf = red >= 0.20 and ddc < q5 and ka >= -0.02 and red_o > 0
    why = []
    if red < 0.20:
        why.append("просадка зменшилась менш ніж на 20 %")
    if ddc >= q5:
        why.append("не краще за випадкове проріджування тієї ж кількості угод (5-й перцентиль)")
    if ka < -0.02:
        why.append("E[R] на угоду гірше більш ніж на 0.02R")
    if red_o <= 0:
        why.append("в OOS просадка не менша")
    emit(
        f"- H2 (кап {CAP_H2}): {'confirmed' if conf else 'rejected'}; IS зменшення {red:.1%}, ΔE[R] {ka:+.3f}; OOS зменшення {red_o:.1%}"
        + (f"; {'; '.join(why)}" if why else "")
    )
    sz = cluster_sizes(list(zip(T["entry_ms"].tolist(), T["side"].tolist(), strict=True)))
    emit("\n### E[R] на угоду за розміром кластера (однонапрямлені входи на одному барі), вторинне")
    emit("| розмір | IS n | IS E[R] [CI] | OOS n | OOS E[R] [CI] |\n|---|---|---|---|---|")
    for lab, mb in size_buckets(sz):
        emit(
            f"| {lab} | {int(np.sum(IS & mb))} | {mean_ci(r[IS & mb], day[IS & mb])} | {int(np.sum(OOS & mb))} | {mean_ci(r[OOS & mb], day[OOS & mb])} |"
        )
    big = sz >= 4
    for lab, m in (("IS", IS), ("OOS", OOS)):
        x = contrast(r, np.where(big, 1, 0), day, m, SEED + 77)
        emit(f"{lab}: кластер < 4 мінус ≥ 4: {fd(x)} (n {x['n0']}/{x['n1']})")


def h4_levels(T: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    r, day, sc = T["r"], T["day"], T["score"]
    emit("\n## H4: E[R] за живим балом 0..4")
    emit("| бал | IS n | IS E[R] [CI] | OOS n | OOS E[R] [CI] |\n|---|---|---|---|---|")
    for k in range(5):
        emit(
            f"| {k} | {int(np.sum(IS & (sc == k)))} | {mean_ci(r[IS & (sc == k)], day[IS & (sc == k)])} | {int(np.sum(OOS & (sc == k)))} | {mean_ci(r[OOS & (sc == k)], day[OOS & (sc == k)])} |"
        )
    for lab, m in (("IS", IS), ("OOS", OOS)):
        s, c = cluster_boot_group_sums(r[m], sc[m], day[m], 5, n_boot=N_BOOT, seed=SEED + 5)
        x = np.arange(5.0)
        tot_c = c.sum(axis=1, keepdims=True)
        xbar = (c * x).sum(axis=1, keepdims=True) / tot_c
        ybar = s.sum(axis=1, keepdims=True) / tot_c
        with np.errstate(invalid="ignore", divide="ignore"):
            mg = np.where(c > 0, s / np.where(c > 0, c, 1), 0.0)
        slope = ((c * (x - xbar) * (mg - ybar)).sum(axis=1)) / ((c * (x - xbar) ** 2).sum(axis=1))
        rv, sv = r[m], sc[m].astype(float)
        pt = float(np.cov(sv, rv, bias=True)[0, 1] / np.var(sv))
        lo, hi = np.quantile(slope, [0.025, 0.975])
        emit(f"{lab}: нахил E[R] на 1 пункт балу {pt:+.4f} [{lo:+.4f}; {hi:+.4f}], p {boot_p_two_sided(slope):.4f}")
    emit("Складові балу (IS, E[R] з пунктом vs без, усі угоди):")
    for lab, f in (
        ("тренд монети в бік угоди", T["coin_trend"] == np.where(T["side"] == "long", 1, -1)),
        ("тренд BTC в бік угоди (альти)", (T["btc_trend"] == np.where(T["side"] == "long", 1, -1)) & (T["sym"] != BTC)),
        ("R:R після комісій ≥ 2", T["rr"] >= 2),
    ):
        x = contrast(r, np.where(f, 0, 1), day, IS, SEED + 9)
        y = contrast(r, np.where(f, 0, 1), day, OOS, SEED + 9)
        emit(f"- {lab}: IS {fd(x)} (n {x['n0']}/{x['n1']}); OOS {fd(y)}")


def slices(T: dict[str, np.ndarray], G: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    r, day = T["r"], T["day"]
    emit("\n## Розрізи: Δ зал. − відк. (IS / OOS), без поправки")
    groups = [("S3", T["setup"] == "S3"), ("S16", T["setup"] == "S16"), ("S14", T["setup"] == "S14")]
    groups += [("long", T["side"] == "long"), ("short", T["side"] == "short"), ("4h", T["tf"] == "4h"), ("1d", T["tf"] == "1d")]
    groups += [("S3 long", (T["setup"] == "S3") & (T["side"] == "long")), ("S3 short", (T["setup"] == "S3") & (T["side"] == "short"))]
    emit("| зріз | " + " | ".join(f"{h} IS | {h} OOS" for h in ("H1", "H3", "H4")) + " |")
    emit("|---|" + "---|" * 6)
    for lab, gm in groups:
        cells = []
        for h in ("H1", "H3", "H4"):
            for m in (IS, OOS):
                x = contrast(r, G[h], day, m & gm, SEED + 3)
                cells.append(f"{fd(x)} ({x['n0']}/{x['n1']})" if x["n0"] >= 2 and x["n1"] >= 2 else "—")
        emit(f"| {lab} | " + " | ".join(cells) + " |")


def sensitivity(T: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    r, day = T["r"], T["day"]
    emit("\n## Чутливість (сітка зареєстрована заздалегідь; рішення лише за основними параметрами)")
    emit("| варіант | IS n зал./відк. | IS Δ [CI] | OOS Δ [CI] | IS E[R] зал. |\n|---|---|---|---|---|")
    for n in N_GRID:
        for lag in LAG_GRID:
            g = h1_groups(T, n, lag)
            x, y = contrast(r, g, day, IS, SEED + n), contrast(r, g, day, OOS, SEED + n)
            emit(f"| H1 N={n} lag={lag} | {x['n0']}/{x['n1']} | {fd(x)} | {fd(y)} | {x['m0']:+.3f} |")
    for key in [f"rep{m}" for m in M_GRID] + ["rep_any"]:
        g = h3_groups(T, key)
        x, y = contrast(r, g, day, IS, SEED + 4), contrast(r, g, day, OOS, SEED + 4)
        lab = "H3 M=30, попередній свип або девіація" if key == "rep_any" else f"H3 M={key[3:]}"
        emit(f"| {lab} | {x['n0']}/{x['n1']} | {fd(x)} | {fd(y)} | {x['m0']:+.3f} |")
    for k in (1, 3):
        g = np.where(T["score"] >= k, 0, 1)
        x, y = contrast(r, g, day, IS, SEED + 6), contrast(r, g, day, OOS, SEED + 6)
        emit(f"| H4 бал ≥ {k} | {x['n0']}/{x['n1']} | {fd(x)} | {fd(y)} | {x['m0']:+.3f} |")


def robustness(T: dict[str, np.ndarray], G: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    r, c, day = T["r"], T["c"], T["day"]
    emit("\n## Стійкість: Δ зал. − відк.")
    emit("| рядок | H1 | H3 | H4 |\n|---|---|---|---|")
    for lab, m in (("IS", IS), ("OOS", OOS)):
        emit(f"| {lab}: витрати ×2 | " + " | ".join(fd(contrast(r - c, G[h], day, m, SEED + 8)) for h in ("H1", "H3", "H4")) + " |")
        u, s = daily_sums(r[m], day[m])
        top = u[np.argsort(-np.abs(s))[:10]]
        keep = m & ~np.isin(day, top)
        emit(
            f"| {lab}: без 10 дат з найбільшою |ΣR| | "
            + " | ".join(fd(contrast(r, G[h], day, keep, SEED + 8)) for h in ("H1", "H3", "H4"))
            + " |"
        )
    for y in sorted(set(T["year"].tolist())):
        m = T["year"] == y
        cells = []
        for h in ("H1", "H3", "H4"):
            x = contrast(r, G[h], day, m, SEED + 8)
            cells.append(f"{fd(x)} ({x['n0']}/{x['n1']})" if x["n0"] >= 2 and x["n1"] >= 2 else "—")
        emit(f"| {y} | " + " | ".join(cells) + " |")


def per_asset(T: dict[str, np.ndarray], g: np.ndarray, IS: np.ndarray, OOS: np.ndarray, h: str) -> None:
    r, day, sym = T["r"], T["day"], T["sym"]
    rows = []
    for s in sorted(set(sym[g >= 0].tolist())):
        m = sym == s
        k, j = r[m & (g == 0)], r[m & (g == 1)]
        if k.size < 5 or j.size < 5:
            continue
        se = math.sqrt(k.var(ddof=1) / k.size + j.var(ddof=1) / j.size)
        ki, ji = r[m & IS & (g == 0)], r[m & IS & (g == 1)]
        dis = float(ki.mean() - ji.mean()) if ki.size >= 5 and ji.size >= 5 else math.nan
        rows.append((s, k.size, j.size, float(k.mean() - j.mean()), se, dis))
    q, df, i2 = cochran_q(np.array([x[3] for x in rows]), np.array([x[4] for x in rows]))
    emit(f"\n## За активами (5a), {h}: Δ зал. − відк. по символах з ≥ 5 угод у кожній групі")
    emit(f"Cochran Q={q:.1f}, df={df}, p={chi2_sf(q, df):.3f}, I²={i2:.1%}")
    emit("| символ | n зал. | n відк. | Δ | SE |\n|---|---|---|---|---|")
    for s, a, b, mu, se, _ in sorted(rows, key=lambda x: -(x[1] + x[2]))[:15]:
        emit(f"| {s} | {a} | {b} | {mu:+.3f} | {se:.3f} |")
    ok = sorted([x for x in rows if np.isfinite(x[5])], key=lambda x: -x[5])
    top = [x[0] for x in ok[: max(1, len(ok) // 4)]]
    mt = np.isin(sym, top)
    x = contrast(r, g, day, OOS & mt, SEED + 12)
    y = contrast(r, g, day, OOS & ~mt, SEED + 12)
    emit(
        f"Стійкість: верхня чверть за IS Δ ({len(top)} з {len(ok)} символів): OOS Δ {fd(x)} (n {x['n0']}/{x['n1']}); решта OOS Δ {fd(y)} (n {y['n0']}/{y['n1']})"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("features", "count", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/demo_context_filters")
    ap.add_argument("--trades-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/demo_stop_management")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        {"features": stage_features, "count": stage_count, "report": stage_report}[a.stage](a)
        emit(f"# done, rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
