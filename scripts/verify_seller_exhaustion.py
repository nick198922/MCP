"""Seller Exhaustion Constant of BTC as a short veto on all demo setups (IDEA-20261030-demo-setups-depth, item
«виснаження продавців BTC забороняє шорти»; registration «Попередня реєстрація — виснаження продавців забороняє шорти»).

Reuses the trade set and harness of `scripts/verify_regime_side.py` (the passports' demo-list trades of the 9 demo
setups, 2021-01 .. 2026-09, S12 = v4, S17 = 1d) and replaces the regime sign by the SEC sign: +1 on a day with
SEC < 0.05 (shorts vetoed), else 0. No network: Bitview day1 arrays from ``--bitview-dir``.

Stages:
  stage0  sanity (data, known number, Bitview vs Binance close, lookahead) + counts + MDE80; no Delta, no group mean R
  report  the registered test (run only after the registration is committed)
Output: ``<out-dir>/<stage>.txt``.
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

import verify_regime_side as vrs  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import cycle_regime as cr  # noqa: E402
from tradedesk.analytics import regime_side as rs  # noqa: E402
from tradedesk.analytics import seller_exhaustion as se  # noqa: E402

BITVIEW_START_MS = int(datetime(2011, 1, 1, tzinfo=UTC).timestamp() * 1000)
SHORT_SETUPS = ("S14", "S16", "S3", "S12", "S38", "S39", "S17")
MDE_GATE = 0.30  # registered: MDE80 above this -> descriptive run only, verdict ◯
INTEREST = 0.05
emit = vrs.emit


def day_str(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def load_sec(a: argparse.Namespace) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    share = se.daily_series(json.loads((a.bitview_dir / "supply_in_profit_share_day1_2011.json").read_text()), BITVIEW_START_MS)
    price = se.daily_series(json.loads((a.bitview_dir / "price_day1_2011.json").read_text()), BITVIEW_START_MS)
    return share, price, se.sec_series(share, price)


def attach(X: dict[str, np.ndarray], sign: dict[int, int]) -> np.ndarray:
    """SEC sign of the last UTC day closed by the decision time (X['day'] = that day's open, as in verify_regime_side)."""
    return np.array([sign.get(int(d), 0) for d in X["day"]], dtype=np.int64)


def months_with(X: dict[str, np.ndarray], m: np.ndarray) -> int:
    return int(np.unique(X["month"][m]).size)


# ---------------------------------------------------------------- stage 0


def stage0(a: argparse.Namespace) -> None:
    share, price, sec = load_sec(a)
    sign = se.sec_sign(sec)
    days = sorted(price)
    emit("## Дані Bitview")
    emit(f"price: {len(price)} днів, {day_str(days[0])} … {day_str(days[-1])}; share: {len(share)} днів; SEC: {len(sec)} днів")
    last = max(sec)
    emit(f"відоме число: SEC {day_str(last)} = {sec[last]:.4f} (чат 04.11: 0.0506; графік Bitview 0.0498)")
    lows = sorted((v, t) for t, v in sec.items() if t >= int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000))[:1]
    emit(f"мінімум 2026: {lows[0][0]:.4f} на {day_str(lows[0][1])} (запис бібліотеки: 0.028 на 2026-08-16)")
    # Bitview price vs Binance spot close: is the Bitview day value the close of day D (no lookahead) ?
    spot = cr.parse_closes(json.loads((a.regime_cache / "spot_1d.json").read_text())["rows"])
    common = [t for t in sorted(spot) if t in price and t - se.DAY_MS in spot]
    r0 = np.array([price[t] / spot[t] for t in common])
    r1 = np.array([price[t] / spot[t - se.DAY_MS] for t in common])
    emit(
        f"Bitview price[D] / Binance close[D]: n {len(common)}, медіана {np.median(r0):.5f}, частка |Δ| < 0.5 % {np.mean(np.abs(r0 - 1) < 0.005):.3f}; "
        f"проти close[D−1]: частка |Δ| < 0.5 % {np.mean(np.abs(r1 - 1) < 0.005):.3f}"
    )
    sec_bn = se.sec_series({t: share[t] for t in common if t in share}, spot)
    both = [t for t in sec_bn if t in sec and t >= vsp.START_MS]
    agree = np.mean([sign[t] == (1 if sec_bn[t] < se.THRESHOLD else 0) for t in both])
    emit(f"знак SEC з ціни Bitview проти ціни Binance spot (2020-02+): збіг {agree:.4f} на {len(both)} днях")
    emit(
        "частка днів SEC < 0.05 за роками: "
        + ", ".join(
            f"{y} {np.mean([sign[t] for t in sign if datetime.fromtimestamp(t / 1000, UTC).year == y]):.3f}" for y in range(2018, 2027)
        )
    )
    span = [t for t in sign if vsp.PASSPORT_MS <= t < vsp.END["4h"]]
    emit(f"частка днів SEC < 0.05, 2021-01 … 2026-09: {np.mean([sign[t] for t in span]):.3f}")
    runs, cur = [], None
    for t in sorted(sign):
        if sign[t] == 1 and t >= vsp.START_MS:
            if cur is not None and t - cur[1] == se.DAY_MS:
                cur[1] = t
            else:
                cur = [t, t]
                runs.append(cur)
    emit("епізоди SEC < 0.05 (2020-02+): " + "; ".join(f"{day_str(s)} … {day_str(e)} ({(e - s) // se.DAY_MS + 1} д)" for s, e in runs))

    X, Y, _, _ = vrs.load_all(a)
    sg = attach(X, sign)
    kept, drop = se.short_masks(X["side"], sg)
    rest_l, low_l = se.long_masks(X["side"], sg)
    oos = X["t_in"] >= vsp.OOS_MS
    emit("\n## Угоди (2021+): шорти лишаються / відкидаються, лонги при SEC < 0.05 (без жодного R)")
    emit("| сетап | шорти | відкидає (2021+) | відкидає IS | відкидає OOS | частка | лонги при SEC < 0.05 |\n|---|---|---|---|---|---|---|")
    for k, s in enumerate(vrs.DEMO):
        m = X["g"] == k
        nsh = int((m & (X["side"] == "short")).sum())
        emit(
            f"| {s} | {nsh} | {int((m & drop).sum())} | {int((m & drop & ~oos).sum())} | {int((m & drop & oos).sum())} | "
            f"{(m & drop).sum() / max(nsh, 1):.3f} | {int((m & low_l).sum())} |"
        )
    emit(
        f"| разом | {int(kept.sum() + drop.sum())} | {int(drop.sum())} | {int((drop & ~oos).sum())} | {int((drop & oos).sum())} | "
        f"{drop.sum() / max(kept.sum() + drop.sum(), 1):.3f} | {int(low_l.sum())} |"
    )
    years = sorted(set(X["year"].tolist()))
    emit("відкинуті шорти за роками: " + ", ".join(f"{y} {int((drop & (X['year'] == y)).sum())}" for y in years))
    emit(
        f"місяців з відкинутими шортами: 2021+ {months_with(X, drop)}, IS {months_with(X, drop & ~oos)}, OOS {months_with(X, drop & oos)}; "
        f"місяців з лишеними шортами: {months_with(X, kept)}"
    )
    ysg = attach(Y, sign)
    emit(
        f"2020 (описово): відкинуті шорти {int(se.short_masks(Y['side'], ysg)[1].sum())}, лонги при SEC < 0.05 {int(se.long_masks(Y['side'], ysg)[1].sum())}"
    )
    emit("\n## MDE₈₀ (SE з бутстрепу на R, центрованому в кожній клітинці сетап × лишає / відкидає: Δ = 0 за побудовою)")
    emit("| вибірка | пул | кластер | SE | MDE₈₀ α 0.05 |\n|---|---|---|---|---|")
    mde_main = math.nan
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
                seed=vrs.SEED,
                center=True,
            )
            se_ = r["pool"]["se"]
            if (lab, pool, cl) == ("2021+", "сетапи порівну", "month"):
                mde_main = rs.mde80(se_)
            emit(f"| {lab} | {pool} | {cl} | {se_:.4f} | {rs.mde80(se_):.3f} |")
    r = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], n_boot=1000, seed=vrs.SEED, center=True)
    emit(
        "SE окремих сетапів (місяці): "
        + ", ".join(
            f"{s} {p['se']:.3f} (n {int(p['n_k'])}/{int(p['n_d'])})" for s, p in zip(vrs.DEMO, r["per"], strict=True) if s in SHORT_SETUPS
        )
    )
    rl = rs.delta_boot(X["g"], X["month"], low_l, rest_l, X["B"], n_boot=1000, seed=vrs.SEED, center=True)
    emit(f"лонги (описово) SE {rl['pool']['se']:.4f}, MDE₈₀ {rs.mde80(rl['pool']['se']):.3f}")
    gate = "описовий прогін, вердикт ◯" if mde_main > MDE_GATE else "прогін з вердиктом за критеріями"
    emit(f"\nворота реєстрації: MDE₈₀ 2021+ (сетапи порівну, місяці) {mde_main:.3f} vs {MDE_GATE} → {gate}")


