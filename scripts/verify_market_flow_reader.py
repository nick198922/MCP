"""Verify IDEA-20261028-market-flow-reader, step 3: does a model on all flow metrics together beat price-only models?

Stages (``uv run python scripts/verify_market_flow_reader.py <stage> --cache-dir DIR``):
    fetch   archive 1h taker klines (USD-M, spot), funding, daily metrics (5-min OI) of BTCUSDT/ETHUSDT and Coinbase
            1h candles BTC-USD/ETH-USD -> per-file JSON cache (``null`` = file not in the archive). ``--sample N``
            fetches only N metrics days per symbol (timing).
    sanity  coverage, price levels across venues, units, funding intervals, a known number (no targets).
    full    registered run (card section «Попередня реєстрація»): tasks (a) (b) (c) x BTC/ETH, Holm, placebo,
            robustness -> ``full.json`` in the cache dir, compact tables on stdout. ``--gb`` adds the boosting check.

Network only through runtime providers (``rt.binance_vision``, ``rt.coinbase``); Coinalyze is used only to report
whether a key is present (no liquidation feature without one, as registered).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import resource
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_liquidation_flush as vlf  # noqa: E402  (per-file JSON Fetcher)

from tradedesk.analytics import flow_model as fm  # noqa: E402
from tradedesk.analytics.oi_flush import archive_obs_times  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.server import build_server  # noqa: E402

DEFAULT_CACHE = Path.home() / ".cache/tradedesk-agents/market_flow_reader"
SYMS = {"BTC": ("BTCUSDT", "BTC-USD"), "ETH": ("ETHUSDT", "ETH-USD")}
FIRST_MONTH, LAST_MONTH = "2020-12", "2026-09"
H = fm.H_MS


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


T0 = ms("2020-12-01")
T_END = ms("2026-10-01")
N = (T_END - T0) // H
TRAIN_FROM = ms("2021-02-01")
OOS_FROM = ms("2025-03-01")
OOS_SPLIT = ms("2025-12-01")  # robustness: two OOS halves (9 + 10 months)
LAST_T = T_END - 1


def rss() -> str:
    return f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.exists() else None


def months(a: str, b: str) -> list[str]:
    out, y, m = [], int(a[:4]), int(a[5:7])
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def days(a: str, b: str) -> list[str]:
    d0, d1 = datetime.fromisoformat(a), datetime.fromisoformat(b)
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]


# ---------------------------------------------------------------- fetch


async def stage_fetch(cache: Path, sample: int | None) -> None:
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(f"coinalyze key present: {rt.coinalyze.has_key}", flush=True)
    try:
        fx = vlf.Fetcher(rt, cache, workers=16)
        bv = rt.binance_vision
        jobs: list[tuple[Path, Any]] = []
        for sym, _cb in SYMS.values():
            for m in months(FIRST_MONTH, LAST_MONTH):
                y, mo = int(m[:4]), int(m[5:])

                async def um(s: str = sym, y: int = y, mo: int = mo) -> Any:
                    return (await bv.um_klines_taker_monthly(s, "1h", y, mo)).data

                async def sp(s: str = sym, y: int = y, mo: int = mo) -> Any:
                    return (await bv.spot_klines_taker_monthly(s, "1h", y, mo)).data

                async def fu(s: str = sym, y: int = y, mo: int = mo) -> Any:
                    return (await bv.um_funding_monthly(s, y, mo)).data

                jobs += [(cache / "um1h" / sym / f"{m}.json", um), (cache / "spot1h" / sym / f"{m}.json", sp)]
                jobs.append((cache / "fund" / sym / f"{m}.json", fu))
        await fx.many("klines+funding", jobs)
        print(rss(), flush=True)

        cbjobs = []
        for _sym, prod in SYMS.values():
            for m in months(FIRST_MONTH, LAST_MONTH):
                a = ms(f"{m}-01")
                y, mo = int(m[:4]), int(m[5:])
                b = ms(f"{y + (mo == 12):04d}-{(mo % 12) + 1:02d}-01")

                async def cb(p: str = prod, a: int = a, b: int = b) -> Any:
                    f = await rt.coinbase.candles_range(p, 3600, a, b, max_pages=4)
                    if not f.data["complete"]:
                        raise ProviderError("coinbase", "backend_error", "incomplete month")  # not cached; rerun retries
                    return [[int(r[0]) * 1000, float(r[4])] for r in f.data["rows"]]  # [open_ms, close]

                cbjobs.append((cache / "cb1h" / prod / f"{m}.json", cb))
        await fx.many("coinbase", cbjobs)
        print(rss(), flush=True)

        ds = days(f"{FIRST_MONTH}-01", "2026-09-30")
        if sample:
            ds = ds[:: max(1, len(ds) // sample)][:sample]
        mjobs = []
        for d in ds:
            for sym, _cb in SYMS.values():

                async def me(s: str = sym, d: str = d) -> Any:
                    return [[int(r[0]), r[1]] for r in (await bv.um_metrics_daily(s, d)).data]  # [create_ms, OI coins]

                mjobs.append((cache / "metrics" / sym / f"{d}.json", me))
        t0 = time.time()
        await fx.many("metrics", mjobs)
        print(f"FETCH COMPLETE metrics {len(mjobs)} files in {time.time() - t0:.0f}s; {rss()}", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- panel


def grid_klines(cache: Path, kind: str, sym: str) -> dict[str, np.ndarray]:
    g = {k: np.full(N, np.nan) for k in ("h", "l", "c", "qv", "tbq")}
    for m in months(FIRST_MONTH, LAST_MONTH):
        rows = rd(cache / kind / sym / f"{m}.json")
        if not rows:
            continue
        a = np.asarray(rows, dtype=float)
        i = ((a[:, 0] - T0) // H).astype(np.int64)
        ok = (i >= 0) & (i < N) & ((a[:, 0] - T0) % H == 0)
        for k, col in (("h", 2), ("l", 3), ("c", 4), ("qv", 6), ("tbq", 8)):
            g[k][i[ok]] = a[ok, col]
    return g


def load_panel(cache: Path, asset: str) -> dict[str, Any]:
    sym, prod = SYMS[asset]
    perp = grid_klines(cache, "um1h", sym)
    spot = grid_klines(cache, "spot1h", sym)
    cb = np.full(N, np.nan)
    for m in months(FIRST_MONTH, LAST_MONTH):
        rows = rd(cache / "cb1h" / prod / f"{m}.json")
        if rows:
            a = np.asarray(rows, dtype=float)
            i = ((a[:, 0] - T0) // H).astype(np.int64)
            ok = (i >= 0) & (i < N)
            cb[i[ok]] = a[ok, 1]
    fr: list[list[float]] = []
    for m in months(FIRST_MONTH, LAST_MONTH):
        fr += rd(cache / "fund" / sym / f"{m}.json") or []
    fa = np.asarray(sorted(fr), dtype=float).reshape(-1, 3)
    ct: list[int] = []
    oi: list[float] = []
    n_metric_days = 0
    for d in days(f"{FIRST_MONTH}-01", "2026-09-30"):
        rows = rd(cache / "metrics" / sym / f"{d}.json")
        if rows:
            n_metric_days += 1
            for c, v in rows:
                if v is not None and v == v and v > 0:
                    ct.append(int(c))
                    oi.append(float(v))
    c_arr = np.asarray(ct, dtype=np.int64)
    o = np.argsort(c_arr, kind="stable")
    obs = archive_obs_times(c_arr[o])
    dec = T0 + (np.arange(N, dtype=np.int64) + 1) * H
    oi_h = fm.asof(obs, np.asarray(oi)[o], dec, max_age_ms=2 * H)
    fund = fm.funding_features(fa[:, 0].astype(np.int64), fa[:, 2], fa[:, 1], dec)
    return {"perp": perp, "spot": spot, "cb": cb, "oi": oi_h, "fund": fund, "fund_raw": fa, "dec": dec, "n_metric_days": n_metric_days}


def build(P: dict[str, Any], flow_shift: int = 0) -> dict[str, Any]:
    """Feature blocks and targets on the hourly grid; ``flow_shift`` rolls the flow block F (placebo)."""
    perp, spot = P["perp"], P["spot"]
    pf = fm.price_features(perp["c"], perp["h"], perp["l"])
    har = fm.har_features(perp["c"])
    prem = fm.coinbase_premium(P["cb"], spot["c"])
    F = fm.flow_features(
        perp_qv=perp["qv"],
        perp_tbq=perp["tbq"],
        spot_qv=spot["qv"],
        spot_tbq=spot["tbq"],
        oi=P["oi"],
        fund_last=P["fund"]["fund_last"],
        fund_3d=P["fund"]["fund_3d"],
        premium=prem,
        r24=pf["r24"],
    )
    if flow_shift:
        F = {k: fm.circular_shift(v, flow_shift) for k, v in F.items()}
        F["r24_x_doi24"] = pf["r24"] * F["doi24"]  # interaction from the shifted OI and the true price
    return {
        "P": pf,
        "HAR": har,
        "F": F,
        "y_sign": fm.forward_logret(perp["c"], 24),
        "y_lrv": fm.forward_log_rv(perp["c"], 24),
    }


def mat(block: dict[str, np.ndarray], keys: list[str] | None = None) -> np.ndarray:
    ks = list(block) if keys is None else keys
    return np.stack([block[k] for k in ks], axis=1)


def week_id(t_ms: np.ndarray) -> np.ndarray:
    return (np.asarray(t_ms) - 4 * 86_400_000) // (7 * 86_400_000)  # Monday-start weeks


# ---------------------------------------------------------------- sanity


def stage_sanity(cache: Path) -> None:
    _, rt = build_server()
    print(f"coinalyze key present: {rt.coinalyze.has_key}")
    win = slice((TRAIN_FROM - T0) // H, N)
    for a in SYMS:
        P = load_panel(cache, a)
        perp, spot = P["perp"], P["spot"]
        cov = {
            k: round(float(np.isfinite(v[win]).mean()), 4)
            for k, v in (
                ("perp_c", perp["c"]),
                ("perp_tbq", perp["tbq"]),
                ("spot_c", spot["c"]),
                ("spot_tbq", spot["tbq"]),
                ("cb_c", P["cb"]),
                ("oi", P["oi"]),
                ("fund_last", P["fund"]["fund_last"]),
            )
        }
        with np.errstate(all="ignore"):
            r_sp = spot["c"] / perp["c"] - 1
            r_cb = P["cb"] / spot["c"] - 1
            share = perp["tbq"] / perp["qv"]
        ivals = np.unique(P["fund_raw"][:, 1], return_counts=True)
        oi_usd = P["oi"] * perp["c"]
        c = perp["c"]
        y25 = slice((ms("2025-01-01") - T0) // H, (ms("2026-01-01") - T0) // H)
        k = int(np.nanargmax(c[y25])) + y25.start
        print(f"== {a}: coverage 2021-02..2026-09 {cov}; metrics days {P['n_metric_days']}")
        print(
            f"   spot/perp-1 median {np.nanmedian(r_sp):+.5f} p1/p99 {np.nanpercentile(r_sp, 1):+.4f}/{np.nanpercentile(r_sp, 99):+.4f}; coinbase/spot-1 median {np.nanmedian(r_cb):+.5f} p1/p99 {np.nanpercentile(r_cb, 1):+.4f}/{np.nanpercentile(r_cb, 99):+.4f}"
        )
        print(
            f"   perp taker-buy share median {np.nanmedian(share):.4f} (expect ~0.5); funding intervals {dict(zip(ivals[0].tolist(), ivals[1].tolist(), strict=True))}"
        )
        print(
            f"   OI coins median {np.nanmedian(P['oi']):.0f}; OI USD 2026-09-30 {oi_usd[N - 1] / 1e9:.2f} bn; max close 2025 {c[k]:.0f} at {datetime.fromtimestamp((T0 + k * H) / 1000, UTC):%Y-%m-%d %H}"
        )
        print(f"   funding annualised median {np.nanmedian(P['fund']['fund_last']):+.4f}; {rss()}")


# ---------------------------------------------------------------- full


def lin_logit(X: np.ndarray, y: np.ndarray) -> Any:
    return fm.fit_logistic(X, y, lam=1.0)


def lin_ridge(X: np.ndarray, y: np.ndarray) -> Any:
    return fm.fit_ridge(X, y, lam=1.0)


def gb_logit(X: np.ndarray, y: np.ndarray) -> Any:
    return fm.fit_gb(X, y, loss="logistic")


def gb_sq(X: np.ndarray, y: np.ndarray) -> Any:
    return fm.fit_gb(X, y, loss="squared")


def pred_gb(m: Any, X: np.ndarray) -> np.ndarray:
    return m.predict(X)


MODELS = {
    ("lin", "cls"): (lin_logit, fm.predict_logistic),
    ("lin", "reg"): (lin_ridge, fm.predict_linear),
    ("gb", "cls"): (gb_logit, pred_gb),
    ("gb", "reg"): (gb_sq, pred_gb),
}
TEST_MONTHS = fm.month_starts(OOS_FROM, LAST_T)


def design(B: dict[str, Any], task: str, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """Rows, base / full matrices, target, times for one task (rows common to both models)."""
    dec = np.asarray(B["dec"])
    Fk = [k for k in B["F"] if k not in drop]
    if task == "a":
        Xb, Xf = mat(B["P"]), np.hstack([mat(B["P"]), mat(B["F"], Fk)])
        y = (B["y_sign"] > 0).astype(float)
        yok = np.isfinite(B["y_sign"])
        res = dec + 24 * H
        rows = np.arange(dec.size)
    elif task == "b":
        Xb = mat(B["HAR"])
        absf = np.stack([np.abs(B["F"][k]) for k in fm.VOL_ABS if k not in drop], axis=1)
        Xf = np.hstack([Xb, mat(B["F"], Fk), absf])
        y = B["y_lrv"]
        yok = np.isfinite(y)
        res = dec + 24 * H
        rows = np.arange(dec.size)
    else:
        ev = B["events"]
        rows = np.asarray([e.i for e in ev], dtype=np.int64)
        Xb = mat(B["P"])[rows]
        Xf = np.hstack([mat(B["P"]), mat(B["F"], Fk)])[rows]
        y = np.asarray([e.up for e in ev], dtype=float)
        yok = np.ones(rows.size, bool)
        res = dec[np.asarray([e.resolve_i for e in ev], dtype=np.int64)] if ev else np.zeros(0, np.int64)
        t = dec[rows]
        ok = np.all(np.isfinite(Xf), axis=1) & (t >= TRAIN_FROM)
        return {"Xb": Xb[ok], "Xf": Xf[ok], "y": y[ok], "t": t[ok], "res": res[ok]}
    t = dec[rows]
    ok = np.all(np.isfinite(Xf), axis=1) & np.all(np.isfinite(Xb), axis=1) & yok & (t >= TRAIN_FROM)
    return {"Xb": Xb[ok], "Xf": Xf[ok], "y": np.nan_to_num(y)[ok], "t": t[ok], "res": res[ok]}


def run_task(D: dict[str, Any], task: str, kind: str = "lin", only_full: bool = False) -> dict[str, np.ndarray]:
    mk = "reg" if task == "b" else "cls"
    fit, pr = MODELS[(kind, mk)]
    out = {"full": fm.walk_forward(D["Xf"], D["y"], D["t"], D["res"], TEST_MONTHS, fit, pr)}
    if not only_full:
        out["base"] = fm.walk_forward(D["Xb"], D["y"], D["t"], D["res"], TEST_MONTHS, fit, pr)
    return out


def metric(task: str) -> Any:
    return {"a": fm.auc, "b": fm.r2_oos, "c": fm.accuracy}[task]


def evaluate(
    D: dict[str, Any], pred: dict[str, np.ndarray], task: str, mask: np.ndarray | None = None, boot: bool = True
) -> dict[str, Any]:
    oos = (D["t"] >= OOS_FROM) & np.isfinite(pred["full"]) & np.isfinite(pred["base"])
    if mask is not None:
        oos &= mask
    y, pf, pb = D["y"][oos], pred["full"][oos], pred["base"][oos]
    f = metric(task)
    out: dict[str, Any] = {"n": int(oos.sum()), "full": f(y, pf), "base": f(y, pb)}
    out["delta"] = out["full"] - out["base"]
    if task != "b":
        out["base_rate_up"] = float(y.mean()) if y.size else float("nan")
    if boot and y.size:

        def stat(ix: np.ndarray) -> float:
            return f(y[ix], pf[ix]) - f(y[ix], pb[ix])

        out["boot"] = fm.block_bootstrap_delta(week_id(D["t"][oos]), stat)
    return out


THRESH = {"a": {"level": 0.53, "delta": 0.02}, "b": {"level": None, "delta": 0.02}, "c": {"level": 0.55, "delta": 0.02}}


def stage_full(cache: Path, out_path: Path, n_placebo: int, with_gb: bool) -> None:
    t_start = time.time()
    res: dict[str, Any] = {"registered": THRESH, "oos": "2025-03-01..2026-09-30", "test_months": len(TEST_MONTHS)}
    rng = np.random.default_rng(20261028)
    for a in SYMS:
        P = load_panel(cache, a)
        B = build(P)
        B["dec"] = P["dec"]
        daily = np.flatnonzero(P["dec"] % 86_400_000 == 0)
        B["events"] = fm.range_exit_events(P["perp"]["h"], P["perp"]["l"], daily)
        ra: dict[str, Any] = {}
        Ds = {t: design(B, t) for t in "abc"}
        for task in "abc":
            D = Ds[task]
            pred = run_task(D, task)
            ev = evaluate(D, pred, task)
            ev["n_train_first"] = int((D["res"] < OOS_FROM).sum())
            midnight = D["t"] % 86_400_000 == 0
            ev["midnight_only"] = evaluate(D, pred, task, midnight, boot=False) if task != "c" else None
            ev["half1"] = evaluate(D, pred, task, D["t"] < OOS_SPLIT, boot=False)
            ev["half2"] = evaluate(D, pred, task, D["t"] >= OOS_SPLIT, boot=False)
            if task == "b":  # level-space R2 as an extra view
                oos = (D["t"] >= OOS_FROM) & np.isfinite(pred["full"]) & np.isfinite(pred["base"])
                yl = np.exp(D["y"][oos])
                ev["r2_level"] = {"full": fm.r2_oos(yl, np.exp(pred["full"][oos])), "base": fm.r2_oos(yl, np.exp(pred["base"][oos]))}
            for name, drop in (("no_interaction", ("r24_x_doi24",)), ("no_cb_premium", ("cb_prem",))):
                Dd = design(B, task, drop)
                pd = run_task(Dd, task)
                ev[name] = evaluate(Dd, pd, task, boot=False)
            ra[task] = ev
            print(
                f"[{a}/{task}] n={ev['n']} full={ev['full']:.4f} base={ev['base']:.4f} delta={ev['delta']:+.4f} CI [{ev['boot']['lo']:+.4f},{ev['boot']['hi']:+.4f}] p={ev['boot']['p_le0']:.3f}; {time.time() - t_start:.0f}s {rss()}",
                flush=True,
            )
        # final-month standardised weights of the full linear model for task (a) (interpretation only)
        Da = Ds["a"]
        tr = Da["res"] < TEST_MONTHS[-1]
        Xs = fm.standardize(Da["Xf"][tr])[0]
        w = fm.fit_logistic(Xs, Da["y"][tr])
        names = list(B["P"]) + list(B["F"])
        ra["a"]["weights_last_month"] = {n: round(float(v), 4) for n, v in zip(names, w[:-1], strict=True)}
        # placebo: shift F by k weeks, linear models only, base predictions reused
        plac: dict[str, list[float]] = {t: [] for t in "abc"}
        nweeks = N // 168
        base_pred = {}
        for task in "abc":
            base_pred[task] = run_task(Ds[task], task)["base"]
        for r in range(n_placebo):
            k = int(rng.integers(8, nweeks - 8)) * 168
            Bp = build(P, flow_shift=k)
            Bp["dec"], Bp["events"] = P["dec"], B["events"]
            for task in "abc":
                Dp = design(Bp, task)
                pf = run_task(Dp, task, only_full=True)["full"]
                # base on the placebo rows (row set can differ slightly because shifted F changes NaN positions)
                pb = run_task(Dp, task)["base"] if Dp["y"].size != Ds[task]["y"].size else base_pred[task]
                plac[task].append(evaluate(Dp, {"full": pf, "base": pb}, task, boot=False)["delta"])
            if (r + 1) % 10 == 0:
                print(f"  [{a}] placebo {r + 1}/{n_placebo}; {time.time() - t_start:.0f}s {rss()}", flush=True)
        for task in "abc":
            pv = np.asarray(plac[task])
            ra[task]["placebo"] = {
                "n": int(pv.size),
                "p95": float(np.percentile(pv, 95)) if pv.size else None,
                "mean": float(pv.mean()) if pv.size else None,
                "share_ge_real": float(np.mean(pv >= ra[task]["delta"])) if pv.size else None,
            }
        if with_gb:
            for task in "abc":
                pg = run_task(Ds[task], task, kind="gb")
                ra[task]["gb"] = evaluate(Ds[task], pg, task, boot=True)
                g = ra[task]["gb"]
                print(
                    f"  [{a}/{task}] GB full={g['full']:.4f} base={g['base']:.4f} delta={g['delta']:+.4f} CI [{g['boot']['lo']:+.4f},{g['boot']['hi']:+.4f}]; {time.time() - t_start:.0f}s",
                    flush=True,
                )
        res[a] = ra
    # Holm over 6 + decision
    keys = [(a, t) for a in SYMS for t in "abc"]
    adj = fm.holm([res[a][t]["boot"]["p_le0"] for a, t in keys])
    print("\nasset task   n     full     base    delta   CI95               p_holm  plac_p95  level delta CI holm plac  PASS")
    for (a, t), ph in zip(keys, adj, strict=True):
        e = res[a][t]
        th = THRESH[t]
        g_level = th["level"] is None or e["full"] >= th["level"]
        g_delta = e["delta"] >= th["delta"]
        g_ci = e["boot"]["lo"] > 0
        g_holm = ph < 0.05
        g_plac = e["placebo"]["p95"] is not None and e["delta"] > e["placebo"]["p95"]
        g_n = t != "c" or e["n"] >= 100
        e["p_holm"] = ph
        e["gates"] = {"level": g_level, "delta": g_delta, "ci": g_ci, "holm": g_holm, "placebo": g_plac, "n": g_n}
        e["pass"] = all(e["gates"].values())
        print(
            f"{a:5} {t:4} {e['n']:6d} {e['full']:8.4f} {e['base']:8.4f} {e['delta']:+8.4f} [{e['boot']['lo']:+.4f},{e['boot']['hi']:+.4f}] {ph:7.3f} {e['placebo']['p95']:+9.4f}  "
            f"{'Y' if g_level else 'n':>5} {'Y' if g_delta else 'n':>5} {'Y' if g_ci else 'n':>2} {'Y' if g_holm else 'n':>4} {'Y' if g_plac else 'n':>4}  {'PASS' if e['pass'] else '-'}"
        )
    print("\nrobustness (delta): midnight-only | OOS half1 | half2 | no interaction | no CB premium | GB")
    for a, t in keys:
        e = res[a][t]

        def d(x: Any) -> str:
            return f"{x['delta']:+.4f}(n={x['n']})" if x else "   —   "

        print(
            f"{a} {t}: {d(e['midnight_only'])} | {d(e['half1'])} | {d(e['half2'])} | {d(e['no_interaction'])} | {d(e['no_cb_premium'])} | {d(e.get('gb'))}"
        )
    for a in SYMS:
        print(
            f"{a} (b) level-space R2 full/base: {res[a]['b']['r2_level']['full']:.4f}/{res[a]['b']['r2_level']['base']:.4f}; (c) up-share OOS {res[a]['c']['base_rate_up']:.3f}"
        )
        print(f"{a} (a) weights last month: {res[a]['a']['weights_last_month']}")
    res["n_pass"] = sum(res[a][t]["pass"] for a, t in keys)
    print(f"\nPASSED: {res['n_pass']} of 6; {time.time() - t_start:.0f}s {rss()}")
    out_path.write_text(json.dumps(res, indent=1, default=float))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["fetch", "sanity", "full"])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--placebo", type=int, default=50)
    ap.add_argument("--gb", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.sample))
    elif a.stage == "sanity":
        stage_sanity(a.cache_dir)
    else:
        stage_full(a.cache_dir, a.out or a.cache_dir / "full.json", a.placebo, a.gb)


if __name__ == "__main__":
    main()
