"""Playbook rule P3 (200-day trend -> vertical debit spread) on Deribit BTC marks with Bybit costs.

IDEA-20261114-options-playbook, item 4.1; pre-registration: the card's "4.1. P3 ... попередня реєстрація".
No network if the caches exist: put-skew-v2 surfaces (``--src-dir``: ``snapshots_v2.json``,
``delivery_btc_usd.json``) and Binance BTC/USDT 1d closes (``--closes-dir``/``binance_BTCUSDT_1d.json``, fetched
once through the runtime provider by ``verify_assistant_recs.load_closes`` if missing). Read only.

    uv run python scripts/verify_p3_trend_spread.py --stage coverage   # stage 0: coverage and sanity, no P&L
    uv run python scripts/verify_p3_trend_spread.py --stage full --out ~/.cache/tradedesk-agents/p3_trend_spread/full.md
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_put_skew as vps  # noqa: E402  (scripts/ is not a package)
from common_helpers import rss_mb, ymd_ms  # noqa: E402
from verify_assistant_recs import load_closes  # noqa: E402

from tradedesk.analytics.option_condor import block_bootstrap_mean  # noqa: E402
from tradedesk.analytics.option_ideas import check_trend  # noqa: E402
from tradedesk.analytics.pm_modules import cochran_q  # noqa: E402
from tradedesk.analytics.put_skew import choose_expiry  # noqa: E402
from tradedesk.analytics.trend_spread import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    cluster_bootstrap,
    entry_debit,
    episodes,
    exec_ivs,
    holm_adjust,
    managed,
    plan_spread,
    settle,
    trend_at,
)

FIRST = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
OOS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY_MS = vps.LAST_EXPIRY_MS
COVERAGE_MIN = 0.60
SEED = 20261114
N_BOOT = 10_000
EDGE_R = 0.15  # pre-registered "edge worth the effort" for the ✗ rule
BASE: dict[str, Any] = {
    "weekday": 5,
    "target": 14.0,
    "lo": 7.0,
    "hi": 24.0,
    "long_delta": 0.40,
    "short_delta": 0.15,
    "model": "mark",
    "hs": 0.015,
    "band": 0.015,
    "exit": "expiry",
    "tp": 0.70,
    "counter": False,
}


def log(msg: str, t0: float) -> None:
    print(f"[{time.perf_counter() - t0:6.0f}s, rss {rss_mb():.0f} MB] {msg}", flush=True)


def pc(x: float | None, k: float = 100.0) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.2f}"


def rr(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.3f}"


def ci_r(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0]:+.3f}; {ci[1]:+.3f}]" if ci else "—"


# ----------------------------------------------------------------------------- data


def load(src: Path, closes_dir: Path, kdir: Path, t0: float) -> dict[str, Any]:
    stamps, closes, info = load_closes(closes_dir, kdir, t0)
    days, _meta = vps.load_snapshots(src, "v2")
    delivery = {r["date"]: float(r["delivery_price"]) for r in json.loads((src / "delivery_btc_usd.json").read_text())}
    log(f"loaded {len(days)} surfaces, {len(delivery)} delivery prices, {len(closes)} daily closes ({info.get('source')})", t0)
    return {"days": days, "delivery": delivery, "stamps": stamps, "closes": closes, "info": info}


def entry_days(D: dict[str, Any], weekday: int) -> list[int]:
    last = LAST_EXPIRY_MS - 7 * DAY_MS
    d = FIRST
    while datetime.fromtimestamp(d / 1000, UTC).weekday() != weekday:
        d += DAY_MS
    out = []
    while d <= last:
        out.append(d)
        d += 7 * DAY_MS
    return out


def labels_for(D: dict[str, Any], weekday: int) -> dict[int, str]:
    return {d: trend_at(D["stamps"], D["closes"], d + 9 * HOUR_MS) for d in entry_days(D, weekday)}


def trade(D: dict[str, Any], d: int, label: str, P: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    snap = D["days"].get(d)
    if snap is None:
        return None, "no surface"
    e = choose_expiry(snap, P["target"], P["lo"], P["hi"], rule="nearest")
    if e is None:
        return None, "no expiry in window"
    if e > LAST_EXPIRY_MS:
        return None, "expiry after cache end"
    dp = D["delivery"].get(ymd_ms(e))
    if dp is None:
        return None, "no delivery price"
    bearish = (label == "bear") != P["counter"]
    plan, why = plan_spread(snap, e, bearish, long_delta=P["long_delta"], short_delta=P["short_delta"], band=P["band"])
    if plan is None:
        return None, why
    iv_l, iv_s, fb = exec_ivs(plan, P["model"], P["hs"])
    debit = entry_debit(plan, iv_l, iv_s)
    if debit is None:
        return None, "debit outside (0, width)"
    if P["exit"] == "expiry":
        res = settle(plan, debit, dp)
    else:
        res = managed(D["days"], plan, debit, dp, tp=P["tp"], beyond_short=True, hs=P["hs"])
    return {
        "day": d,
        "expiry": e,
        "label": label,
        "bearish": bearish,
        "spot": plan["spot"],
        "K_long": plan["K_long"],
        "K_short": plan["K_short"],
        "width": plan["width"],
        "debit": debit,
        "delta_long": plan["delta_long"],
        "delta_short": plan["delta_short"],
        "dte": plan["T"] * 365,
        "fallbacks": fb,
        **res,
    }, ""


def run(D: dict[str, Any], labels: dict[int, str], P: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str], int]:
    out: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    elig = 0
    for d, lab in sorted(labels.items()):
        if lab == "neutral":
            continue
        elig += 1
        r, w = trade(D, d, lab, P)
        if r is None:
            why[w] += 1
        else:
            out.append(r)
    return out, why, elig


# ----------------------------------------------------------------------------- stats


def summary(trades: list[dict[str, Any]], ep: dict[int, int], key: str = "r") -> dict[str, Any]:
    v = [t[key] for t in trades]
    b = cluster_bootstrap(v, [ep[t["day"]] for t in trades], n_boot=N_BOOT, seed=SEED)
    b["pnl_spot"] = statistics.fmean(t["pnl_spot"] for t in trades) if trades else float("nan")
    b["mde"] = 2.8 * b["se"] if trades else float("nan")
    return b


def row(label: str, trades: list[dict[str, Any]], ep: dict[int, int]) -> str:
    if not trades:
        return f"| {label} | 0 | 0 | — | — | — | — | — |"
    s = summary(trades, ep)
    win = sum(1 for t in trades if t["r"] > 0) / len(trades)
    return (
        f"| {label} | {s['n']} | {s['n_clusters']} | {rr(s['mean'])} | {ci_r(s['ci'])} | {s['p']:.3f} | {pc(s['pnl_spot'])} | {win:.0%} |"
    )


HDR = [
    "| варіант | n | епізодів | середній R | 95 % CI (кластери-епізоди) | p | % спота | частка R > 0 |",
    "|---|---|---|---|---|---|---|---|",
]


def sanity(L: list[str], D: dict[str, Any], labels: dict[int, str], trades: list[dict[str, Any]]) -> None:
    L.append("## Перевірки до прогону\n")
    close_at = dict(zip(D["stamps"], D["closes"], strict=True))
    diffs = [abs(D["days"][d]["spot"] / close_at[d - DAY_MS] - 1) for d in labels if d in D["days"] and d - DAY_MS in close_at]
    L.append(
        f"- рівень цін: закриття Binance (п'ятниця) проти спота поверхні (субота 09:00): n = {len(diffs)}, медіана |різниці| "
        f"{statistics.median(diffs):.2%}, максимум {max(diffs):.2%}"
    )
    ivs = [
        x["strikes"][k]["mark_iv"] for s in list(D["days"].values())[:50] for x in s["expiries"].values() for k in list(x["strikes"])[:3]
    ]
    L.append(
        f"- одиниці: mark IV (перші 50 днів) медіана {statistics.median(ivs):.3f} (десяткові), DTE угод медіана {statistics.median(t['dte'] for t in trades):.2f} д"
    )
    dl = [abs(t["delta_long"]) for t in trades]
    ds = [abs(t["delta_short"]) for t in trades]
    L.append(
        f"- фактичні |Δ| ніг: куплена медіана {statistics.median(dl):.3f} [p10 {sorted(dl)[len(dl) // 10]:.3f}; p90 {sorted(dl)[9 * len(dl) // 10]:.3f}], "
        f"продана {statistics.median(ds):.3f} [p10 {sorted(ds)[len(ds) // 10]:.3f}; p90 {sorted(ds)[9 * len(ds) // 10]:.3f}]"
    )
    fr = [t["debit"] / t["width"] for t in trades]
    L.append(f"- дебет / ширина: медіана {statistics.median(fr):.3f}, мін {min(fr):.3f}, макс {max(fr):.3f} (має бути в (0; 1))")
    last = check_trend(D["closes"])
    L.append(
        f"- мітка тренду на останньому закритті кешу ({ymd_ms(D['stamps'][-1])}): `check_trend` = {last.detail!r}, `trend_at` = {trend_at(D['stamps'], D['closes'], D['stamps'][-1] + DAY_MS)!r}"
    )
    L.append(f"- джерело закриттів: {D['info']}\n")


def coverage(L: list[str], D: dict[str, Any], labels: dict[int, str]) -> tuple[float, list[dict[str, Any]]]:
    trades, why, elig = run(D, labels, BASE)
    cnt = Counter(labels.values())
    cov = len(trades) / elig if elig else 0.0
    L.append(f"## Стадія 0: покриття (поріг {COVERAGE_MIN:.0%}; P&L не рахується)\n")
    L.append(
        f"- суботи {ymd_ms(min(labels))} … {ymd_ms(max(labels))}: {len(labels)}; bull {cnt['bull']}, bear {cnt['bear']}, neutral {cnt['neutral']}"
    )
    L.append(f"- трендових субот {elig}, спред побудовано {len(trades)} → **покриття {cov:.1%}**")
    L.append(f"- причини пропуску: {dict(why.most_common())}")
    by_year: Counter[str] = Counter(ymd_ms(d)[:4] for d, lab in labels.items() if lab != "neutral")
    ok_year: Counter[str] = Counter(ymd_ms(t["day"])[:4] for t in trades)
    L.append("- по роках (трендові / побудовано): " + ", ".join(f"{y}: {by_year[y]}/{ok_year[y]}" for y in sorted(by_year)))
    ep = episodes(list(labels.items()))
    L.append(
        f"- трендових епізодів (кластерів) серед усіх субот: {len(set(ep.values()))}; серед угод: {len({ep[t['day']] for t in trades})}\n"
    )
    return cov, trades


# ----------------------------------------------------------------------------- full


def full(L: list[str], D: dict[str, Any], labels: dict[int, str], cov: float, base: list[dict[str, Any]], cache: Path, t0: float) -> None:
    ep = episodes(list(labels.items()))
    L.append("## H1 (первинна): тримати до експірації, mark ± 1.5 в.п., комісії Bybit\n")
    L += HDR
    L.append(row("уся вибірка", base, ep))
    is_ = [t for t in base if t["day"] < OOS]
    oos = [t for t in base if t["day"] >= OOS]
    L.append(row("IS (до 2024)", is_, ep))
    L.append(row("OOS (від 2024)", oos, ep))
    for side in ("bull", "bear"):
        L.append(row(f"сторона {side}", [t for t in base if t["label"] == side], ep))
        L.append(row(f"  {side} IS", [t for t in is_ if t["label"] == side], ep))
        L.append(row(f"  {side} OOS", [t for t in oos if t["label"] == side], ep))
    for y in sorted({ymd_ms(t["day"])[:4] for t in base}):
        L.append(row(f"рік {y}", [t for t in base if ymd_ms(t["day"])[:4] == y], ep))
    top = sorted(base, key=lambda t: t["r"], reverse=True)[:5]
    L.append(row("без 5 найкращих угод", [t for t in base if t not in top], ep))
    s1 = summary(base, ep)
    blk = block_bootstrap_mean([t["r"] for t in base], block=4, n_boot=N_BOOT, seed=SEED)
    L.append(f"\n- блоковий бутстреп по тижнях (блок 4): {rr(blk['mean'])} {ci_r(blk['ci'])}, p = {blk['p']:.3f}")
    for off in (0, 1):
        sub = base[off::2]
        b = cluster_bootstrap([t["r"] for t in sub], [ep[t["day"]] for t in sub], n_boot=N_BOOT, seed=SEED)
        L.append(f"- неперекривна підвибірка (кожен другий вхід, зсув {off}): n = {b['n']}, {rr(b['mean'])} {ci_r(b['ci'])}")
    L.append(f"- MDE (2.8 × кластерна SE) = {s1['mde']:.3f} R; SE = {s1['se']:.3f}")
    L.append(f"- 5 найкращих угод: {[(ymd_ms(t['day']), t['label'], round(t['r'], 2)) for t in top]}")
    L.append(
        f"- результат угоди: мін R {min(t['r'] for t in base):+.2f}, частка R = −1 (повна втрата): {sum(1 for t in base if t['r'] <= -0.99) / len(base):.0%}"
    )
    sides = [summary([t for t in base if t["label"] == s], ep) for s in ("bull", "bear")]
    if all(s["n"] > 1 and s["se"] > 0 for s in sides):
        q = cochran_q([s["mean"] for s in sides], [s["se"] for s in sides])
        L.append(
            f"- гетерогенність сторін (Cochran Q): Q = {q.get('q', float('nan')):.2f}, p = {q.get('p', float('nan')):.3f}, I² = {q.get('i2', float('nan')):.2f}"
        )
    L.append("")

    # H2 managed exit
    L.append("## H2 (вторинна): вихід за правилом плейбуку (≥ 70 % макс. прибутку на денному зрізі або спот пройшов продану ногу)\n")
    L += HDR
    h2: dict[float, list[dict[str, Any]]] = {}
    for tp in (0.70, 0.60, 0.80):
        tr, _, _ = run(D, labels, dict(BASE, exit="managed", tp=tp))
        h2[tp] = tr
        L.append(row(f"TP {tp:.0%}" + (" (зареєстровано)" if tp == 0.70 else " (чутливість)"), tr, ep))
        L.append(row(f"  TP {tp:.0%} OOS", [t for t in tr if t["day"] >= OOS], ep))
    L.append(f"- виходи TP 70 %: {dict(Counter(t['exit'] for t in h2[0.70]))}")
    s2 = summary(h2[0.70], ep)
    log("H2 done", t0)

    # H3 paired: trend side minus counter side
    ctr, _, _ = run(D, labels, dict(BASE, counter=True))
    cmap = {t["day"]: t for t in ctr}
    pairs = [dict(t, r=t["r"] - cmap[t["day"]]["r"], pnl_spot=t["pnl_spot"] - cmap[t["day"]]["pnl_spot"]) for t in base if t["day"] in cmap]
    L.append("\n## H3 (вторинна): R за трендом − R проти тренду (той самий вхід і експірація)\n")
    L += HDR
    L.append(row("проти тренду (сам)", [t for t in ctr if t["day"] in {p["day"] for p in pairs}], ep))
    L.append(row("різниця (пари)", pairs, ep))
    L.append(row("  різниця OOS", [p for p in pairs if p["day"] >= OOS], ep))
    s3 = summary(pairs, ep)
    log("H3 done", t0)

    adj = holm_adjust({"H1": s1["p"], "H2": s2["p"], "H3": s3["p"]})
    L.append(f"\n**Холм (H1–H3):** скориговані p: {', '.join(f'{k} {v:.3f}' for k, v in adj.items())}\n")

    # sensitivity grid (all pre-registered)
    L.append("## Чутливість (усі варіанти заздалегідь зареєстровані; H1, тримати до експірації)\n")
    L += HDR
    grid: list[tuple[str, dict[str, Any], dict[int, str] | None]] = [
        ("половина спреду 0 (марки)", {"hs": 0.0}, None),
        ("половина спреду 3 в.п. (стрес)", {"hs": 0.03}, None),
        ("модель «сторона» (IV тейкерів Deribit)", {"model": "side"}, None),
        ("ціль експірації 7 д", {"target": 7.0}, None),
        ("ціль експірації 21 д", {"target": 21.0}, None),
    ]
    for ld in (0.35, 0.40, 0.45):
        for sd in (0.10, 0.15, 0.20):
            if (ld, sd) != (0.40, 0.15):
                grid.append((f"|Δ| {ld:.2f} / {sd:.2f}", {"long_delta": ld, "short_delta": sd}, None))
    fri = labels_for(D, 4)
    grid.append(("вхід у п'ятницю", {"weekday": 4}, fri))
    stress = None
    for name, ov, lab in grid:
        lb = lab or labels
        tr, _, el = run(D, lb, dict(BASE, **ov))
        e2 = episodes(list(lb.items()))
        L.append(row(f"{name} (покриття {len(tr) / el:.0%})" if el else name, tr, e2))
        if ov == {"hs": 0.03}:
            stress = summary(tr, e2)
    log("sensitivity done", t0)

    # persistence by side (5a with one asset: the two sides are the only groups)
    L.append("\n## Стійкість по сторонах (IS → OOS)\n")
    for side in ("bull", "bear"):
        a = [t["r"] for t in is_ if t["label"] == side]
        b = [t["r"] for t in oos if t["label"] == side]
        L.append(
            f"- {side}: IS n = {len(a)}, середній R {rr(statistics.fmean(a) if a else None)}; OOS n = {len(b)}, середній R {rr(statistics.fmean(b) if b else None)}"
        )

    # verdict per the registration
    m, ci = s1["mean"], s1["ci"]
    mi = statistics.fmean(t["r"] for t in is_) if is_ else float("nan")
    mo = statistics.fmean(t["r"] for t in oos) if oos else float("nan")
    if cov < COVERAGE_MIN:
        v = "◯ UNVERIFIABLE (покриття нижче воріт)"
    elif ci[0] > 0 and adj["H1"] < 0.05 and mo > 0 and stress is not None and stress["mean"] > 0:
        v = "✓ CONFIRMED"
    elif ci[1] < 0 or (m <= 0 and ci[1] < EDGE_R):
        v = "✗ NOT CONFIRMED"
    elif m > 0 and mi > 0 and mo > 0:
        v = "≈ DIRECTIONALLY RIGHT (CI містить 0)"
    else:
        v = "◯ INCONCLUSIVE (CI містить 0, верхня межа ≥ 0.15 R)"
    L.append(f"\n## Вердикт H1 за реєстрацією: **{v}**\n")
    L.append(
        f"- середній R {rr(m)} {ci_r(ci)}, IS {rr(mi)}, OOS {rr(mo)}, Холм p {adj['H1']:.3f}, стрес 3 в.п. {rr(stress['mean'] if stress else None)}, MDE {s1['mde']:.3f} R"
    )
    (cache / "trades_h1.json").write_text(json.dumps(base))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "full"), default="coverage")
    ap.add_argument("--src-dir", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--closes-dir", default="~/.cache/tradedesk-agents/assistant_recs")
    ap.add_argument("--klines-dir", default="~/.cache/tradedesk-agents/macro_regimes/klines")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/p3_trend_spread")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.src_dir).expanduser(), Path(a.closes_dir).expanduser(), Path(a.klines_dir).expanduser(), t0)
    labels = labels_for(D, BASE["weekday"])
    L = [f"# P3: 200-денний тренд → дебетовий спред, BTC (stage {a.stage}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n"]
    cov, base = coverage(L, D, labels)
    sanity(L, D, labels, base)
    log("coverage and sanity done", t0)
    if a.stage == "full":
        full(L, D, labels, cov, base, cache, t0)
    text = "\n".join(L)
    print(text)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")
    log("done", t0)


if __name__ == "__main__":
    main()