# ---------------------------------------------------------------- report (the registered test; only after the commit)


def report(a: argparse.Namespace) -> None:
    _, _, sec = load_sec(a)
    sign = se.sec_sign(sec)
    X, Y, _, main = vrs.load_all(a)
    sg = attach(X, sign)
    kept, drop = se.short_masks(X["side"], sg)
    rest_l, low_l = se.long_masks(X["side"], sg)
    oos = X["t_in"] >= vsp.OOS_MS
    full = np.ones_like(oos)
    f3 = vrs.f3
    N = vrs.N_BOOT
    r = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], n_boot=N, seed=vrs.SEED)
    c = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], n_boot=N, seed=vrs.SEED, center=True)
    mde = rs.mde80(c["pool"]["se"])
    emit("## Основне: Δ = E[R] лишених шортів (SEC ≥ 0.05) − E[R] відкинутих шортів (SEC < 0.05), сетапи порівну, CI місяцями")
    emit(
        f"2021+: {f3(r['pool'])}, p {r['pool']['p']:.4f}; n лишає {int(r['pool']['n_k'])}, відкидає {int(r['pool']['n_d'])}; MDE₈₀ {mde:.3f}"
    )
    res: dict[str, Any] = {"all": r["pool"], "mde": mde}
    for lab, m in (("IS", ~oos), ("OOS", oos)):
        rr = rs.delta_boot(X["g"][m], X["month"][m], kept[m], drop[m], X["B"][m], n_boot=N, seed=vrs.SEED)
        res[lab] = rr["pool"]
        emit(f"{lab}: {f3(rr['pool'])}; n лишає {int(rr['pool']['n_k'])}, відкидає {int(rr['pool']['n_d'])}")
    wk = rs.delta_boot(X["g"], X["week"], kept, drop, X["B"], n_boot=N, seed=vrs.SEED)
    tr = rs.delta_boot(X["g"], X["month"], kept, drop, X["B"], pooled="trades", n_boot=N, seed=vrs.SEED)
    c15 = rs.delta_boot(X["g"], X["month"], kept, drop, X["B15"], n_boot=N, seed=vrs.SEED)
    res["c15"] = c15["pool"]
    emit(f"чутливість: тижневі кластери {f3(wk['pool'])}; угоди разом {f3(tr['pool'])}; витрати ×1.5 {f3(c15['pool'])}")
    years = sorted(set(X["year"][drop].tolist()))
    yr = {}
    for y in years:
        m = X["year"] == y
        yr[y] = rs.delta_boot(X["g"][m], X["month"][m], kept[m], drop[m], X["B"][m], n_boot=1)["pool"]
    res["years"] = {y: v["m"] for y, v in yr.items()}
    emit(
        "за роками з відкинутими шортами (точково): "
        + ", ".join(f"{y} {v['m']:+.3f} (n {int(v['n_k'])}/{int(v['n_d'])})" for y, v in yr.items())
    )
    ysg = attach(Y, sign)
    yk, yd = se.short_masks(Y["side"], ysg)
    emit(
        f"2020 (описово, свій ланцюг): відкинутих шортів {int(yd.sum())}"
        + (f", Δ {rs.delta_boot(Y['g'], Y['month'], yk, yd, Y['B'], n_boot=1)['pool']['m']:+.3f}" if yd.any() else "")
    )
    emit("| сетап | n лишає | n відкидає | Δ | 95 % CI | p |\n|---|---|---|---|---|---|")
    ms, ss_ = [], []
    for s, p in zip(vrs.DEMO, r["per"], strict=True):
        if s not in SHORT_SETUPS:
            continue
        emit(f"| {s} | {int(p['n_k'])} | {int(p['n_d'])} | {p['m']:+.3f} | [{p['lo']:+.3f}; {p['hi']:+.3f}] | {p['p']:.3f} |")
        if math.isfinite(p["se"]) and p["se"] > 0:
            ms.append(p["m"])
            ss_.append(p["se"])
    if len(ms) >= 2:
        w = 1 / np.asarray(ss_) ** 2
        q = float(np.sum(w * (np.asarray(ms) - np.sum(w * np.asarray(ms)) / w.sum()) ** 2))
        emit(f"гетерогенність сетапів: Q {q:.2f}, df {len(ms) - 1}, I² {max(0.0, (q - len(ms) + 1) / q) if q > 0 else 0.0:.2f}")
    valid = [(s, p["m"]) for s, p in zip(vrs.DEMO, r["per"], strict=True) if math.isfinite(p["m"])]
    top = max(valid, key=lambda x: x[1])[0]
    mt = X["g"] != vrs.DEMO.index(top)
    nt = rs.delta_boot(X["g"][mt], X["month"][mt], kept[mt], drop[mt], X["B"][mt], n_boot=N, seed=vrs.SEED)["pool"]
    res["notop"] = nt["m"]
    emit(f"без найбільшого внеску ({top}): {f3(nt)}")
    pl = vrs.placebo_drop(X, kept, drop, r["pool"]["m"])
    res["pl"] = pl
    emit(
        f"П1 плацебо: випадкове відкидання тієї самої кількості шортів у кожному сетапі ({vrs.N_PLACEBO}): середнє {pl['mean']:+.4f}, p {pl['p']:.4f}"
    )
    rng = np.random.default_rng(vrs.SEED + 1)
    nz = len(sign)
    udays, inv = np.unique(X["day"], return_inverse=True)
    vals = []
    for _ in range(vrs.N_SHIFT):
        sh = rs.shift_signs(
            {t: (1 if v == 1 else -1) for t, v in sign.items()}, int(rng.integers(vrs.MIN_SHIFT_DAYS, nz - vrs.MIN_SHIFT_DAYS))
        )
        rg = np.array([1 if sh.get(int(d), 0) == 1 else 0 for d in udays], dtype=np.int64)[inv]
        k_, d_ = se.short_masks(X["side"], rg)
        vals.append(rs.delta_boot(X["g"], X["month"], k_, d_, X["B"], n_boot=1)["pool"]["m"])
    b = np.asarray(vals)
    emit(
        f"П2 (описово): круговий зсув денного ряду SEC-знаків ≥ {vrs.MIN_SHIFT_DAYS} днів ({vrs.N_SHIFT}): середнє {np.nanmean(b):+.4f}, p {(np.sum(b >= r['pool']['m']) + 1) / (b.size + 1):.4f}"
    )
    # strategy rows
    emit("| вибірка | n | E[R] ×1 | 95 % CI (місяці) | E[R] ×1.5 | угод / рік | Σ R / рік |\n|---|---|---|---|---|---|---|")
    span = {"2021+": (vsp.PASSPORT_MS, vsp.END["4h"]), "IS": (vsp.PASSPORT_MS, vsp.OOS_MS), "OOS": (vsp.OOS_MS, vsp.END["4h"])}
    for lab, pm in (("2021+", full), ("IS", ~oos), ("OOS", oos)):
        yrs = (span[lab][1] - span[lab][0]) / vrs.YEAR_MS
        groups = (
            ("усі угоди (база)", full),
            ("з вето (без відкинутих шортів)", ~drop),
            ("шорти всі", kept | drop),
            ("шорти лишені", kept),
            ("шорти відкинуті", drop),
        )
        for gl, gm in groups:
            m = pm & gm
            if not m.any():
                emit(f"| {lab} {gl} | 0 | | | | | |")
                continue
            ci = vrs.mean_ci(X["B"][m], X["month"][m])
            emit(
                f"| {lab} {gl} | {int(m.sum())} | {ci['m']:+.3f} | [{ci['lo']:+.3f}; {ci['hi']:+.3f}] | {np.mean(X['B15'][m]):+.3f} | {m.sum() / yrs:.0f} | {X['B'][m].sum() / yrs:+.1f} |"
            )
    res["veto15_oos"] = float(np.mean(X["B15"][oos & ~drop]))
    for lab, lo_ms in (("2021+", vsp.PASSPORT_MS), ("OOS", vsp.OOS_MS)):
        tot, tot15, n_ = 0.0, 0.0, 0
        for p in main:
            T = p["_T"]
            if T is None:
                continue
            day = (T["t_in"] // rs.DAY_MS) * rs.DAY_MS - rs.DAY_MS
            rg = np.array([sign.get(int(d), 0) for d in day], dtype=np.int64)
            _, d_ = se.short_masks(T["side"].astype(str), rg)
            Vf = vsp.chain(T, p["_cand"] & ~d_, p["_cap"])
            if p["_filled"] is not None:
                Vf &= p["_filled"]
            Vf &= T["t_in"] >= lo_ms
            tot += float(T["B"][Vf].sum())
            tot15 += float(T["B15"][Vf].sum())
            n_ += int(Vf.sum())
        yrs = (vsp.END["4h"] - lo_ms) / vrs.YEAR_MS
        emit(
            f"ланцюг з вето до нього (реалістично), {lab}: n {n_}, E[R] {tot / max(n_, 1):+.3f} (×1.5 {tot15 / max(n_, 1):+.3f}), угод / рік {n_ / yrs:.0f}, Σ R / рік {tot / yrs:+.1f}"
        )
    # 5a
    pc = vrs.per_coin(X, kept, drop, full)
    if len(pc) >= 2:
        m_, s_ = np.array([x[0] for x in pc.values()]), np.array([x[1] for x in pc.values()])
        w = 1 / s_**2
        q = float(np.sum(w * (m_ - np.sum(w * m_) / w.sum()) ** 2))
        emit(
            f"5a монети (≥ 10 лишених і ≥ 10 відкинутих шортів, 2021+): {len(pc)} монет, Q {q:.1f}, df {len(pc) - 1}, I² {max(0.0, (q - len(pc) + 1) / q) if q > 0 else 0.0:.2f}"
        )
    else:
        emit(f"5a монети: монет з ≥ 10 / ≥ 10 шортів лише {len(pc)} — гетерогенність не рахується")
    pis = vrs.per_coin(X, kept, drop, ~oos, 5)
    rank = sorted(pis, key=lambda s: -pis[s][0])[:10]
    topm, restm = np.isin(X["sym"], rank) & oos, ~np.isin(X["sym"], rank) & oos
    if rank and (topm & drop).any() and (restm & drop).any():
        z = np.zeros
        dt = rs.delta_boot(
            z(int(topm.sum()), np.int64), X["month"][topm], kept[topm], drop[topm], X["B"][topm], n_boot=1000, seed=vrs.SEED
        )["pool"]
        dr = rs.delta_boot(
            z(int(restm.sum()), np.int64), X["month"][restm], kept[restm], drop[restm], X["B"][restm], n_boot=1000, seed=vrs.SEED
        )["pool"]
        emit(f"5a стійкість: топ-{len(rank)} монет за Δ на IS ({', '.join(rank)}) на OOS {f3(dt)} проти решти {f3(dr)}")
    else:
        emit(f"5a стійкість: монет з ≥ 5 / ≥ 5 шортів на IS {len(pis)}; на OOS бракує відкинутих шортів у топ / решти — не рахується")
    # longs, descriptive
    rl = rs.delta_boot(X["g"], X["month"], low_l, rest_l, X["B"], n_boot=N, seed=vrs.SEED)
    emit(
        f"\n## Лонги (описово): E[R] лонгів при SEC < 0.05 − E[R] решти лонгів, сетапи порівну: {f3(rl['pool'])}, n {int(rl['pool']['n_k'])} / {int(rl['pool']['n_d'])}"
    )
    for lab, m in (("IS", ~oos), ("OOS", oos)):
        x = rs.delta_boot(X["g"][m], X["month"][m], low_l[m], rest_l[m], X["B"][m], n_boot=N, seed=vrs.SEED)["pool"]
        emit(f"лонги {lab}: {f3(x)}")
    # verdict
    d, lo, hi = res["all"]["m"], res["all"]["lo"], res["all"]["hi"]
    emit("\n## Вердикт за реєстрацією")
    if a.registered_mde > MDE_GATE:
        emit(
            f"◯ (зареєстрований MDE₈₀ етапу 0 {a.registered_mde:.3f} > {MDE_GATE}: прогін описовий; MDE₈₀ цього прогону {mde:.3f}); "
            f"Δ {d:+.3f} [{lo:+.3f}; {hi:+.3f}]"
        )
    else:
        checks = {
            "p < 0.05, Δ > 0": res["all"]["p"] < 0.05 and d > 0,
            "IS > 0 і OOS > 0": res["IS"]["m"] > 0 and res["OOS"]["m"] > 0,
            "кожен рік з відкинутими > 0": all(v > 0 for v in res["years"].values()),
            "×1.5 > 0": res["c15"]["m"] > 0,
            "П1 p < 0.05": res["pl"]["p"] < 0.05,
            "без найбільшого внеску > 0": res["notop"] > 0,
        }
        if all(checks.values()):
            lab = "✓"
        elif checks["p < 0.05, Δ > 0"]:
            lab = "≈"
        elif hi < INTEREST:
            lab = "✗ (корисний ефект ≥ 0.05R виключено)"
        else:
            lab = "◯"
        emit(f"{lab}; Δ {d:+.3f} [{lo:+.3f}; {hi:+.3f}]; " + "; ".join(f"{k}: {'так' if ok else 'ні'}" for k, ok in checks.items()))
    emit(f"E[R] стратегії з вето OOS ×1.5 {res['veto15_oos']:+.3f} (корисно для демо лише якщо > 0)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("stage0", "report"), required=True)
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/seller_exhaustion")
    ap.add_argument("--bitview-dir", type=Path, default=Path.home() / "private/data/bitview")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s12-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_modules")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--regime-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/cycle_regime")
    # the gate is the stage-0 value written into the registration (2026-11-04 18:22 UTC), not this run's re-estimate
    ap.add_argument("--registered-mde", type=float, default=0.306)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with (a.out_dir / f"{a.stage}.txt").open("w") as fh:
        vrs.OUT["fh"] = fh
        emit(f"# seller_exhaustion {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        (stage0 if a.stage == "stage0" else report)(a)
        emit(f"peak RSS {vrs.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
