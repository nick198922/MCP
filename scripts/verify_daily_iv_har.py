"""Daily Deribit BTC ATM IV vs a HAR-RV forecast from 5-minute Binance bars (IDEA-20261023-daily-iv-vs-intraday-vol-forecast).

The pre-registration is in the idea card ("Попередня реєстрація"). Data are read from caches only, no network:
  * BTC option prints, 24 h before 09:00 UTC per day: ``~/.cache/tradedesk-agents/put_skew/trades24``;
  * per-day entry quotes of the event-day study (strike, forward ratio, premium): ``event_straddle/quotes_v1.json``;
  * Deribit delivery prices: ``put_skew/delivery_btc_usd.json``;
  * 5-minute BTCUSDT Binance klines: ``session_stop_hunt/k5m/BTCUSDT/YYYY-MM.json``.
Per-day ATM IV summaries (all prints in [07:00, 09:00]) are cached in ``--cache-dir`` (``ivprints_v1.json``).

Stages: ``gate`` (calibration regression, the kill gate) and ``main`` (tercile slice; refuses to run unless the
gate file says the gate passed).
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import resource
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.event_straddle import DAY_MS, iso_week
from tradedesk.analytics.iv_har import (
    annualize,
    atm_iv_prints,
    har_features,
    har_fit,
    har_forecast_var,
    huber_cluster,
    iv_summary,
    ols_cluster,
    realized_var,
    wing_strikes,
)

START = datetime(2021, 1, 1, tzinfo=UTC)
END = datetime(2026, 10, 13, tzinfo=UTC)
SPLIT = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_H = 23.0  # D 09:00 -> D+1 08:00
VERSION = "v1"
OUT: list[str] = []


def ms(d: datetime | date) -> int:
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(d.timestamp() * 1000)


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def say(s: str = "") -> None:
    print(s, flush=True)
    OUT.append(s)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


START_MS, END_MS, SPLIT_MS = ms(START), ms(END), ms(SPLIT)


# ----------------------------------------------------------------------------- data


def load_closes(src: Path) -> dict[int, float]:
    out: dict[int, float] = {}
    for f in sorted(src.glob("*.json")):
        for b in json.loads(f.read_text()) or []:
            out[int(b[0]) // 1000] = float(b[4])
    return out


def _iv_day(args: tuple[str, dict[str, Any]]) -> tuple[int, dict[str, Any] | None]:
    path, q = args
    trades = json.loads(gzip.decompress(Path(path).read_bytes()))["trades"]
    s = iv_summary(atm_iv_prints(trades, q))
    w = wing_strikes(trades, q)
    return q["day"], (dict(s, wings=w["covered"]) if s else {"n_iv": 0, "wings": w["covered"]})


def load_iv(trades_dir: Path, quotes: dict[int, dict[str, Any]], cache: Path) -> dict[int, dict[str, Any]]:
    cp = cache / f"ivprints_{VERSION}.json"
    jobs = []
    for d, q in sorted(quotes.items()):
        p = trades_dir / f"{ymd(d)}.json.gz"
        if q.get("covered") and p.exists():
            jobs.append((str(p), q))
    if cp.exists():
        blob = json.loads(cp.read_text())
        if blob.get("n_jobs") == len(jobs):
            return {int(k): v for k, v in blob["iv"].items()}
    say(f"iv: processing {len(jobs)} day files")
    t0 = time.perf_counter()
    out: dict[str, Any] = {}
    with ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1))) as ex:
        for i, (d, s) in enumerate(ex.map(_iv_day, jobs, chunksize=8)):
            out[str(d)] = s
            if i % 250 == 0 or i == len(jobs) - 1:
                print(f"  {i + 1}/{len(jobs)} in {time.perf_counter() - t0:.0f}s, peak RSS {rss_mb():.0f} MB", flush=True)
    cp.write_text(json.dumps({"n_jobs": len(jobs), "iv": out}))
    return {int(k): v for k, v in out.items()}


def build_rows(a: argparse.Namespace) -> tuple[list[dict[str, Any]], Counter[str]]:
    home = Path("~/.cache/tradedesk-agents").expanduser()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    quotes = {int(k): v for k, v in json.loads(Path(a.quotes).expanduser().read_text())["quotes"].items() if v}
    iv = load_iv(Path(a.trades_dir).expanduser(), quotes, cache)
    delivery = {
        ms(date.fromisoformat(r["date"])): float(r["delivery_price"])
        for r in json.loads((home / "put_skew/delivery_btc_usd.json").read_text())
    }
    closes = load_closes(Path(a.k5m_dir).expanduser())
    last_bar = max(closes)
    say(
        f"5m bars: {len(closes)}, {datetime.fromtimestamp(min(closes), UTC):%Y-%m-%d} .. {datetime.fromtimestamp(last_bar, UTC):%Y-%m-%d %H:%M}; peak RSS {rss_mb():.0f} MB"
    )
    rows, why = [], Counter()
    for d in range(START_MS, END_MS + 1, DAY_MS):
        q = quotes.get(d)
        if not q or not q.get("covered"):
            why["no ATM quote"] += 1
            continue
        s = iv.get(d)
        if not s or not s.get("n_iv"):
            why["no ATM mark IV"] += 1
            continue
        st = delivery.get(d + DAY_MS)
        if not st:
            why["no delivery D+1"] += 1
            continue
        t0s = q["t0"] // 1000
        if t0s - 300 > last_bar:
            why["after 5m cache end"] += 1
            continue
        f = har_features(closes, t0s)
        if f is None:
            why["thin 5m features"] += 1
            continue
        tgt = realized_var(closes, t0s, q["expiry"] // 1000) if q["expiry"] // 1000 - 300 <= last_bar else None
        bin0 = closes.get(t0s - 300)
        rows.append(
            {
                "day": d,
                "week": iso_week(d),
                "oos": d >= SPLIT_MS,
                "wd": datetime.fromtimestamp(d / 1000, UTC).weekday(),
                "S0": q["spot"],
                "ST": st,
                "K": q["K"],
                "T": q["T"],
                "prem": q["premium"] / q["spot"],
                "r": math.log(st / q["spot"]),
                "iv": s["iv_mean"],
                "iv_last": s["iv_last"],
                "iv_trade": s["iv_trade_mean"],
                "iv_eq": (q["iv_call"] + q["iv_put"]) / 2,
                "n_iv": s["n_iv"],
                "wings": s["wings"],
                "f": f,
                "tgt": tgt,
                "bin0": bin0,
            }
        )
    return rows, why


# ----------------------------------------------------------------------------- formatting


def coef_line(label: str, r: dict[str, Any], j: int = 2) -> str:
    return (
        f"| {label} | {r['n']} | {r['clusters']} | {r['coef'][1]:+.3f} ({r['se'][1]:.3f}) | "
        f"**{r['coef'][j]:+.3f}** ({r['se'][j]:.3f}) | {r['p'][j]:.2g} | {r['r2']:.3f} |"
    )


def regressions(rows: list[dict[str, Any]], ivk: str, kind: str) -> dict[str, Any]:
    cl = [x["week"] for x in rows]
    if kind == "ln":
        y = [math.log(abs(x["r"])) for x in rows]
        X = [[1.0, math.log(x[ivk]), math.log(x["sig"])] for x in rows]
        return ols_cluster(y, X, cl)
    if kind == "huber":
        y = [math.log(abs(x["r"])) for x in rows]
        X = [[1.0, math.log(x[ivk]), math.log(x["sig"])] for x in rows]
        return huber_cluster(y, X, cl)
    if kind == "r2":
        y = [x["r"] ** 2 for x in rows]
        X = [[1.0, x[ivk] ** 2 * x["T"], x["var_hat"]] for x in rows]
        return ols_cluster(y, X, cl)
    if kind == "abs":
        y = [abs(x["r"]) for x in rows]
        X = [[1.0, x[ivk] * math.sqrt(x["T"]), math.sqrt(x["var_hat"])] for x in rows]
        return ols_cluster(y, X, cl)
    raise ValueError(kind)


# ----------------------------------------------------------------------------- stages


def stage_gate(a: argparse.Namespace) -> None:
    rows, why = build_rows(a)
    total = (END_MS - START_MS) // DAY_MS + 1
    say(
        f"\n## Покриття\n\nДнів у періоді {total}; у вибірці {len(rows)} ({len(rows) / total:.1%}); IS {sum(not x['oos'] for x in rows)}, OOS {sum(x['oos'] for x in rows)}."
    )
    say("Причини непокриття: " + (", ".join(f"{k} {v}" for k, v in why.most_common()) or "немає") + ".")
    say(
        f"Перший / останній день: {ymd(rows[0]['day'])} / {ymd(rows[-1]['day'])}. Принтів IV на день: медіана {statistics.median(x['n_iv'] for x in rows):.0f}, частка днів з 1 принтом {sum(x['n_iv'] == 1 for x in rows) / len(rows):.1%}."
    )

    # ---- sanity
    say("\n## Перевірки узгодженості\n")
    lv = [abs(x["bin0"] / x["S0"] - 1) for x in rows if x["bin0"]]
    say(
        f"- Binance BTCUSDT close 09:00 проти S0 Deribit: медіана |різниці| {statistics.median(lv) * 100:.3f} %, p99 {sorted(lv)[int(0.99 * len(lv))] * 100:.3f} %, n={len(lv)}."
    )
    ratio = [abs(x["r"]) / (math.sqrt(2 / math.pi) * x["iv_eq"] * math.sqrt(x["T"])) for x in rows]
    say(f"- Еталон (звіт event-day: |ln(S_T/S0)| / закладений рух ≈ 0.90): середнє {statistics.mean(ratio):.3f}.")
    rp = [abs(x["r"]) / x["prem"] for x in rows]
    rpw = [abs(x["r"]) / x["prem"] for x in rows if x["wd"] < 5]
    wk = [x for x in rows if x["wd"] < 5]
    say(
        f"  Як у звіті event-day (відношення середніх, будні входи): середнє |ln(S_T/S0)| {statistics.mean(abs(x['r']) for x in wk) * 100:.2f} % / "
        f"середня премія {statistics.mean(x['prem'] for x in wk) * 100:.2f} % = {statistics.mean(abs(x['r']) for x in wk) / statistics.mean(x['prem'] for x in wk):.3f}."
    )
    say(
        f"  Середнє відношень |ln(S_T/S0)| / премія стреддла: усі дні {statistics.mean(rp):.3f}, будні входи {statistics.mean(rpw):.3f}, вихідні входи {statistics.mean(abs(x['r']) / x['prem'] for x in rows if x['wd'] >= 5):.3f}."
    )
    tv = [x for x in rows if x["tgt"]]
    gap = [abs(math.log(x["ST"] / x["S0"])) for x in tv]
    say(f"- Днів із ціллю RV (вікно 09:00 → 08:00 у кеші 5m): {len(tv)}; середній |r| {statistics.mean(gap) * 100:.2f} %.")
    say(
        "\n| Рік | n | IV (основна), середнє | RV-вол цілі (річн.), середнє | √RV_d (річн.), середнє | IV/RV-вол, медіана |\n|---|---|---|---|---|---|"
    )
    for y in range(2021, 2027):
        ys = [x for x in tv if ymd(x["day"])[:4] == str(y)]
        if ys:
            rvv = [annualize(x["tgt"], WINDOW_H) for x in ys]
            say(
                f"| {y} | {len(ys)} | {statistics.mean(x['iv'] for x in ys):.3f} | {statistics.mean(rvv):.3f} | "
                f"{statistics.mean(math.sqrt(x['f']['rv_d'] * 365) for x in ys):.3f} | {statistics.median(x['iv'] / v for x, v in zip(ys, rvv, strict=True) if v > 0):.2f} |"
            )

    # ---- HAR on IS (frozen)
    is_t = [x for x in tv if not x["oos"]]
    fit = har_fit([x["f"] for x in is_t], [x["tgt"] for x in is_t])
    b = fit["coef"]
    say(
        f"\n## HAR-RV (лог, МНК на IS, заморожено)\n\nln RV = {b[0]:+.3f} {b[1]:+.3f}·ln RV_d {b[2]:+.3f}·ln RV_w {b[3]:+.3f}·ln RV_m; s² = {fit['s2']:.3f}, R² IS = {fit['r2']:.3f}, n = {fit['n']}."
    )
    for x in rows:
        x["var_hat"] = har_forecast_var(fit, x["f"])
        x["sig"] = annualize(x["var_hat"], WINDOW_H)
    oos_t = [x for x in tv if x["oos"]]
    if oos_t:
        e = [math.log(x["tgt"]) - math.log(x["var_hat"]) + fit["s2"] / 2 for x in oos_t]
        yv = [math.log(x["tgt"]) for x in oos_t]
        my = statistics.mean(yv)
        r2o = 1 - sum(v * v for v in e) / sum((v - my) ** 2 for v in yv)
        say(f"R² OOS (той самий прогноз, ln RV) = {r2o:.3f}, n = {len(oos_t)}; середня помилка ln {statistics.mean(e):+.3f}.")
    ivt = [math.log(x["iv"] ** 2 * x["T"]) for x in oos_t]
    if oos_t:
        yv = [math.log(x["tgt"]) for x in oos_t]
        c = ols_cluster(yv, [[1.0, v] for v in ivt], [x["week"] for x in oos_t])
        say(f"Довідково: ln RV_ціль на ln(IV²·T) на OOS: R² = {c['r2']:.3f} (наскільки IV сама прогнозує RV).")
    lg = [math.log(x["iv"] / x["sig"]) for x in rows]
    say(
        f"g = ln(IV/σ̂): середнє {statistics.mean(lg):+.3f}, SD {statistics.stdev(lg):.3f}; corr(ln IV, ln σ̂) = {statistics.correlation([math.log(x['iv']) for x in rows], [math.log(x['sig']) for x in rows]):.3f}."
    )

    # ---- gate
    say("\n## Шлюз: ln|r| = a + b·ln IV + c·ln σ̂ (SE кластеризовані за ISO-тижнями)\n")
    say("| Версія | n | тижнів | b (SE) | c (SE) | p(c) | R² |\n|---|---|---|---|---|---|---|")
    res: dict[str, Any] = {}
    for per, sub in (("IS", [x for x in rows if not x["oos"]]), ("OOS", [x for x in rows if x["oos"]])):
        r = regressions(sub, "iv", "ln")
        res[per] = r
        say(coef_line(f"**основна** {per}: ln\\|r\\|, IV = середнє mark усіх принтів", r))
    is_ok = res["IS"]["coef"][2] > 0 and res["IS"]["p"][2] < 0.01
    oos_ok = res["OOS"]["coef"][2] > 0
    passed = is_ok and oos_ok
    say(
        f"\nШлюз (зареєстровано: c > 0, p < 0.01 на IS і c > 0 на OOS): IS {'так' if is_ok else 'НІ'}, OOS {'так' if oos_ok else 'НІ'} → **{'ПРОЙДЕНО' if passed else 'НЕ ПРОЙДЕНО'}**.\n"
    )

    say("Звітні версії (усі зареєстровані, показано всі):\n")
    say("| Версія | n | тижнів | b (SE) | c (SE) | p(c) | R² |\n|---|---|---|---|---|---|---|")
    fragile = False
    for per, sub in (("IS", [x for x in rows if not x["oos"]]), ("OOS", [x for x in rows if x["oos"]])):
        for kind, lab in (("huber", "ln\\|r\\|, Хубер"), ("r2", "r² на IV²T, σ̂²T"), ("abs", "\\|r\\| на IV√T, σ̂√T")):
            r = regressions(sub, "iv", kind)
            say(coef_line(f"{lab} {per}", r))
            if per == "IS" and kind in ("huber", "r2") and r["coef"][2] <= 0:
                fragile = True
        for ivk, lab in (("iv_last", "IV = остання печатка"), ("iv_trade", "IV = середнє trade IV"), ("iv_eq", "IV = entry_quote")):
            sub2 = [x for x in sub if x[ivk]]
            say(coef_line(f"ln\\|r\\|, {lab} {per}", regressions(sub2, ivk, "ln")))
        wk = [x for x in sub if x["wd"] < 5]
        say(coef_line(f"ln\\|r\\|, лише будні входи {per}", regressions(wk, "iv", "ln")))
    say(f"\nКрихкість (Хубер або r² з c ≤ 0 на IS): {'так' if fragile else 'ні'}.")

    # old (spurious) form, illustration only
    say("\nІлюстрація (стара форма шлюзу, без висновку): нахил ln(|r|/премія) на g.\n")
    for per, sub in (("IS", [x for x in rows if not x["oos"]]), ("OOS", [x for x in rows if x["oos"]])):
        y = [math.log(abs(x["r"]) / x["prem"]) for x in sub]
        X = [[1.0, math.log(x["iv"] / x["sig"])] for x in sub]
        r = ols_cluster(y, X, [x["week"] for x in sub])
        say(f"- {per}: нахил {r['coef'][1]:+.3f} (SE {r['se'][1]:.3f}), n={r['n']}.")
    # power actually achieved
    r = res["IS"]
    say(f"\nПотужність шлюзу (факт): SE(c) IS = {r['se'][2]:.3f} → MDE (p<0.01, 80 %) ≈ {3.4 * r['se'][2]:.2f}.")

    gate = {"passed": passed, "fragile": fragile, "is": res["IS"], "oos": res["OOS"], "har": fit}
    cache = Path(a.cache_dir).expanduser()
    (cache / "gate.json").write_text(json.dumps(gate, indent=1))
    say(f"\npeak RSS {rss_mb():.0f} MB")
    Path(a.out).expanduser().write_text("\n".join(OUT) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("gate", "main"), default="gate")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/daily_iv_har")
    ap.add_argument("--trades-dir", default="~/.cache/tradedesk-agents/put_skew/trades24")
    ap.add_argument("--quotes", default="~/.cache/tradedesk-agents/event_straddle/quotes_v1.json")
    ap.add_argument("--k5m-dir", default="~/.cache/tradedesk-agents/session_stop_hunt/k5m/BTCUSDT")
    ap.add_argument("--out", default="~/.cache/tradedesk-agents/daily_iv_har/gate.md")
    a = ap.parse_args()
    if a.stage == "gate":
        stage_gate(a)
        return
    g = Path(a.cache_dir).expanduser() / "gate.json"
    if not g.exists() or not json.loads(g.read_text())["passed"]:
        say("main stage refused: the registered gate did not pass (see gate.json)")
        return
    say("main stage: not implemented until the gate passes")


if __name__ == "__main__":
    main()
