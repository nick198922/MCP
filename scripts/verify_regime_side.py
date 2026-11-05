#!/usr/bin/env python3
"""Market regime as the allowed side, all demo setups (IDEA-20261030-demo-setups-depth, item «режим ринку задає
дозволену сторону»; registration «Попередня реєстрація — режим ринку задає сторону»).

    uv run python scripts/verify_regime_side.py --stage stage0   # counts, shares dropped, SD of R, MDE80 (no Delta)
    uv run python scripts/verify_regime_side.py --stage report   # the registered test (only after the registration commit)

No network: the trades are the passports' candidate files (``verify_setup_passport.py``, ``--passport-cache``) and the
S12 v4 rows of ``verify_s12_modules.py`` (``--s12-cache``); BTC spot daily closes and the cycle-regime inputs from the
``cycle_regime`` cache (``--regime-cache``), all filled earlier through the runtime providers. Output also goes to
``<out-dir>/<stage>.txt``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s12_modules as m12s  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402
from common_helpers import rss_mb  # noqa: E402

from tradedesk.analytics import cycle_regime as cr  # noqa: E402
from tradedesk.analytics import regime_side as rs  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.risk_overlay import holm  # noqa: E402

DEMO = ("S14", "S16", "S3", "S12", "S38", "S39", "S40", "S28", "S17")  # demo_trader.SETUPS (S12 = v4, S17 = 1d)
VARIANTS = ("SMA200", "CYCLE")  # registered, Holm on 2
DESCR = ("SMA365",)  # descriptive, outside Holm
SEED = 20261104
N_BOOT = 4000
N_PLACEBO = 2000
N_SHIFT = 1000
MIN_SHIFT_DAYS = 180
INTEREST = 0.05
YEAR_MS = 365.25 * rs.DAY_MS
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


# ---------------------------------------------------------------- data


def day_signs(a: argparse.Namespace) -> dict[str, dict[int, int]]:
    spot = cr.parse_closes(json.loads((a.regime_cache / "spot_1d.json").read_text())["rows"])
    states = vsp.regime_states(a.regime_cache)
    return {
        "SMA200": rs.sma_sign(spot, 200),
        "SMA365": rs.sma_sign(spot, 365),
        "CYCLE": {t: rs.cycle_sign(s) for t, s in states.items()},
    }


def month_of(t: np.ndarray) -> np.ndarray:
    return np.array([(d.year * 12 + d.month) for d in (datetime.fromtimestamp(x / 1000, UTC) for x in t)], dtype=np.int64)


def setup_rows(setup: str, a: argparse.Namespace, qv: dict[str, Any], tops: dict[int, dict[int, set[str]]]) -> list[dict[str, Any]]:
    """The setup's demo-list trades (the passport's verdict sample: demo selection, the demo's list, one trade per
    symbol, filled limits only) for 2021+ ('main') and 2020-02 .. 2020-12 ('y2020', its own chain), plus the masks the
    report needs to rebuild the chain with a filter before it. Returns compact arrays."""
    if setup == "S12":  # demo runs S12 v4 (entry at the touch bar's close): the s12_modules base
        T, _, _, _ = m12s.load(a.s12_cache / "S12")
        floor, n_top, cap, filled = 5e6, vsp.TOP_N, None, None
    else:
        vsp.OUT["tf"] = "1d" if setup == "S17" else None
        T = vsp.load_rows(a.passport_cache / setup)
        floor, n_top, cap = vsp.SETUPS[setup][2], vsp.top_n_of(setup), vsp.BAR_CAP.get(setup)
        filled = T["filled"] if setup in vsp.LIMIT else None
    if n_top not in tops:
        tops[n_top] = vsp.top_n_days(qv, n_top)
    base = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    base &= np.nan_to_num(T["turn"].astype(float), nan=-1) >= floor
    base &= vsp.in_prev_day_top(T, tops[n_top])
    out = []
    for lab, per in (("main", T["t_in"] >= vsp.PASSPORT_MS), ("y2020", (T["t_in"] >= vsp.START_MS) & (T["t_in"] < vsp.PASSPORT_MS))):
        V = vsp.chain(T, base & per, cap)
        if filled is not None:
            V &= filled
        out.append(
            {
                "setup": setup,
                "part": lab,
                "sym": T["sym"][V].astype(str),
                "side": T["side"][V].astype(str),
                "t_in": T["t_in"][V].astype(np.int64),
                "B": T["B"][V].astype(float),
                "B15": T["B15"][V].astype(float),
                "regime": T["regime"][V].astype(str) if "regime" in T else np.array(["n/a"] * int(V.sum())),
                "_T": T if lab == "main" else None,
                "_cand": base & per,
                "_cap": cap,
                "_filled": filled,
            }
        )
    return out


def assemble(parts: list[dict[str, Any]], signs: dict[str, dict[int, int]]) -> dict[str, np.ndarray]:
    keys = ("sym", "side", "t_in", "B", "B15", "regime")
    X = {k: np.concatenate([p[k] for p in parts]) for k in keys}
    X["g"] = np.concatenate([np.full(p["t_in"].size, DEMO.index(p["setup"]), dtype=np.int64) for p in parts])
    ok = np.isfinite(X["B"]) & np.isfinite(X["B15"])
    if not ok.all():
        emit(f"рядків без R відкинуто: {int((~ok).sum())}")
        X = {k: x[ok] for k, x in X.items()}
    X["month"] = month_of(X["t_in"])
    X["week"] = X["t_in"] // sp.WEEK_MS
    X["year"] = X["month"] // 12 - (X["month"] % 12 == 0)
    X["day"] = (X["t_in"] // rs.DAY_MS) * rs.DAY_MS - rs.DAY_MS
    for v, s in signs.items():
        X[v] = np.array([s.get(int(d), 0) for d in X["day"]], dtype=np.int64)
    return X


def load_all(a: argparse.Namespace) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, dict[int, int]], list[dict[str, Any]]]:
    signs = day_signs(a)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    tops: dict[int, dict[int, set[str]]] = {}
    main, y20 = [], []
    for s in DEMO:
        m, y = setup_rows(s, a, qv, tops)
        main.append(m)
        y20.append(y)
        emit(f"loaded {s}: 2021+ n {m['t_in'].size}, 2020 n {y['t_in'].size}, rss {rss_mb():.0f} MB")
    return assemble(main, signs), assemble(y20, signs), signs, main


# ---------------------------------------------------------------- stage 0 (no Delta, no mean R of a filter group)


def stage0(a: argparse.Namespace) -> None:
    X, Y, signs, _ = load_all(a)
    oos = X["t_in"] >= vsp.OOS_MS
    emit("\n## Вибірка (2021+, список демо, відбір демо, ланцюг паспорта; лімітки — заповнені)")
    emit("| сетап | n | IS | OOS | лонги | шорти | SD R (×1) | 2020 n |\n|---|---|---|---|---|---|---|---|")
    for k, s in enumerate(DEMO):
        m = X["g"] == k
        emit(
            f"| {s} | {int(m.sum())} | {int((m & ~oos).sum())} | {int((m & oos).sum())} | {int((m & (X['side'] == 'long')).sum())} | "
            f"{int((m & (X['side'] == 'short')).sum())} | {np.std(X['B'][m]):.3f} | {int((Y['g'] == k).sum())} |"
        )
    emit(
        f"| разом | {X['B'].size} | {int((~oos).sum())} | {int(oos.sum())} | {int((X['side'] == 'long').sum())} | {int((X['side'] == 'short').sum())} | {np.std(X['B']):.3f} | {Y['B'].size} |"
    )
    years = sorted(set(X["year"].tolist()))
    emit(
        "\nугод за роками (лонги / шорти): "
        + ", ".join(
            f"{y} {int(((X['year'] == y) & (X['side'] == 'long')).sum())} / {int(((X['year'] == y) & (X['side'] == 'short')).sum())}"
            for y in years
        )
    )
    # consistency: our cycle sign vs the passport row's own regime label
    has = X["regime"] != "n/a"  # the S12 v4 rows carry no regime label
    agree = np.array([rs.cycle_sign(r) for r in X["regime"][has]]) == X["CYCLE"][has]
    emit(
        f"звірка CYCLE з полем regime рядків паспорта (без S12 v4): збіг {agree.mean():.4f} ({int((~agree).sum())} розбіжностей з {agree.size})"
    )
    for v in ("SMA200", "SMA365"):
        ds = sorted(t for t in signs[v] if vsp.START_MS <= t < vsp.END["4h"])
        up = np.array([signs[v][t] for t in ds])
        emit(
            f"{v}: днів BTC вище {np.mean(up == 1):.3f}, нижче {np.mean(up == -1):.3f}, без знака {np.mean(up == 0):.3f} (2020-02 … 2026-09)"
        )
    emit("\n## Частка угод, яку відкидає варіант (лишає / відкидає / не діє)")
    emit("| варіант | вибірка | лишає | відкидає | не діє |\n|---|---|---|---|---|")
    for v in (*VARIANTS, *DESCR):
        kept, drop = rs.split(X["side"], X[v])
        for lab, m in (("2021+", np.ones_like(oos)), ("IS", ~oos), ("OOS", oos)):
            emit(f"| {v} | {lab} | {kept[m].mean():.3f} | {drop[m].mean():.3f} | {(~kept & ~drop)[m].mean():.3f} |")
        emit(f"| {v} | за роками (відкидає) | " + ", ".join(f"{y} {drop[X['year'] == y].mean():.2f}" for y in years) + " | | |")
        emit(f"| {v} | за сетапами (відкидає) | " + ", ".join(f"{s} {drop[X['g'] == k].mean():.2f}" for k, s in enumerate(DEMO)) + " | | |")
        emit(
            f"| {v} | за боками (відкидає) | лонги {drop[X['side'] == 'long'].mean():.2f}, шорти {drop[X['side'] == 'short'].mean():.2f} | | |"
        )
    emit("\n## MDE₈₀ (SE з бутстрепу на R, центрованому всередині кожної клітинки сетап × лишає / відкидає: Δ = 0 за побудовою)")
    emit(
        "| варіант | вибірка | пул | кластер | SE | MDE₈₀ α 0.05 | MDE₈₀ α 0.025 (Холм, найсуворіший крок) |\n|---|---|---|---|---|---|---|"
    )
    for v in (*VARIANTS, *DESCR):
        kept, drop = rs.split(X["side"], X[v])
        for lab, m in (("2021+", np.ones_like(oos)), ("IS", ~oos), ("OOS", oos)):
            for pool, cl in (("сетапи порівну", "month"), ("сетапи порівну", "week"), ("угоди", "month")):
                if lab != "2021+" and (pool, cl) != ("сетапи порівну", "month"):
                    continue
                r = rs.delta_boot(
                    X["g"][m],
                    X[cl][m],
                    kept[m],
                    drop[m],
                    X["B"][m],
                    pooled="groups" if pool != "угоди" else "trades",
                    n_boot=1000,
                    seed=SEED,
                    center=True,
                )
                se = r["pool"]["se"]
                emit(f"| {v} | {lab} | {pool} | {cl} | {se:.4f} | {rs.mde80(se):.3f} | {rs.mde80(se, 0.025):.3f} |")
        if v == "SMA200":
            r = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], n_boot=1000, seed=SEED, center=True)
            emit(
                "SE окремих сетапів (місяці), SMA200: "
                + ", ".join(f"{s} {p['se']:.3f} (n {int(p['n_k'])}/{int(p['n_d'])})" for s, p in zip(DEMO, r["per"], strict=True))
            )


# ---------------------------------------------------------------- report (the registered test; run only after the registration commit)


def f3(r: dict[str, float]) -> str:
    return f"{r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]"


def mean_ci(v: np.ndarray, cl: np.ndarray) -> dict[str, float]:
    return sp.mean_ci(v, cl, n_boot=N_BOOT, seed=SEED)


def placebo_drop(X: dict[str, np.ndarray], kept: np.ndarray, drop: np.ndarray, obs: float) -> dict[str, float]:
    rng = np.random.default_rng(SEED)
    el = kept | drop
    strata = np.array([f"{g}:{s}" for g, s in zip(X["g"], X["side"], strict=True)])
    vals = []
    for _ in range(N_PLACEBO):
        d = rs.random_drop(strata, drop, el, rng)
        vals.append(rs.delta_boot(X["g"], X["month"], el & ~d, d, X["B"], n_boot=1)["pool"]["m"])
    b = np.asarray(vals)
    return {"mean": float(np.nanmean(b)), "p": float((np.sum(b >= obs) + 1) / (b.size + 1))}


def placebo_shift(X: dict[str, np.ndarray], signs: dict[int, int], obs: float) -> dict[str, float]:
    rng = np.random.default_rng(SEED + 1)
    nz = sum(1 for s in signs.values() if s != 0)
    udays, inv = np.unique(X["day"], return_inverse=True)
    vals = []
    for _ in range(N_SHIFT):
        sh = rs.shift_signs(signs, int(rng.integers(MIN_SHIFT_DAYS, nz - MIN_SHIFT_DAYS)))
        rg = np.array([sh.get(int(d), 0) for d in udays], dtype=np.int64)[inv]
        k, d = rs.split(X["side"], rg)
        vals.append(rs.delta_boot(X["g"], X["month"], k, d, X["B"], n_boot=1)["pool"]["m"])
    b = np.asarray(vals)
    return {"mean": float(np.nanmean(b)), "p": float((np.sum(b >= obs) + 1) / (b.size + 1))}


def per_coin(
    X: dict[str, np.ndarray], kept: np.ndarray, drop: np.ndarray, m: np.ndarray, min_n: int = 10
) -> dict[str, tuple[float, float]]:
    out = {}
    for s in np.unique(X["sym"][m]):
        mk, md = m & kept & (X["sym"] == s), m & drop & (X["sym"] == s)
        if mk.sum() >= min_n and md.sum() >= min_n:
            d = X["B"][mk].mean() - X["B"][md].mean()
            se = math.sqrt(X["B"][mk].var(ddof=1) / mk.sum() + X["B"][md].var(ddof=1) / md.sum())
            out[str(s)] = (float(d), se)
    return out


def stage_report(a: argparse.Namespace) -> None:
    X, Y, signs, main = load_all(a)
    oos = X["t_in"] >= vsp.OOS_MS
    full = np.ones_like(oos)
    res: dict[str, dict[str, Any]] = {}
    for v in (*VARIANTS, *DESCR):
        kept, drop = rs.split(X["side"], X[v])
        r = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], n_boot=N_BOOT, seed=SEED)
        res[v] = {"all": r, "kept": kept, "drop": drop}
    adj = holm([res[v]["all"]["pool"]["p"] for v in VARIANTS])
    for v, p in zip(VARIANTS, adj, strict=True):
        res[v]["holm"] = float(p)
    years = sorted(set(X["year"].tolist()))
    for v in (*VARIANTS, *DESCR):
        kept, drop, r = res[v]["kept"], res[v]["drop"], res[v]["all"]
        tag = "основний" if v in VARIANTS else "описово"
        emit(f"\n## {v} ({tag}): Δ = E[R] лишених − E[R] відкинутих, сетапи порівну, CI кластерами за місяцем")
        emit(
            f"2021+: {f3(r['pool'])}, p {r['pool']['p']:.4f}"
            + (f", Холм {res[v]['holm']:.4f}" if v in VARIANTS else "")
            + f"; n лишає {int(r['pool']['n_k'])}, відкидає {int(r['pool']['n_d'])}"
        )
        for lab, m in (("IS", ~oos), ("OOS", oos)):
            rr = rs.delta_boot(X["g"][m], X["month"][m], kept[m], drop[m], X["B"][m], n_boot=N_BOOT, seed=SEED)
            emit(f"{lab}: {f3(rr['pool'])}")
            res[v][lab] = rr["pool"]
        wk = rs.delta_boot(X["g"], X["week"], kept, drop, X["B"], n_boot=N_BOOT, seed=SEED)
        tr = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], pooled="trades", n_boot=N_BOOT, seed=SEED)
        c15 = rs.delta_boot(X["g"], X["month"], kept, drop, X["B15"], n_boot=N_BOOT, seed=SEED)
        emit(f"чутливість: тижневі кластери {f3(wk['pool'])}; угоди разом {f3(tr['pool'])}; витрати ×1.5 {f3(c15['pool'])}")
        yr = {}
        for y in years:
            m = X["year"] == y
            yr[y] = rs.delta_boot(X["g"][m], X["month"][m], kept[m], drop[m], X["B"][m], n_boot=1)["pool"]["m"]
        y20k, y20d = rs.split(Y["side"], Y[v])
        y20 = rs.delta_boot(Y["g"], Y["month"], y20k, y20d, Y["B"], n_boot=1)["pool"]["m"] if Y["B"].size else math.nan
        emit("за роками (точково): " + ", ".join(f"{y} {d:+.3f}" for y, d in yr.items()) + f"; 2020 (описово, свій ланцюг) {y20:+.3f}")
        res[v]["years"] = yr
        sides = {}
        for sd in ("long", "short"):
            m = X["side"] == sd
            sides[sd] = rs.delta_boot(X["g"][m], X["month"][m], kept[m], drop[m], X["B"][m], n_boot=N_BOOT, seed=SEED)["pool"]
        emit(f"за боками: лонги {f3(sides['long'])}, шорти {f3(sides['short'])}")
        emit("| сетап | n лишає | n відкидає | Δ | 95 % CI | p |\n|---|---|---|---|---|---|")
        ms, ss_ = [], []
        for s, p in zip(DEMO, r["per"], strict=True):
            emit(f"| {s} | {int(p['n_k'])} | {int(p['n_d'])} | {p['m']:+.3f} | [{p['lo']:+.3f}; {p['hi']:+.3f}] | {p['p']:.3f} |")
            if math.isfinite(p["se"]) and p["se"] > 0:
                ms.append(p["m"])
                ss_.append(p["se"])
        w = 1 / np.asarray(ss_) ** 2
        q = float(np.sum(w * (np.asarray(ms) - np.sum(w * np.asarray(ms)) / w.sum()) ** 2))
        emit(f"гетерогенність сетапів: Q {q:.2f}, df {len(ms) - 1}, I² {max(0.0, (q - len(ms) + 1) / q) if q > 0 else 0.0:.2f}")
        valid = [(s, p["m"]) for s, p in zip(DEMO, r["per"], strict=True) if math.isfinite(p["m"])]
        top = max(valid, key=lambda x: x[1])[0]
        mt = X["g"] != DEMO.index(top)
        emit(
            f"без найбільшого внеску ({top}): {f3(rs.delta_boot(X['g'][mt], X['month'][mt], kept[mt], drop[mt], X['B'][mt], n_boot=N_BOOT, seed=SEED)['pool'])}"
        )
        res[v]["notop"] = rs.delta_boot(X["g"][mt], X["month"][mt], kept[mt], drop[mt], X["B"][mt], n_boot=1)["pool"]["m"]
        pd_ = placebo_drop(X, kept, drop, r["pool"]["m"])
        psh = placebo_shift(X, signs[v], r["pool"]["m"])
        res[v]["pl"], res[v]["sh"] = pd_, psh
        emit(
            f"плацебо випадкового відкидання (та сама частка в кожному сетап × бік, {N_PLACEBO}): середнє {pd_['mean']:+.4f}, p {pd_['p']:.4f}"
        )
        emit(f"плацебо зсуву режиму (кругове, ≥ {MIN_SHIFT_DAYS} днів, {N_SHIFT}): середнє {psh['mean']:+.4f}, p {psh['p']:.4f}")
        emit("| вибірка | n | E[R] ×1 | 95 % CI (місяці) | E[R] ×1.5 | угод / рік | Σ R / рік |\n|---|---|---|---|---|---|---|")
        span = {"2021+": (vsp.PASSPORT_MS, vsp.END["4h"]), "IS": (vsp.PASSPORT_MS, vsp.OOS_MS), "OOS": (vsp.OOS_MS, vsp.END["4h"])}
        for lab, pm in (("2021+", full), ("IS", ~oos), ("OOS", oos)):
            yrs = (span[lab][1] - span[lab][0]) / YEAR_MS
            for gl, gm in (("усі (база)", full), ("з фільтром (лишає + не діє)", kept | ~(kept | drop)), ("відкидає", drop)):
                m = pm & gm
                c = mean_ci(X["B"][m], X["month"][m])
                emit(
                    f"| {lab} {gl} | {int(m.sum())} | {c['m']:+.3f} | [{c['lo']:+.3f}; {c['hi']:+.3f}] | {np.mean(X['B15'][m]):+.3f} | {m.sum() / yrs:.0f} | {X['B'][m].sum() / yrs:+.1f} |"
                )
        if v in VARIANTS:
            res[v]["kept15_oos"] = float(np.mean(X["B15"][oos & (kept | ~(kept | drop))]))
        # realistic: filter before the chain (a dropped trade frees the symbol for a later one)
        tot, tot15, n_ = 0.0, 0.0, 0
        for p in main:
            T = p["_T"]
            if T is None:
                continue
            sg = signs[v]
            day = (T["t_in"] // rs.DAY_MS) * rs.DAY_MS - rs.DAY_MS
            rg = np.array([sg.get(int(d), 0) for d in day], dtype=np.int64)
            k_, d_ = rs.split(T["side"].astype(str), rg)
            Vf = vsp.chain(T, p["_cand"] & ~d_, p["_cap"])
            if p["_filled"] is not None:
                Vf &= p["_filled"]
            Vf &= T["t_in"] >= vsp.OOS_MS
            tot += float(T["B"][Vf].sum())
            tot15 += float(T["B15"][Vf].sum())
            n_ += int(Vf.sum())
        yrs = (vsp.END["4h"] - vsp.OOS_MS) / YEAR_MS
        emit(
            f"ланцюг з фільтром до нього (реалістично), OOS: n {n_}, E[R] {tot / max(n_, 1):+.3f} (×1.5 {tot15 / max(n_, 1):+.3f}), угод / рік {n_ / yrs:.0f}, Σ R / рік {tot / yrs:+.1f}"
        )
        # 5a: per coin
        pc = per_coin(X, kept, drop, full)
        if len(pc) >= 2:
            m_, s_ = np.array([x[0] for x in pc.values()]), np.array([x[1] for x in pc.values()])
            w = 1 / s_**2
            q = float(np.sum(w * (m_ - np.sum(w * m_) / w.sum()) ** 2))
            emit(
                f"5a монети (≥ 10 лишених і ≥ 10 відкинутих, 2021+): {len(pc)} монет, Q {q:.1f}, df {len(pc) - 1}, I² {max(0.0, (q - len(pc) + 1) / q) if q > 0 else 0.0:.2f}"
            )
        pis, poos = per_coin(X, kept, drop, ~oos, 5), per_coin(X, kept, drop, oos, 1)
        rank = sorted(pis, key=lambda s: -pis[s][0])[:10]
        topm = np.isin(X["sym"], rank) & oos
        rest = ~np.isin(X["sym"], rank) & oos
        dt = rs.delta_boot(
            np.zeros(int(topm.sum()), np.int64), X["month"][topm], kept[topm], drop[topm], X["B"][topm], n_boot=1000, seed=SEED
        )["pool"]
        dr = rs.delta_boot(
            np.zeros(int(rest.sum()), np.int64), X["month"][rest], kept[rest], drop[rest], X["B"][rest], n_boot=1000, seed=SEED
        )["pool"]
        emit(
            f"5a стійкість: топ-10 монет за Δ на IS ({', '.join(rank)}) на OOS {f3(dt)} проти решти {f3(dr)}; монет з OOS-даними {len(poos)}"
        )
    emit("\n## Вердикт за реєстрацією")
    for v in VARIANTS:
        r = res[v]
        d, lo, hi, se = r["all"]["pool"]["m"], r["all"]["pool"]["lo"], r["all"]["pool"]["hi"], r["all"]["pool"]["se"]
        sig = r["holm"] < 0.05 and d > 0
        checks = {
            "Холм < 0.05, Δ > 0": sig,
            "IS > 0 і OOS > 0": r["IS"]["m"] > 0 and r["OOS"]["m"] > 0,
            "2022 > 0 і 2024 > 0": r["years"].get(2022, math.nan) > 0 and r["years"].get(2024, math.nan) > 0,
            "плацебо відкидання p < 0.05": r["pl"]["p"] < 0.05,
            "без найбільшого внеску > 0": r["notop"] > 0,
        }
        if all(checks.values()):
            lab = "✓" if r["sh"]["p"] < 0.10 else "≈ (плацебо зсуву p ≥ 0.10)"
        elif sig:
            lab = "≈ (значуще, але не всі перевірки)"
        elif hi < INTEREST:
            lab = "✗ (корисний ефект ≥ 0.05R виключено)"
        elif rs.mde80(se, 0.025) > INTEREST:
            lab = f"◯ (MDE₈₀ {rs.mde80(se, 0.025):.3f} > {INTEREST})"
        else:
            lab = "✗"
        emit(f"{v}: {lab}; Δ {d:+.3f} [{lo:+.3f}; {hi:+.3f}]; " + "; ".join(f"{k}: {'так' if ok else 'ні'}" for k, ok in checks.items()))
        emit(f"{v}: E[R] лишених OOS ×1.5 {r['kept15_oos']:+.3f} (корисно для демо лише якщо > 0)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("stage0", "report"), required=True)
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/regime_side")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s12-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_modules")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--regime-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/cycle_regime")
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with (a.out_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"verify_regime_side --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        stage0(a) if a.stage == "stage0" else stage_report(a)
        emit(f"peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
