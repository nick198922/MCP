"""BTC 15-minute shock -> lagging altcoins (IDEA-20261025-btc-shock-alt-lag).

Pre-registered in the idea card ("Попередня реєстрація") before any alt return was computed:
  event    = BTCUSDT |r15| >= 1.2 % and >= 4 sigma (30-day SD of overlapping r15 strictly before the bar), first bar of
             the condition, 2 h cooldown; event bars in [2021-12-01, 2026-10-01);
  universe = registered top-60 alts by turnover over the 30 days before the event day (u=1), terciles 1-20/21-40/41-60;
             primary when its bottom-tercile pair coverage is >= 90 %. Otherwise amendment 1 (after stage 0, before any
             return): the point-in-time monthly top-50 without BTC (u=0), terciles by thirds; the other one is a
             sensitivity row;
  entry    = open of bar i+2 (5 min after the signal), exit 60 min later; i+1 only as an upper bound;
  residual = s * (r_alt - beta * r_btc), beta = 30-day 5m OLS strictly before the event bar;
  costs    = 0.32 % round trip on alt-basket notional (stress 0.62 %); day-clustered bootstrap; Holm over H1-H3.

Reads the 5m/1d kline cache of the liquidation-flush study (data.binance.vision via rt.binance_vision, delisted
symbols included). Only ``--stage fetch`` uses the network (rt.binance_vision, the files listed by ``--stage missing``).

  uv run python scripts/verify_btc_shock_alt_lag.py --stage compute
  uv run python scripts/verify_btc_shock_alt_lag.py --stage missing     # writes missing_files.json
  uv run python scripts/verify_btc_shock_alt_lag.py --stage fetch       # downloads them into the kline cache
  uv run python scripts/verify_btc_shock_alt_lag.py --stage compute     # again, with the full data
  uv run python scripts/verify_btc_shock_alt_lag.py --stage stage0
  uv run python scripts/verify_btc_shock_alt_lag.py --stage report --out ~/.cache/tradedesk-agents/btc_shock_alt_lag/report.md
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_liquidation_flush as vlf  # noqa: E402  (shared kline cache helpers of the liquidation-flush study)

from tradedesk.analytics.btc_shock_lag import (  # noqa: E402
    BARS_30D,
    DAY_MS,
    FIVE_MIN_MS,
    HOUR_MS,
    adverse_excursion,
    beta_from_closes,
    beta_window,
    cluster_se,
    detect_shocks,
    lag_return,
    max_abs_log_jump,
    mde,
    placebo_times,
    post_entry_return,
    tercile,
    winsorize,
)
from tradedesk.analytics.liquidation_flush import (  # noqa: E402
    cluster_bootstrap_ci,
    cluster_bootstrap_p,
    holm,
    month_start,
    month_starts,
    monthly_universe,
    without_top_days,
)
from tradedesk.analytics.macro_release import cochran_q  # noqa: E402


def ms(y: int, m: int, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


GRID0, GRID1 = ms(2021, 11, 1), ms(2026, 10, 1)
START, END = ms(2021, 12, 1), ms(2026, 10, 1)
SPLIT = ms(2025, 1, 1)
NBARS = (GRID1 - GRID0) // FIVE_MIN_MS
BTC = "BTCUSDT"
WIDTH = 60
UNIVERSES = ("top50m", "top60d")  # 0 = primary (amendment 1), 1 = registered top-60, descriptive (coverage-biased)
MONTHLY_UNIVERSE_FILE = "universe_oi1.json"
ENTRIES = (1, 2)  # bars after the event bar: 1 = zero delay (upper bound), 2 = 5 min delay (primary)
HORIZONS = {"15m": 3, "30m": 6, "60m": 12, "120m": 24}
COST, COST_MID, COST_STRESS = 0.0032, 0.0042, 0.0062
CRASH_DAY = "2025-10-10"
N_BOOT = 10_000
CTRL_OFFSETS = (-14 * DAY_MS, -7 * DAY_MS, 7 * DAY_MS, 14 * DAY_MS)
SHIFT_OFFSETS = (-DAY_MS, DAY_MS)
CTRL_MAX_ABS_R15 = 0.006
KEYS = [f"e{e}_{h}" for e in ENTRIES for h in HORIZONS]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def day_str(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d")


def to_grid(a: np.ndarray) -> dict[str, np.ndarray]:
    """Rows [ts, o, h, l, c, qv, bv] -> open/high/low/close arrays on the global 5m grid (NaN where missing)."""
    out = {k: np.full(NBARS, np.nan) for k in ("o", "h", "l", "c")}
    if a.shape[0] == 0:
        return out
    t = a[:, 0].astype(np.int64)
    idx = (t - GRID0) // FIVE_MIN_MS
    ok = (idx >= 0) & (idx < NBARS) & ((t - GRID0) % FIVE_MIN_MS == 0)
    for col, k in ((1, "o"), (2, "h"), (3, "l"), (4, "c")):
        out[k][idx[ok]] = a[ok, col]
    return out


# ---------------------------------------------------------------- compute


def build_moments(btc: dict[str, np.ndarray]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ts = GRID0 + np.arange(NBARS, dtype=np.int64) * FIVE_MIN_MS
    last_ok = GRID1 - (max(ENTRIES) + max(HORIZONS.values()) + 1) * FIVE_MIN_MS
    events = detect_shocks(ts, btc["c"], start_ms=START, end_ms=min(END, last_ok))
    r15 = lag_return(btc["c"], 3)
    ev_ts = [e["ts"] for e in events]
    moments: list[dict[str, Any]] = []
    for k, e in enumerate(events):
        moments.append({"kind": "event", "i": e["i"], "ts": e["ts"], "sign": e["sign"], "parent": k})
    for k, e in enumerate(events):
        for t in placebo_times(e["ts"], ev_ts, CTRL_OFFSETS):
            i = (t - GRID0) // FIVE_MIN_MS
            if not (BARS_30D <= i < NBARS - 40) or not np.isfinite(r15[i]) or r15[i] == 0 or abs(r15[i]) >= CTRL_MAX_ABS_R15:
                continue
            moments.append({"kind": "ctrl", "i": int(i), "ts": t, "sign": 1 if r15[i] > 0 else -1, "parent": k})
        for off in SHIFT_OFFSETS:
            t = e["ts"] + off
            i = (t - GRID0) // FIVE_MIN_MS
            if BARS_30D <= i < NBARS - 40:
                moments.append({"kind": "shift", "i": int(i), "ts": t, "sign": e["sign"], "parent": k})
    return events, moments


def compute(cache: Path, out: Path) -> None:
    t0 = time.time()
    symbols, info = vlf.read_json(cache / "symbols.json"), vlf.read_json(cache / vlf.INFO_FILE)
    cands = [s for s in vlf.candidate_symbols(symbols, info) if s != BTC]
    monthly = {int(k): [s for s in v if s != BTC] for k, v in vlf.read_json(cache / MONTHLY_UNIVERSE_FILE).items()}
    btc = to_grid(vlf.load_5m(cache, BTC, month_starts(GRID0, GRID1)))
    print(f"BTC bars {int(np.isfinite(btc['c']).sum())}/{NBARS}; rss {rss_mb():.0f} MB", flush=True)
    events, moments = build_moments(btc)
    print(f"events {len(events)}, moments {len(moments)} ({Counter(m['kind'] for m in moments)})", flush=True)
    daily = vlf.load_daily(cache, cands)
    print(f"daily series {len(daily)}; rss {rss_mb():.0f} MB; {time.time() - t0:.0f}s", flush=True)
    memo: dict[tuple[int, int], list[str]] = {}
    M, U = len(moments), len(UNIVERSES)
    syms = np.full((U, M, WIDTH), "", dtype=object)
    by_sym: dict[str, dict[int, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for mi, m in enumerate(moments):
        d0 = m["ts"] - m["ts"] % DAY_MS
        if (0, d0) not in memo:
            members = monthly.get(month_start(d0), [])
            memo[(0, d0)] = monthly_universe({s: daily[s] for s in members if s in daily}, d0, top_n=WIDTH)
            memo[(1, d0)] = monthly_universe(daily, d0, top_n=WIDTH)
        for u in range(U):
            for r, s in enumerate(memo[(u, d0)]):
                syms[u, mi, r] = s
                by_sym[s][mi].append((u, r))
    del daily
    print(f"universe days {len(memo) // 2}, symbols needed {len(by_sym)}; rss {rss_mb():.0f} MB", flush=True)
    arr = {k: np.full((U, M, WIDTH), np.nan) for k in [*KEYS, "beta5", "beta1h", "mae60", "jump60"]}
    btc_r = {k: np.full(M, np.nan) for k in KEYS}
    file_missing = np.zeros((U, M, WIDTH), dtype=bool)
    for mi, m in enumerate(moments):
        i = m["i"]
        for e in ENTRIES:
            for h, nb in HORIZONS.items():
                btc_r[f"e{e}_{h}"][mi] = post_entry_return(btc["o"], i, e, nb)
    jumps: list[tuple[str, float]] = []
    for n_done, (s, per_m) in enumerate(sorted(by_sym.items())):
        months = sorted({x for mi in per_m for x in month_starts(moments[mi]["ts"] - 31 * DAY_MS, moments[mi]["ts"] + 4 * HOUR_MS)})
        have = {m for m in months if (cache / "k5m" / s / f"{datetime.fromtimestamp(m / 1000, tz=UTC):%Y-%m}.json").exists()}
        g = to_grid(vlf.load_5m(cache, s, sorted(have)))
        with np.errstate(invalid="ignore", divide="ignore"):
            adj = np.abs(np.diff(np.log(g["c"])))  # adjacent grid bars only (NaN across gaps)
        if np.isfinite(adj).any() and np.nanmax(adj) > math.log(1.5):
            jumps.append((s, float(np.nanmax(adj))))
        for mi, slots in per_m.items():
            m = moments[mi]
            i = m["i"]
            miss = not set(month_starts(m["ts"] - 30 * DAY_MS, m["ts"] + 3 * HOUR_MS)) <= have
            w = beta_window(i, BARS_30D)
            b5 = beta_from_closes(btc["c"][w], g["c"][w], min_count=BARS_30D // 2)
            b1 = beta_from_closes(btc["c"][w], g["c"][w], step=12, min_count=BARS_30D // 24)
            vals: dict[str, float] = {"beta5": np.nan if b5 is None else b5, "beta1h": np.nan if b1 is None else b1}
            # largest adjacent-bar jump over the beta window and the primary hold (exit = open of bar i+14)
            vals["jump60"] = max_abs_log_jump(g["c"], w.start, i + 2 + HORIZONS["60m"] + 1)
            for e in ENTRIES:
                for h, nb in HORIZONS.items():
                    vals[f"e{e}_{h}"] = post_entry_return(g["o"], i, e, nb)
            en = g["o"][i + 2]
            v = adverse_excursion(m["sign"], en, g["h"][i + 2 : i + 14], g["l"][i + 2 : i + 14]) if np.isfinite(en) else None
            vals["mae60"] = np.nan if v is None else v
            for u, r in slots:
                file_missing[u, mi, r] = miss
                for k, x in vals.items():
                    arr[k][u, mi, r] = x
        if n_done % 100 == 0:
            print(f"  [{n_done}/{len(by_sym)}] {s}; {time.time() - t0:.0f}s; rss {rss_mb():.0f} MB", flush=True)
    meta = {"moments": moments, "events": events, "jumps": jumps, "symbols_needed": len(by_sym)}
    (out / "meta.json").write_text(json.dumps(meta))
    np.savez_compressed(
        out / "arrays.npz",
        syms=syms.astype(str),
        file_missing=file_missing,
        **{f"alt_{k}": v for k, v in arr.items()},
        **{f"btc_{k}": v for k, v in btc_r.items()},
    )
    print(f"compute done in {time.time() - t0:.0f}s; rss {rss_mb():.0f} MB", flush=True)


MISSING_FILE = "missing_files.json"


def k5m_path(cache: Path, s: str, m: int) -> Path:
    return cache / "k5m" / s / f"{datetime.fromtimestamp(m / 1000, tz=UTC):%Y-%m}.json"


def write_missing(cache: Path, out: Path) -> list[list[str]]:
    """List the monthly 5m files that the moments of every universe need (30 d before .. 3 h after) and the cache lacks.

    Needs ``--stage compute`` first (it stores the per-moment universes); the cache stays untouched.
    """
    meta = json.loads((out / "meta.json").read_text())
    syms = np.load(out / "arrays.npz", allow_pickle=False)["syms"]
    need: set[tuple[str, int]] = set()
    for mi, m in enumerate(meta["moments"]):
        months = month_starts(m["ts"] - 30 * DAY_MS, m["ts"] + 3 * HOUR_MS)
        for s in {str(x) for x in syms[:, mi].ravel() if x}:
            need.update((s, mo) for mo in months)
    miss = sorted([s, f"{datetime.fromtimestamp(mo / 1000, tz=UTC):%Y-%m}"] for s, mo in need if not k5m_path(cache, s, mo).exists())
    (out / MISSING_FILE).write_text(json.dumps(miss))
    print(f"needed monthly 5m files {len(need)}, missing {len(miss)} -> {out / MISSING_FILE}", flush=True)
    return miss


async def fetch_missing(cache: Path, out: Path, sample: int | None) -> None:
    """Download the files of ``missing_files.json`` via rt.binance_vision into the shared kline cache (per-file JSON)."""
    import logging

    from tradedesk.server import build_server

    miss = json.loads((out / MISSING_FILE).read_text())
    if sample:
        miss = miss[:sample]
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        fx = vlf.Fetcher(rt, cache)
        jobs = []
        for s, yms in miss:
            y, mo = int(yms[:4]), int(yms[5:7])

            async def go(s: str = s, y: int = y, mo: int = mo) -> Any:
                return vlf.kline_rows((await rt.binance_vision.um_klines_monthly(s, "5m", y, mo)).data)

            jobs.append((cache / "k5m" / s / f"{y:04d}-{mo:02d}.json", go))
        await fx.many("klines 5m (missing)", jobs)
        print(f"FETCH COMPLETE; rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- analysis


class Data:
    def __init__(self, out: Path) -> None:
        meta = json.loads((out / "meta.json").read_text())
        self.moments = meta["moments"]
        self.events = meta["events"]
        self.jumps = meta["jumps"]
        z = np.load(out / "arrays.npz", allow_pickle=False)
        self.syms = z["syms"]
        self.file_missing = z["file_missing"]
        self.alt = {k[4:]: z[k] for k in z.files if k.startswith("alt_")}
        self.btc = {k[4:]: z[k] for k in z.files if k.startswith("btc_")}
        self.kind = np.array([m["kind"] for m in self.moments])
        self.sign = np.array([m["sign"] for m in self.moments], dtype=float)
        self.ts = np.array([m["ts"] for m in self.moments], dtype=np.int64)
        self.day = np.array([day_str(int(t)) for t in self.ts])
        self.masks: list[dict[str, np.ndarray]] = []
        for u in range(len(UNIVERSES)):
            n = (self.syms[u] != "").sum(axis=1)
            mk = {t: np.zeros(self.syms[u].shape, dtype=bool) for t in ("high", "mid", "low", "all")}
            for mi, nm in enumerate(n):
                for r in range(int(nm)):
                    mk[tercile(r + 1, int(nm))][mi, r] = True
                    mk["all"][mi, r] = True
            self.masks.append(mk)
        # Registered rule: the top-60 (u=1) is H1 when the bottom-tercile pair coverage of the events is >= 90 %;
        # otherwise amendment 1 applies and the monthly top-50 (u=0) is primary.
        ev = self.kind == "event"
        mk1 = self.masks[1]["low"][ev]
        self.coverage60 = float((np.isfinite(self.resid(1)[ev]) & mk1).sum() / mk1.sum())
        self.primary = 1 if self.coverage60 >= 0.90 else 0

    def resid(self, u: int | None = None, key: str = "e2_60m", beta: str = "beta5", hedge: bool = True) -> np.ndarray:
        u = self.primary if u is None else u
        r = self.alt[key][u]
        b = self.alt[beta][u]
        if not hedge:
            return np.where(np.isfinite(b), self.sign[:, None] * r, np.nan)
        return self.sign[:, None] * (r - b * self.btc[key][:, None])

    def basket(self, terc: str, u: int | None = None, **kw: Any) -> np.ndarray:
        u = self.primary if u is None else u
        return mean_rows(np.where(self.masks[u][terc], self.resid(u, **kw), np.nan))


def mean_rows(e: np.ndarray) -> np.ndarray:
    n = np.isfinite(e).sum(axis=1)
    return np.where(n > 0, np.nansum(e, axis=1) / np.maximum(n, 1), np.nan)


def pct(x: float | None, nd: int = 3) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{100 * x:+.{nd}f} %"


def ci_s(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{100 * ci[0]:+.3f}; {100 * ci[1]:+.3f}]"


def stats(v: np.ndarray, days: np.ndarray, cost: float = COST) -> dict[str, Any]:
    ok = np.isfinite(v)
    v, d = v[ok], days[ok]
    if v.size < 2:
        return {"n": int(v.size)}
    net, stress, dl = v - cost, v - COST_STRESS, d.tolist()
    return {
        "n": int(v.size),
        "days": len(set(dl)),
        "gross": float(v.mean()),
        "net": float(net.mean()),
        "ci": cluster_bootstrap_ci(net.tolist(), dl, n_boot=N_BOOT),
        "p": cluster_bootstrap_p(net.tolist(), dl, n_boot=N_BOOT),
        "stress": float(stress.mean()),
        "ci_stress": cluster_bootstrap_ci(stress.tolist(), dl, n_boot=N_BOOT),
        "ci_gross": cluster_bootstrap_ci(v.tolist(), dl, n_boot=N_BOOT),
        "p_gross": cluster_bootstrap_p(v.tolist(), dl, n_boot=N_BOOT),
    }


HEAD = (
    "| рядок | n | днів | брутто | CI брутто | net 0.32 % | CI net | net 0.62 % (стрес) | CI стрес |\n|---|---|---|---|---|---|---|---|---|"
)


def row(label: str, s: dict[str, Any]) -> str:
    if s.get("n", 0) < 2:
        return f"| {label} | {s.get('n', 0)} | | | | | | | |"
    return (
        f"| {label} | {s['n']} | {s['days']} | {pct(s['gross'])} | {ci_s(s['ci_gross'])} | {pct(s['net'])} | {ci_s(s['ci'])} | "
        f"{pct(s['stress'])} | {ci_s(s['ci_stress'])} |"
    )


def stage0(out: Path) -> list[str]:
    D = Data(out)
    ev = D.kind == "event"
    lines = ["## Стадія 0 (без середніх)", ""]
    years = Counter(d[:4] for d in D.day[ev])
    lines.append(f"Подій (після розрідження 2 год): {int(ev.sum())}; по роках: {dict(sorted(years.items()))}")
    lines.append(
        f"Днів-кластерів: {len(set(D.day[ev].tolist()))}; напрямки: падіння {int((D.sign[ev] < 0).sum())}, ріст {int((D.sign[ev] > 0).sum())}"
    )
    for u, name in enumerate(UNIVERSES):
        n = (D.syms[u][ev] != "").sum(axis=1)
        lines.append(f"Всесвіт {name}: членів на подію мін {int(n.min())}, медіана {int(np.median(n))}, макс {int(n.max())}")
        res = D.resid(u)[ev]
        for t in ("high", "mid", "low"):
            mk = D.masks[u][t][ev]
            valid = np.isfinite(res) & mk
            lines.append(
                f"  {t}: валідних пар (60 хв, i+2, β) {100 * valid.sum() / mk.sum():.2f} %; без 5-хв файлу {100 * (D.file_missing[u][ev] & mk).sum() / mk.sum():.2f} %; "
                f"подій без валідного члена {int((valid.sum(axis=1) == 0).sum())}"
            )
    lines.append(f"Стрибки ціни > ×1.5 між сусідніми 5-хв барами (перевірка колізій тікерів): {[(s, round(x, 2)) for s, x in D.jumps]}")
    for u, name in enumerate(UNIVERSES):
        b = D.basket("low", u)[ev]
        ok = np.isfinite(b)
        sd = float(b[ok].std(ddof=1))
        se = cluster_se(b[ok].tolist(), D.day[ev][ok].tolist())
        gate = "пройдено" if mde(se) <= 0.0025 else "НЕ пройдено"
        lines.append(
            f"[{name}] кошик нижнього терцилю (60 хв, i+2): n={int(ok.sum())}, SD={100 * sd:.3f} %, SE наївна {100 * sd / math.sqrt(ok.sum()):.4f} %, "
            f"SE кластерна (дні) {100 * se:.4f} %; MDE = 2.8 × SE = {100 * mde(se):.3f} % (ворота ≤ 0.25 %: {gate})"
        )
        for nm, sg in (("падіння", -1), ("ріст", 1)):
            sel = ok & (D.sign[ev] == sg)
            lines.append(f"  {nm}: n={int(sel.sum())}, MDE {100 * mde(cluster_se(b[sel].tolist(), D.day[ev][sel].tolist())):.3f} %")
    return lines


def per_asset(D: Data, ev: np.ndarray, is_m: np.ndarray, days: np.ndarray, out: Path) -> list[str]:
    u = D.primary
    L = ["", f"## Пер-активна перевірка ({UNIVERSES[u]}, нижній терциль, 60 хв, i+2, net 0.32 %)", ""]
    mk = D.masks[u]["low"][ev]
    e_low = np.where(mk, D.resid(u)[ev] - COST, np.nan)
    s_low = D.syms[u][ev]
    is_rows = np.repeat(is_m[:, None], WIDTH, axis=1)
    d_rows = np.repeat(days[:, None], WIDTH, axis=1)
    okm = np.isfinite(e_low)
    per: dict[str, list[float]] = defaultdict(list)
    for s, x in zip(s_low[okm], e_low[okm], strict=True):
        per[str(s)].append(float(x))
    tab = [(s, len(x), float(np.mean(x)), float(np.std(x, ddof=1) / math.sqrt(len(x)))) for s, x in per.items() if len(x) >= 10]
    tab.sort(key=lambda t: -t[1])
    q = cochran_q([t[2] for t in tab], [t[3] for t in tab])
    L.append(
        f"Монет з n ≥ 10: {len(tab)} (усього різних монет у нижньому терцилі: {len(per)}); Cochran Q={q['Q']:.1f}, df={q['df']}, p={q['p']:.4f}, I²={100 * q['I2']:.1f} %"
    )
    L.append("\n| монета | n | E[R] net | 95 % CI (норм.) |\n|---|---|---|---|")
    for s, n, mu, se in tab[:20]:
        L.append(f"| {s} | {n} | {pct(mu)} | [{100 * (mu - 1.96 * se):+.3f}; {100 * (mu + 1.96 * se):+.3f}] |")
    L.append(f"\n(20 монет з найбільшим n із {len(tab)}; повна таблиця — `per_asset.json`)")
    (out / "per_asset.json").write_text(json.dumps(tab))
    is_per: dict[str, list[float]] = defaultdict(list)
    for s, x in zip(s_low[okm & is_rows], e_low[okm & is_rows], strict=True):
        is_per[str(s)].append(float(x))
    ranked = sorted(((float(np.mean(x)), s) for s, x in is_per.items() if len(x) >= 10), reverse=True)
    top = {s for _, s in ranked[:10]}
    oos = okm & ~is_rows
    vals, dd = e_low[oos], d_rows[oos]
    # registered: top-10; the top quarter is a descriptive addition (the top-10 coins are rare in OOS)
    for name, chosen in (("топ-10 за IS", top), ("верхня чверть за IS (описово)", {s for _, s in ranked[: len(ranked) // 4]})):
        in_top = np.array([str(s) in chosen for s in s_low[oos]])
        for label, sel in ((name, in_top), ("решта", ~in_top)):
            if sel.sum() > 1:
                ci = cluster_bootstrap_ci(vals[sel].tolist(), dd[sel].tolist(), n_boot=N_BOOT)
                L.append(f"OOS {label}: n={int(sel.sum())}, E[R] net {pct(float(vals[sel].mean()))}, CI {ci_s(ci)}")
    L.append(f"Топ-10 за IS (n_IS ≥ 10, з {len(ranked)}): {sorted(top)}")
    return L


def report(out: Path) -> list[str]:
    D = Data(out)
    ev = D.kind == "event"
    days = D.day[ev]
    sg = D.sign[ev]
    is_m = D.ts[ev] < SPLIT
    low = D.basket("low")[ev]
    high = D.basket("high")[ev]
    s_h1 = stats(low, days)
    s_dn = stats(np.where(sg < 0, low, np.nan), days)
    s_up = stats(np.where(sg > 0, low, np.nan), days)
    s_h2 = stats(low - high, days, cost=0.0)
    s_is = stats(np.where(is_m, low, np.nan), days)
    s_oos = stats(np.where(~is_m, low, np.nan), days)
    u = D.primary
    other = 1 - u
    other_label = (
        "top50m нижній (ранги ≈ 33–49 місячного топ-50; поправка 1, чутливість)"
        if u == 1
        else "top60d нижній (ранги 41–60, лише з даними; зміщено)"
    )
    L = [
        f"Повнота пар нижнього терцилю top60d (події, 60 хв, i+2, β): {100 * D.coverage60:.2f} % → основний всесвіт "
        f"{UNIVERSES[u]} ({'зареєстрований, поправка 1 втрачає силу' if u == 1 else 'поправка 1'})",
        "",
        f"## Основна таблиця (всесвіт {UNIVERSES[u]}, 60 хв, вхід i+2, β 5 хв, якщо не сказано інше)",
        "",
        HEAD,
    ]
    L.append(row("**H1** нижній терциль, обидва напрямки", s_h1))
    L.append(row("H3 падіння BTC (шорт кошика)", s_dn))
    L.append(row("H3 ріст BTC (лонг кошика)", s_up))
    L.append(row("середній терциль", stats(D.basket("mid")[ev], days)))
    L.append(row("верхній терциль", stats(high, days)))
    L.append(row("весь всесвіт", stats(D.basket("all")[ev], days)))
    L.append(row(other_label, stats(D.basket("low", other)[ev], days)))
    # critic item 4 (descriptive): bad prints / ticker re-use
    e_ev = np.where(D.masks[u]["low"][ev], D.resid(u)[ev], np.nan)
    jmp = D.alt["jump60"][u][ev]
    no_jump = np.where(np.isfinite(jmp) & (jmp > math.log(1.5)), np.nan, e_ev)
    n_jump = int((np.isfinite(e_ev) & np.isfinite(jmp) & (jmp > math.log(1.5))).sum())
    L.append(row(f"H1 без пар зі стрибком > ×1.5 у вікні β/утримання ({n_jump} пар; описово)", stats(mean_rows(no_jump), days)))
    L.append(row("H1 з вінзоризацією e_a 1 %/99 % (описово)", stats(mean_rows(winsorize(e_ev, 0.01, 0.99)), days)))
    L.append("")
    L.append(
        f"H2 різниця нижній − верхній (брутто, парно): n={s_h2['n']}, {pct(s_h2['gross'])}, CI {ci_s(s_h2['ci_gross'])}, p={s_h2['p_gross']:.4f}"
    )
    ps = [s_h1["p"], s_h2["p_gross"], s_dn["p"], s_up["p"]]
    adj = holm(ps)
    L.append(f"Holm (H1, H2, H3-падіння, H3-ріст), односторонні p: сирі {[round(p, 4) for p in ps]} → скориг. {[round(p, 4) for p in adj]}")
    L += ["", "## Часовий поділ і робастність (нижній терциль, 60 хв, i+2)", "", HEAD]
    L.append(row("IS 2021-12 … 2024", s_is))
    L.append(row("OOS 2025 … 2026-09", s_oos))
    dstr = days.astype(str)
    for y in sorted({d[:4] for d in dstr}):
        L.append(row(f"рік {y}", stats(np.where(np.char.startswith(dstr, y), low, np.nan), days)))
    L.append(row("без 2022", stats(np.where(~np.char.startswith(dstr, "2022"), low, np.nan), days)))
    L.append(row(f"без {CRASH_DAY}", stats(np.where(days != CRASH_DAY, low, np.nan), days)))
    ok = np.isfinite(low)
    v, d, dropped = without_top_days(low[ok].tolist(), days[ok].tolist(), 10)
    L.append(row("без топ-10 днів", stats(np.array(v), np.array(d))))
    L.append(f"\nВиключені топ-10 днів: {dropped}")
    L += ["", "## Чутливість (описово)", "", HEAD]
    for h in HORIZONS:
        L.append(row(f"нижній, {h}, вхід i+2", stats(D.basket("low", key=f"e2_{h}")[ev], days)))
    for h in HORIZONS:
        L.append(row(f"нижній, {h}, вхід i+1 (верхня межа)", stats(D.basket("low", key=f"e1_{h}")[ev], days)))
    L.append(row("верхній, 60m, вхід i+1", stats(D.basket("high", key="e1_60m")[ev], days)))
    L.append(row("нижній, β за 1-год доходностями", stats(D.basket("low", beta="beta1h")[ev], days)))
    L.append(row("нижній, без хеджу (s × r_alt)", stats(D.basket("low", hedge=False)[ev], days)))
    L.append(row(f"{UNIVERSES[other]} нижній, вхід i+1", stats(D.basket("low", other, key="e1_60m")[ev], days)))
    s42 = stats(low, days, cost=COST_MID)
    L.append(f"\nH1 з витратами 0.42 %: net {pct(s42['net'])}, CI {ci_s(s42['ci'])}")
    bb = D.btc["e2_60m"][ev] * sg
    L.append(
        f"BTC після входу (s × r_btc, 60 хв, i+2): {pct(float(np.nanmean(bb)))}, CI {ci_s(cluster_bootstrap_ci(bb[np.isfinite(bb)].tolist(), days[np.isfinite(bb)].tolist(), n_boot=N_BOOT))}"
    )
    L += ["", "## Плацебо H4 (нижній терциль, 60 хв, i+2; очікується ≈ 0 брутто)", "", HEAD]
    for k, label in (("ctrl", "(а) ±7/±14 діб, без шоку, s = знак r15 BTC"), ("shift", "(б) зсув ±1 доба, s події")):
        sel = D.kind == k
        L.append(row(label, stats(D.basket("low")[sel], D.day[sel])))
    mae = np.where(D.masks[u]["low"][ev], D.alt["mae60"][u][ev], np.nan)
    mae = mae[np.isfinite(mae)]
    lb = low[np.isfinite(low)]
    L += [
        "",
        "## Хвости ризику (на нотіонал кошика / монети)",
        f"Кошик нижнього терцилю брутто: p1 {pct(float(np.quantile(lb, 0.01)))}, мін {pct(float(lb.min()))}, p99 {pct(float(np.quantile(lb, 0.99)))}, макс {pct(float(lb.max()))}",
        f"Найгірший хід окремої монети проти позиції за 60 хв (high/low): p99 {pct(float(np.quantile(mae, 0.99)))}, макс {pct(float(mae.max()))}, n={mae.size}",
    ]
    L += per_asset(D, ev, is_m, days, out)
    h1_ok = s_h1["net"] >= 0.0035 and s_h1["ci"][0] > 0 and adj[0] < 0.05 and s_oos["net"] > 0
    h2_ok = s_h2["gross"] >= 0.002 and s_h2["ci_gross"][0] > 0
    killed = s_h1["ci"][0] <= 0 or s_h1["gross"] < COST or (s_is["net"] > 0 and s_oos["net"] <= 0) or not h2_ok
    verdict = "confirmed" if h1_ok and h2_ok else ("rejected" if killed else "inconclusive")
    L += [
        "",
        f"**Вердикт за пре-реєстрованим правилом: {verdict}** (H1 пройдено: {h1_ok}; H2 пройдено: {h2_ok}; критерій «вбити»: {killed})",
    ]
    return L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/liquidation_flush", help="kline cache (read only)")
    ap.add_argument("--work-dir", default="~/.cache/tradedesk-agents/btc_shock_alt_lag", help="derived arrays")
    ap.add_argument("--stage", choices=["compute", "missing", "fetch", "stage0", "report"], required=True)
    ap.add_argument("--out", default=None, help="markdown output file for stage0/report")
    ap.add_argument("--sample", type=int, default=None, help="fetch only the first N missing files (timing)")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    work = Path(a.work_dir).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    if a.stage == "compute":
        compute(cache, work)
        return
    if a.stage == "missing":
        write_missing(cache, work)
        return
    if a.stage == "fetch":
        import asyncio

        asyncio.run(fetch_missing(cache, work, a.sample))
        return
    lines = stage0(work) if a.stage == "stage0" else stage0(work) + [""] + report(work)
    text = "\n".join(lines)
    print(text if a.stage == "stage0" else "\n".join(lines[-60:]), flush=True)
    if a.out:
        Path(a.out).expanduser().write_text(text + "\n")


if __name__ == "__main__":
    main()
