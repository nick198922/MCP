"""Volatility targeting and a Donchian trend ensemble as exposure rules for BTC/ETH spot (IDEA-20261018-risk-overlay).

The pre-registration is the card section "Попередня реєстрація" in
``docs/ideas/IDEA-20261018-risk-overlay-vol-target-trend.md``. Network only through the runtime providers
(``rt.exchanges`` for Bitstamp daily candles, ``rt.binance_vision`` for 2019-09..2021-09 BTC/ETH funding); every
response is written to the per-file cache under ``--cache-dir`` as it arrives (default
``~/.cache/tradedesk-agents/risk_overlay``). Read only: ``macro_regimes/klines`` (Binance spot 1h, 2017-12+) and
``funding_crowding`` (Binance USDT-M daily klines and funding, 2021-10..2026-09).

    uv run python scripts/verify_risk_overlay.py --stage prereg   # hash of the registration -> registration.txt
    uv run python scripts/verify_risk_overlay.py --stage fetch
    uv run python scripts/verify_risk_overlay.py --stage sanity --out sanity.md
    uv run python scripts/verify_risk_overlay.py --stage stage0 --out stage0.md   # SE / MDE only, no point estimates
    uv run python scripts/verify_risk_overlay.py --stage report --out report.md
    uv run python scripts/verify_risk_overlay.py --stage explore --out explore.md   # added after the registered run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.listing_drift import symbol_eligible
from tradedesk.analytics.per_asset import persistence
from tradedesk.analytics.risk_overlay import (
    block_permute,
    bootstrap,
    card_status,
    cochran_q,
    cvar,
    donchian_fraction,
    ewma_vol,
    h1_verdict,
    holm,
    log_returns,
    max_drawdown,
    mde,
    net_returns,
    placebo_p,
    positions,
    rolling_vol,
    sharpe,
    sr_verdict,
    vol_target_signal,
)

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261018-risk-overlay-vol-target-trend.md"
SECTION = "## Попередня реєстрація"
MR = Path("~/.cache/tradedesk-agents/macro_regimes/klines").expanduser()
FC = Path("~/.cache/tradedesk-agents/funding_crowding").expanduser()
DAY_MS, HOUR_MS = 86_400_000, 3_600_000
BTC_START, END = date(2014, 1, 1), date(2026, 9, 30)
HALF2 = date(2021, 1, 1)
COVID = (date(2020, 3, 1), date(2020, 12, 31))
PERP_START = date(2019, 9, 10)
FEE, PERP_FEE, LAM, CAP, LAG = 0.001, 0.0005, 0.94, 1.0, 2
WIN, MIN_N, CH = 365, 200, (20, 50, 100, 200)
REPS, PL_REPS, PL_BLOCK, DBLOCK, MBLOCK, SEED = 2000, 1000, 60, 20, 3, 20261018
CONST_SIG = {"BTC": 0.60, "ETH": 0.80}
L: list[str] = []


def emit(s: str = "") -> None:
    L.append(s)
    print(s, flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def d2ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def ms2d(ms: float) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date()


def _write(p: Path, data: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(data)
    tmp.replace(p)


# ----------------------------------------------------------------------------- prereg / fetch


def stage_prereg(cache: Path) -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index(SECTION)
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}"
    emit(line)
    with (cache / "registration.txt").open("a") as f:
        f.write(line + "\n")


def months_between(a: date, b: date) -> list[tuple[int, int]]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def fetch(cache: Path) -> None:
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    bdir, fdir = cache / "bitstamp", cache / "funding"
    bdir.mkdir(exist_ok=True)
    fdir.mkdir(exist_ok=True)
    _, rt = build_server()
    t0 = time.perf_counter()
    end_ms = d2ms(date(2026, 10, 2))
    try:
        for sym, start in (("BTC/USD", date(2013, 1, 1)), ("ETH/USD", date(2017, 8, 1))):
            since = d2ms(start)
            while since < end_ms:
                p = bdir / f"{sym.replace('/', '_')}_{since}.json"
                if p.exists():
                    rows = json.loads(p.read_text())
                else:
                    f = await rt.exchanges.ohlcv("bitstamp", sym, "1d", 1000, since_ms=since)
                    rows = [r for r in f.data if r[0] >= since]
                    _write(p, json.dumps(rows))
                    print(
                        f"bitstamp {sym} since {ms2d(since)}: {len(rows)} rows {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB",
                        flush=True,
                    )
                    await asyncio.sleep(1.0)
                if not rows:
                    break
                since = int(rows[-1][0]) + DAY_MS
        for sym, start in (("BTCUSDT", date(2019, 9, 1)), ("ETHUSDT", date(2019, 11, 1))):
            (fdir / sym).mkdir(exist_ok=True)
            for y, m in months_between(start, date(2021, 9, 1)):
                p = fdir / sym / f"{y:04d}-{m:02d}.json"
                if p.exists():
                    continue
                try:
                    f = await rt.binance_vision.um_funding_monthly(sym, y, m)
                    rows = f.data
                except ProviderError as e:
                    print(f"funding {sym} {y}-{m:02d}: {e}", flush=True)
                    rows = []
                _write(p, json.dumps(rows))
                print(f"funding {sym} {y}-{m:02d}: {len(rows)} rows {time.perf_counter() - t0:.0f}s", flush=True)
        # the archive starts at 2020-01: earlier months from the venue API (ccxt), stored in the same row format
        for sym, ccxt_sym, start in (("BTCUSDT", "BTC/USDT:USDT", date(2019, 9, 1)), ("ETHUSDT", "ETH/USDT:USDT", date(2019, 11, 1))):
            p = fdir / sym / "api_2019.json"
            if p.exists():
                continue
            f = await rt.exchanges.funding_rate_history_range("binanceusdm", ccxt_sym, d2ms(start), d2ms(date(2020, 1, 1)) - 1)
            api_rows = [[float(r["timestamp"]), 8.0, float(r["fundingRate"])] for r in f.data["rows"] if r.get("timestamp") is not None]
            _write(p, json.dumps(api_rows))
            print(
                f"funding api {sym} 2019: {len(api_rows)} rows, complete={f.data['complete']} {time.perf_counter() - t0:.0f}s", flush=True
            )
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ----------------------------------------------------------------------------- data


@dataclass
class Daily:
    dates: list[date]
    close: np.ndarray
    vol: np.ndarray = field(default_factory=lambda: np.zeros(0))


def _daily_from_rows(rows: list[list[float]]) -> Daily:
    by = {}
    for r in rows:
        by[ms2d(r[0])] = (float(r[4]), float(r[5]) if len(r) > 5 else float("nan"))
    ds = sorted(by)
    return Daily(ds, np.array([by[d][0] for d in ds]), np.array([by[d][1] for d in ds]))


def load_bitstamp(cache: Path, a: str) -> Daily:
    rows: list[list[float]] = []
    for p in sorted((cache / "bitstamp").glob(f"{a}_USD_*.json"), key=lambda q: int(q.stem.rsplit("_", 1)[1])):
        rows.extend(json.loads(p.read_text()))
    return _daily_from_rows(rows)


def load_binance_spot(a: str) -> tuple[Daily, Daily]:
    """Binance spot from hourly bars: (close at 00:00 UTC per day, price at 01:00 UTC per day), both keyed by the day
    whose close it is (00:00 of d + 1 -> d; 01:00 of d + 1 -> d)."""
    c00: dict[date, float] = {}
    c01: dict[date, float] = {}
    for p in sorted(MR.glob(f"{a}USDT-1h-*.json")):
        for row in json.loads(p.read_text()):
            t_end = datetime.fromtimestamp((int(row[0]) + HOUR_MS) / 1000, tz=UTC)
            if t_end.hour == 0 and t_end.minute == 0:
                c00[(t_end - timedelta(days=1)).date()] = float(row[4])
            elif t_end.hour == 1 and t_end.minute == 0:
                c01[(t_end - timedelta(days=1)).date()] = float(row[4])
    d0, d1 = sorted(c00), sorted(c01)
    return Daily(d0, np.array([c00[d] for d in d0])), Daily(d1, np.array([c01[d] for d in d1]))


def load_funding_daily(cache: Path, sym: str) -> dict[date, float]:
    """Sum of funding settlements per UTC day of the position that pays them (settlement at T -> day of T - 1 ms)."""
    out: dict[date, float] = {}
    files = sorted((cache / "funding" / sym).glob("*.json")) + sorted((FC / "funding" / sym).glob("*.json"))
    seen: set[int] = set()
    for p in files:
        for t, _h, rate in json.loads(p.read_text()):
            k = int(t) // 1000
            if k in seen:
                continue
            seen.add(k)
            d = ms2d(int(t) - 1)
            out[d] = out.get(d, 0.0) + float(rate)
    return out


def funding_intervals(cache: Path, sym: str) -> dict[float, int]:
    cnt: dict[float, int] = {}
    for p in sorted((cache / "funding" / sym).glob("*.json")) + sorted((FC / "funding" / sym).glob("*.json")):
        for _t, h, _r in json.loads(p.read_text()):
            cnt[float(h)] = cnt.get(float(h), 0) + 1
    return cnt


# ----------------------------------------------------------------------------- strategy evaluation


@dataclass
class Run:
    dates: list[date]
    R: np.ndarray
    w: np.ndarray  # target weights
    P: np.ndarray  # strategy net
    Pc: np.ndarray  # constant weight c, net
    c: float
    held: np.ndarray
    turnover: float  # sum |dw| per year
    funding: np.ndarray | None = None
    kw: dict[str, Any] = field(default_factory=dict)

    @property
    def mkeys(self) -> list[tuple[int, int]]:
        return [(d.year, d.month) for d in self.dates]


def signals(
    close: np.ndarray, *, lam: float = LAM, sigma: str = "rolling", const: float | None = None, cap: float = CAP
) -> dict[str, np.ndarray]:
    r = log_returns(close)
    sh = ewma_vol(r, lam)
    if sigma == "rolling":
        st: Any = rolling_vol(r, WIN, min_n=MIN_N)
    elif sigma == "expanding":
        st = rolling_vol(r, None, min_n=MIN_N)
    else:
        st = np.where(np.arange(close.size) >= MIN_N, float(const or 0.0), np.nan)
    s1 = vol_target_signal(st, sh, cap)
    f = donchian_fraction(close, CH)
    return {"H1": s1, "H2": f * np.minimum(s1, 1.0), "H2raw": f}


def simple_returns(close: np.ndarray) -> np.ndarray:
    R = np.full(close.size, np.nan)
    R[1:] = close[1:] / close[:-1] - 1.0
    return R


def make_run(
    dates: list[date],
    R_all: np.ndarray,
    sig: np.ndarray,
    lo: date,
    hi: date,
    *,
    lag: int = LAG,
    fee: float = FEE,
    band: float = 0.0,
    funding: dict[date, float] | None = None,
) -> Run:
    w_all = positions(sig, lag)
    idx = [i for i, d in enumerate(dates) if lo <= d <= hi and np.isfinite(w_all[i]) and np.isfinite(R_all[i])]
    ds = [dates[i] for i in idx]
    R, w = R_all[idx], w_all[idx]
    fu = np.array([funding.get(d, 0.0) for d in ds]) if funding is not None else None
    kw = {"fee": fee, "band": band, "perp_fee": PERP_FEE, "funding": fu}
    o = net_returns(w, R, **kw)
    c = float(o["held"].mean())
    oc = net_returns(np.full(w.size, c), R, **kw)
    yrs = len(ds) / 365.0
    return Run(ds, R, w, o["ret"], oc["ret"], c, o["held"], float(o["turnover"][1:].sum() / yrs), fu, kw)


def sub_run(run: Run, keep: Callable[[date], bool]) -> Run:
    idx = [i for i, d in enumerate(run.dates) if keep(d)]
    return Run([run.dates[i] for i in idx], run.R[idx], run.w[idx], run.P[idx], run.Pc[idx], run.c, run.held[idx], run.turnover)


def month_index(keys: list[tuple[int, int]]) -> np.ndarray:
    starts = [0] + [i for i in range(1, len(keys)) if keys[i] != keys[i - 1]]
    return np.array(starts, dtype=int)


def monthly(p: np.ndarray, starts: np.ndarray) -> np.ndarray:
    return np.expm1(np.add.reduceat(np.log1p(p), starts))


def stats_point(run: Run) -> dict[str, float]:
    st = month_index(run.mkeys)
    mP, mC = monthly(run.P, st), monthly(run.Pc, st)
    return {
        "T1": cvar(mC) - cvar(mP),
        "T2": max_drawdown(run.Pc) - max_drawdown(run.P),
        "dSR": sharpe(run.P) - sharpe(run.Pc),
        "cvarP": cvar(mP),
        "cvarC": cvar(mC),
        "mddP": max_drawdown(run.P),
        "mddC": max_drawdown(run.Pc),
        "mddBH": max_drawdown(run.R),
        "srP": sharpe(run.P),
        "srC": sharpe(run.Pc),
        "srBH": sharpe(run.R),
        "n_days": len(run.dates),
        "n_months": len(st),
    }


def boots(run: Run, reps: int = REPS, seed: int = SEED) -> dict[str, dict[str, Any]]:
    st = month_index(run.mkeys)
    mP, mC = monthly(run.P, st), monthly(run.Pc, st)
    P, Pc = run.P, run.Pc
    return {
        "T1": bootstrap(mP.size, lambda i: cvar(mC[i]) - cvar(mP[i]), block=MBLOCK, reps=reps, seed=seed),
        "T2": bootstrap(P.size, lambda i: max_drawdown(Pc[i]) - max_drawdown(P[i]), block=DBLOCK, reps=reps, seed=seed),
        "dSR": bootstrap(P.size, lambda i: sharpe(P[i]) - sharpe(Pc[i]), block=DBLOCK, reps=reps, seed=seed),
    }


def placebo(run: Run, reps: int = PL_REPS, seed: int = SEED) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    st = month_index(run.mkeys)
    mC = monthly(run.Pc, st)
    cv_c, mdd_c, sr_c = cvar(mC), max_drawdown(run.Pc), sharpe(run.Pc)
    kw = dict(run.kw)
    out: dict[str, list[float]] = {"T1": [], "T2": [], "dSR": []}
    for _ in range(reps):
        wp = block_permute(run.w, PL_BLOCK, rng)
        P = net_returns(wp, run.R, **kw)["ret"]
        out["T1"].append(cv_c - cvar(monthly(P, st)))
        out["T2"].append(mdd_c - max_drawdown(P))
        out["dSR"].append(sharpe(P) - sr_c)
    return {k: np.array(v) for k, v in out.items()}


def oos_cut(run: Run) -> date:
    return run.dates[int(len(run.dates) * 0.75)]


# ----------------------------------------------------------------------------- formatting


def f(x: float | None, k: float = 1.0, d: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x * k:+.{d}f}"


def ci(c: tuple[float, float] | None, k: float = 1.0, d: int = 2) -> str:
    return f"[{c[0] * k:+.{d}f}; {c[1] * k:+.{d}f}]" if c else "—"


def pct(x: float, d: int = 1) -> str:
    return "—" if not math.isfinite(x) else f"{x * 100:.{d}f} %"


# ----------------------------------------------------------------------------- shared setup


class Data:
    def __init__(self, cache: Path) -> None:
        self.bs = {a: load_bitstamp(cache, a) for a in ("BTC", "ETH")}
        self.R = {a: simple_returns(self.bs[a].close) for a in ("BTC", "ETH")}
        self.sig = {a: signals(self.bs[a].close) for a in ("BTC", "ETH")}
        self.start = {"BTC": BTC_START}
        eth = self.bs["ETH"]
        self.start["ETH"] = eth.dates[MIN_N + LAG]  # first day whose position uses >= 200 returns of history
        self.cache = cache

    def run(self, a: str, strat: str = "H1", lo: date | None = None, hi: date = END, **kw: Any) -> Run:
        return make_run(self.bs[a].dates, self.R[a], self.sig[a][strat], lo or self.start[a], hi, **kw)


# ----------------------------------------------------------------------------- sanity


def stage_sanity(D: Data) -> None:
    emit("## Санітарні перевірки")
    for a in ("BTC", "ETH"):
        s = D.bs[a]
        gaps = [(x, y) for x, y in zip(s.dates[:-1], s.dates[1:], strict=True) if (y - x).days > 1]
        zero = [d for d, v in zip(s.dates, s.vol, strict=True) if not v > 0]
        flat = int(np.sum(np.diff(s.close) == 0))
        emit(
            f"- Bitstamp {a}/USD: днів {len(s.dates)} ({s.dates[0]} … {s.dates[-1]}); розривів > 1 доби {len(gaps)} {[(str(x), str(y)) for x, y in gaps[:8]]}; "
            f"діб з нульовим обсягом {len(zero)} {[str(d) for d in zero[:8]]}; діб з незмінним close {flat}"
        )
        for lo, hi in ((date(2014, 1, 1), date(2016, 12, 31)), (date(2017, 1, 1), END)):
            z = [d for d in zero if lo <= d <= hi]
            emit(f"  - нульовий обсяг {lo:%Y}–{hi:%Y}: {len(z)}")
        r = log_returns(s.close)
        big = sorted(((abs(r[i]), s.dates[i], r[i]) for i in range(1, r.size)), reverse=True)[:5]
        emit(f"  - 5 найбільших |лог-дохідностей|: {[(str(d), round(float(x), 3)) for _, d, x in big]}")
    px = dict(zip(D.bs["BTC"].dates, D.bs["BTC"].close, strict=True))
    emit(
        f"- BTC close 2017-12-16 (00:00 17-го): {px.get(date(2017, 12, 16))}; 2021-11-09: {px.get(date(2021, 11, 9))} (очікуємо ≈ 19–20 тис., ≈ 67–68 тис.)"
    )
    for a in ("BTC", "ETH"):
        b00, b01 = load_binance_spot(a)
        bp = dict(zip(b00.dates, b00.close, strict=True))
        s = D.bs[a]
        diffs = np.array([abs(c / bp[d] - 1) for d, c in zip(s.dates, s.close, strict=True) if d in bp])
        emit(
            f"- |Bitstamp {a}/USD / Binance {a}USDT 00:00 UTC − 1| (n {diffs.size}): медіана {np.median(diffs):.3%}, 99-й перц. {np.quantile(diffs, 0.99):.2%}, макс {diffs.max():.1%}; "
            f"Binance 00:00 днів {len(b00.dates)} ({b00.dates[0]}…{b00.dates[-1]}), 01:00 днів {len(b01.dates)}"
        )
    for a in ("BTC", "ETH"):
        lo = D.start[a]
        idx = [i for i, d in enumerate(D.bs[a].dates) if lo <= d <= END]
        r = log_returns(D.bs[a].close)[idx]
        emit(
            f"- {a}: період оцінки {D.bs[a].dates[idx[0]]} … {D.bs[a].dates[idx[-1]]} ({len(idx)} діб); вола BH {np.std(r, ddof=1) * math.sqrt(365):.2f}"
        )
        s1 = D.sig[a]["H1"]
        ok = np.isfinite(s1)
        emit(
            f"  - перший визначений сигнал H1: {D.bs[a].dates[int(np.argmax(ok))]}; H2: {D.bs[a].dates[int(np.argmax(np.isfinite(D.sig[a]['H2'])))]}"
        )
    for sym in ("BTCUSDT", "ETHUSDT"):
        fd = load_funding_daily(D.cache, sym)
        ds = sorted(fd)
        miss = [d for d in (ds[0] + timedelta(days=k) for k in range((ds[-1] - ds[0]).days + 1)) if d not in fd]
        vals = np.array([fd[d] for d in ds])
        emit(
            f"- funding {sym}: діб {len(ds)} ({ds[0]} … {ds[-1]}), без жодного розрахунку {len(miss)} {[str(d) for d in miss[:6]]}; "
            f"інтервали (год: к-сть) {funding_intervals(D.cache, sym)}; середній денний {vals.mean():.5f} (річних {vals.mean() * 365:.1%})"
        )
    u = alt_universe()
    emit(f"- панель альтів (5a, описово): {len(u)} символів: {', '.join(u)}")
    emit(f"- peak RSS {rss_mb():.0f} MB")


# ----------------------------------------------------------------------------- stage 0 (no point estimates)


def stage0(D: Data) -> None:
    emit("## Стадія 0: SE і MDE (точкові оцінки не друкуються)")
    emit(
        "| актив | діб | місяців | c (сер. вага H1) | ρ(P_H1, P_C) | SE(ΔSR) | MDE(ΔSR) | SE(T1) | SE(T2) | √(2(1−ρ)/T) |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    runs = {}
    for a in ("BTC", "ETH"):
        run = D.run(a)
        runs[a] = run
        b = boots(run)
        rho = float(np.corrcoef(run.P, run.Pc)[0, 1])
        yrs = len(run.dates) / 365
        emit(
            f"| {a} | {len(run.dates)} | {len(month_index(run.mkeys))} | {run.c:.2f} | {rho:.3f} | {b['dSR']['se']:.3f} | {mde(b['dSR']['se']):.2f} | "
            f"{b['T1']['se']:.3f} | {b['T2']['se']:.3f} | {math.sqrt(2 * (1 - rho) / yrs):.3f} |"
        )
    pooled = pooled_boot(runs["BTC"], runs["ETH"])
    emit(f"- пул BTC+ETH (спільні доби {pooled['n']}): SE(ΔSR) {pooled['se']:.3f}, MDE {mde(pooled['se']):.2f}")
    b = boots(runs["BTC"])
    gate = mde(b["dSR"]["se"])
    emit(
        f"- ворота: MDE(ΔSR H1, BTC) = {gate:.2f} {'> 0.40 → дохідність ◯ без обговорення' if gate > 0.40 else '≤ 0.40'}; {'> 0.30 → ΔSR описова (◯)' if gate > 0.30 else '≤ 0.30 → ΔSR має вердикт'}"
    )
    emit(f"- peak RSS {rss_mb():.0f} MB")


def pooled_boot(rb: Run, re: Run, reps: int = REPS) -> dict[str, Any]:
    common = sorted(set(rb.dates) & set(re.dates))
    ib = {d: i for i, d in enumerate(rb.dates)}
    ie = {d: i for i, d in enumerate(re.dates)}
    jb = np.array([ib[d] for d in common])
    je = np.array([ie[d] for d in common])
    Pb, Cb, Pe, Ce = rb.P[jb], rb.Pc[jb], re.P[je], re.Pc[je]

    def stat(i: np.ndarray) -> float:
        return 0.5 * ((sharpe(Pb[i]) - sharpe(Cb[i])) + (sharpe(Pe[i]) - sharpe(Ce[i])))

    return bootstrap(len(common), stat, block=DBLOCK, reps=reps, seed=SEED)


# ----------------------------------------------------------------------------- alt panel


def alt_universe(k: int = 30) -> list[str]:
    info = json.loads((FC / "exchange_info.json").read_text())["info"]
    vol: dict[str, float] = {}
    for p in (FC / "k1d").glob("*/2021-10.json"):
        s = p.parent.name
        if s in ("BTCUSDT", "ETHUSDT"):
            continue
        i = info.get(s) or {}
        if not symbol_eligible(s, i.get("contractType"), i.get("underlyingType")):
            continue
        rows = json.loads(p.read_text())
        if len(rows) < 31 or ms2d(rows[0][0]) != date(2021, 10, 1):
            continue
        vol[s] = sum(float(r[6]) for r in rows)
    return [s for s, _ in sorted(vol.items(), key=lambda x: -x[1])[:k]]


def load_alt(sym: str) -> Daily:
    rows: list[list[float]] = []
    for p in sorted((FC / "k1d" / sym).glob("*.json")):
        rows.extend(json.loads(p.read_text()))
    return _daily_from_rows(rows)


# ----------------------------------------------------------------------------- report


def endpoint_table(D: Data) -> dict[str, Any]:
    emit("\n## Головні кінцеві точки H1 (кап 1.0, спот, витрати 0.10 %, лаг 2)")
    emit(
        "| актив | період | діб / міс. | c | CVaR5 міс. H1 / C | T1 | 95 % CI | p | MaxDD H1 / C / BH | T2 | 95 % CI | p |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    res: dict[str, Any] = {}
    for a in ("BTC", "ETH"):
        run = D.run(a)
        sp, b = stats_point(run), boots(run)
        pl = placebo(run)
        cut = oos_cut(run)
        oo = stats_point(sub_run(run, lambda d, cut=cut: d >= cut))
        res[a] = {"run": run, "sp": sp, "b": b, "pl": pl, "cut": cut, "oos": oo}
        emit(
            f"| {a} | {run.dates[0]}…{run.dates[-1]} | {sp['n_days']} / {sp['n_months']} | {run.c:.2f} | {pct(sp['cvarP'])} / {pct(sp['cvarC'])} | "
            f"{f(sp['T1'], 100, 1)} pp | {ci(b['T1']['ci95'], 100, 1)} | {b['T1']['p']:.4f} | {pct(sp['mddP'])} / {pct(sp['mddC'])} / {pct(sp['mddBH'])} | "
            f"{f(sp['T2'], 100, 1)} pp | {ci(b['T2']['ci95'], 100, 1)} | {b['T2']['p']:.4f} |"
        )
    keys = [("BTC", "T1"), ("BTC", "T2"), ("ETH", "T1"), ("ETH", "T2")]
    adj = holm([res[a]["b"][t]["p"] for a, t in keys])
    emit("\n| точка | p | p Holm | проходить | плацебо: середнє, 95-й перц. | p плацебо | OOS (від) |\n|---|---|---|---|---|---|---|")
    for (a, t), pa in zip(keys, adj, strict=True):
        r = res[a]
        pp = placebo_p(r["sp"][t], r["pl"][t])
        r.setdefault("holm", {})[t] = pa
        r.setdefault("pp", {})[t] = pp
        emit(
            f"| {a} {t} | {r['b'][t]['p']:.4f} | {pa:.4f} | {'так' if pa <= 0.05 else 'ні'} | {f(float(np.mean(r['pl'][t])), 100, 1)} / {f(float(np.quantile(r['pl'][t], 0.95)), 100, 1)} pp | "
            f"{pp:.3f} | {f(r['oos'][t], 100, 1)} pp ({r['cut']}) |"
        )
    for a in ("BTC", "ETH"):
        r = res[a]
        r["v"] = h1_verdict(
            [r["holm"]["T1"] <= 0.05, r["holm"]["T2"] <= 0.05],
            [r["b"]["T1"]["ci95"][1], r["b"]["T2"]["ci95"][1]],
            [r["pp"]["T1"], r["pp"]["T2"]],
            [r["oos"]["T1"], r["oos"]["T2"]],
        )
        emit(f"- **H1 {a}: {r['v']}**")
    status = card_status(
        res["BTC"]["v"],
        res["ETH"]["v"],
        btc_placebo=[res["BTC"]["pp"]["T1"], res["BTC"]["pp"]["T2"]],
        btc_oos=[res["BTC"]["oos"]["T1"], res["BTC"]["oos"]["T2"]],
        btc_points=[res["BTC"]["sp"]["T1"], res["BTC"]["sp"]["T2"]],
        eth_points=[res["ETH"]["sp"]["T1"], res["ETH"]["sp"]["T2"]],
    )
    emit(f"- **Статус картки за зареєстрованим правилом: `{status}`**")
    res["status"] = status
    return res


def sr_table(D: Data, res: dict[str, Any]) -> None:
    emit("\n## Друга кінцева точка: ΔSR = SR(H1) − SR(C)")
    emit(
        "| актив | SR H1 | SR C | SR BH | ΔSR | 95 % CI | SE | MDE | плацебо p | OOS ΔSR | вердикт |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ("BTC", "ETH"):
        r = res[a]
        sp, b = r["sp"], r["b"]["dSR"]
        pp = placebo_p(sp["dSR"], r["pl"]["dSR"])
        md = mde(b["se"])
        v = sr_verdict(sp["dSR"], b["ci95"], r["oos"]["dSR"], pp, md)
        emit(
            f"| {a} | {sp['srP']:.2f} | {sp['srC']:.2f} | {sp['srBH']:.2f} | {f(sp['dSR'])} | {ci(b['ci95'])} | {b['se']:.3f} | {md:.2f} | {pp:.3f} | {f(r['oos']['dSR'])} | **{v}** |"
        )
    pb = pooled_boot(res["BTC"]["run"], res["ETH"]["run"])
    emit(f"- пул BTC+ETH (спільні {pb['n']} діб): ΔSR {f(pb['stat'])} {ci(pb['ci95'])}, SE {pb['se']:.3f}, MDE {mde(pb['se']):.2f}")
    emit("- SR — річний, з денних чистих дохідностей на капітал; кеш 0 %. SR(C) = SR(BH) з точністю до витрат ребалансу.")


def describe(run: Run) -> dict[str, float]:
    sp = stats_point(run)
    yrs = len(run.dates) / 365
    eq = float(np.prod(1 + run.P))
    sp.update(
        {
            "cagr": eq ** (1 / yrs) - 1,
            "cagr_bh": float(np.prod(1 + run.R)) ** (1 / yrs) - 1,
            "cagr_c": float(np.prod(1 + run.Pc)) ** (1 / yrs) - 1,
            "in_mkt": float(np.mean(run.held > 1e-9)),
            "mean_w": float(run.held.mean()),
            "turn": run.turnover,
            "calmar": (eq ** (1 / yrs) - 1) / sp["mddP"] if sp["mddP"] > 0 else float("nan"),
        }
    )
    return sp


def descriptive(D: Data) -> None:
    emit("\n## Описово: H1, H2 (Donchian × вола), H2-raw (лише Donchian)")
    emit(
        "| актив | правило | сер. вага | у ринку | оборот/рік | CAGR (BH; C) | SR (BH) | ΔSR до C | MaxDD (BH) | MaxDD / MaxDD_BH | T1 | T2 | Calmar |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ("BTC", "ETH"):
        for s in ("H1", "H2", "H2raw"):
            run = D.run(a, s)
            x = describe(run)
            emit(
                f"| {a} | {s} | {x['mean_w']:.2f} | {x['in_mkt']:.0%} | {x['turn']:.1f} | {pct(x['cagr'])} ({pct(x['cagr_bh'])}; {pct(x['cagr_c'])}) | "
                f"{x['srP']:.2f} ({x['srBH']:.2f}) | {f(x['dSR'])} | {pct(x['mddP'])} ({pct(x['mddBH'])}) | {x['mddP'] / x['mddBH']:.2f} | "
                f"{f(x['T1'], 100, 1)} pp | {f(x['T2'], 100, 1)} pp | {x['calmar']:.2f} |"
            )
    emit("- Критерій картки для H2: MaxDD(H2) ≤ 0.7 × MaxDD(BH) — див. колонку «MaxDD / MaxDD_BH».")
    for a in ("BTC", "ETH"):
        for s in ("H2", "H2raw"):
            run = D.run(a, s)
            b = boots(run, reps=1000)
            emit(
                f"- {a} {s}: ΔSR {f(b['dSR']['stat'])} {ci(b['dSR']['ci95'])} (MDE {mde(b['dSR']['se']):.2f}); T1 {f(b['T1']['stat'], 100, 1)} {ci(b['T1']['ci95'], 100, 1)} pp; "
                f"T2 {f(b['T2']['stat'], 100, 1)} {ci(b['T2']['ci95'], 100, 1)} pp (1000 повторів)"
            )


def robust_row(a: str, label: str, run: Run, reps: int = 1000) -> None:
    b = boots(run, reps=reps)
    emit(
        f"| {a} | {label} | {len(run.dates)} | {run.c:.2f} | {f(b['T1']['stat'], 100, 1)} | {ci(b['T1']['ci95'], 100, 1)} | {f(b['T2']['stat'], 100, 1)} | "
        f"{ci(b['T2']['ci95'], 100, 1)} | {f(b['dSR']['stat'])} | {ci(b['dSR']['ci95'])} | {run.turnover:.1f} |"
    )


def robustness(D: Data, res: dict[str, Any]) -> None:
    emit("\n## Стійкість H1 (зареєстрований перелік, друкується весь; 1000 повторів бутстрепа)")
    emit(
        "| актив | варіант | діб | c | T1, pp | 95 % CI | T2, pp | 95 % CI | ΔSR | 95 % CI | оборот/рік |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ("BTC", "ETH"):
        s = D.bs[a]
        base = res[a]["run"]
        robust_row(a, "**головний**", base)
        for lam in (0.90, 0.97):
            robust_row(a, f"λ = {lam}", make_run(s.dates, D.R[a], signals(s.close, lam=lam)["H1"], D.start[a], END))
        robust_row(a, "σ* розширюване", make_run(s.dates, D.R[a], signals(s.close, sigma="expanding")["H1"], D.start[a], END))
        robust_row(
            a,
            f"σ* стала {CONST_SIG[a]}",
            make_run(s.dates, D.R[a], signals(s.close, sigma="const", const=CONST_SIG[a])["H1"], D.start[a], END),
        )
        robust_row(a, "лаг 1 (той самий close, неторгований)", D.run(a, lag=1))
        robust_row(a, "витрати 0.30 %", D.run(a, fee=0.003))
        robust_row(a, "смуга 0.10", D.run(a, band=0.10))
        robust_row(a, "2014–2020" if a == "BTC" else "до 2020", D.run(a, hi=date(2020, 12, 31)))
        robust_row(a, "2021–2026-09", D.run(a, lo=max(HALF2, D.start[a])))
        robust_row(a, "без 2020-03…2020-12", sub_run(base, lambda d: not COVID[0] <= d <= COVID[1]))
        st = month_index(base.mkeys)
        mP, mC = monthly(base.P, st), monthly(base.Pc, st)
        mk = [base.mkeys[i] for i in st]
        top = sorted(range(len(mk)), key=lambda i: -abs(mP[i] - mC[i]))[:3]
        drop = {mk[i] for i in top}
        robust_row(
            a,
            "без 3 міс. з найбільшою |H1 − C|: " + ", ".join(f"{y}-{m:02d}" for y, m in sorted(drop)),
            sub_run(base, lambda d, drop=drop: (d.year, d.month) not in drop),
        )
    emit("- «без …»: доби вирізано з обох рядів, криві склеєні; c і ваги — з головного прогону.")


def binance_checks(D: Data) -> None:
    emit("\n## 2018+: Bitstamp проти Binance і виконання о 01:00 UTC")
    emit(
        "| актив | варіант | діб | c | T1, pp | 95 % CI | T2, pp | 95 % CI | ΔSR | 95 % CI | оборот/рік |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a in ("BTC", "ETH"):
        b00, b01 = load_binance_spot(a)
        sig = signals(b00.close)["H1"]
        first = b00.dates[MIN_N + LAG]
        lo = max(first, D.start[a])
        R00 = simple_returns(b00.close)
        robust_row(a, f"Bitstamp, від {lo}", D.run(a, lo=lo))
        robust_row(a, "Binance close 00:00, лаг 2", make_run(b00.dates, R00, sig, lo, END))
        # 01:00 execution: position over [01:00 d, 01:00 d+1] = signal from close 00:00 of d (index d - 1), lag 1 on R01
        p01 = dict(zip(b01.dates, b01.close, strict=True))
        sig_map = dict(zip(b00.dates, sig, strict=True))
        ds = [d for d in b00.dates if d in p01 and (d - timedelta(days=1)) in p01]
        R01 = np.array([p01[d] / p01[d - timedelta(days=1)] - 1 for d in ds])
        # R01 at d spans 01:00 of day d .. 01:00 of day d + 1; signal known at 00:00 of day d = close of d - 1
        s_al = np.array([sig_map.get(d - timedelta(days=1), np.nan) for d in ds])
        robust_row(a, "Binance, виконання 01:00 UTC (лаг 1 год)", make_run(ds, R01, s_al, lo, END, lag=0))


def perp_check(D: Data) -> None:
    emit("\n## Кап 1.5 через перп (з 2019-09-10 / ETHUSDT з 2019-11-27): частина > 1 платить 0.05 % і фактичний funding")
    emit(
        "| актив | варіант | діб | c | T1, pp | 95 % CI | T2, pp | 95 % CI | ΔSR | 95 % CI | оборот/рік |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for a, lo in (("BTC", PERP_START), ("ETH", date(2019, 11, 28))):
        fu = load_funding_daily(D.cache, f"{a}USDT")
        s = D.bs[a]
        sig15 = signals(s.close, cap=1.5)["H1"]
        r15 = make_run(s.dates, D.R[a], sig15, lo, END, funding=fu)
        r10 = D.run(a, lo=lo)
        robust_row(a, "кап 1.0 (той самий період)", r10)
        robust_row(a, "кап 1.5 перп + funding", r15)
        lev = r15.held > 1
        paid = float(np.sum(np.maximum(r15.held - 1, 0) * (r15.funding if r15.funding is not None else 0)))
        sr15, sr10 = sharpe(r15.P), sharpe(r10.P)
        emit(
            f"- {a}: частка діб з вагою > 1: {lev.mean():.0%}; funding сплачено за період {paid:.2%} капіталу; SR кап 1.5 {sr15:.2f} проти кап 1.0 {sr10:.2f}; "
            f"MaxDD {pct(max_drawdown(r15.P))} проти {pct(max_drawdown(r10.P))}; CAGR {pct(float(np.prod(1 + r15.P)) ** (365 / len(r15.dates)) - 1)} проти {pct(float(np.prod(1 + r10.P)) ** (365 / len(r10.dates)) - 1)}"
        )


def bear_phases(D: Data) -> None:
    emit("\n## Ведмежі фази (просідання BH > 50 % у період оцінки), описово")
    emit("| актив | пік | дно | BH | H1 | H2 | H2-raw | C (H1) |\n|---|---|---|---|---|---|---|---|")
    for a in ("BTC", "ETH"):
        runs = {s: D.run(a, s) for s in ("H1", "H2", "H2raw")}
        base = runs["H1"]
        for s in ("H2", "H2raw"):
            assert runs[s].dates == base.dates, f"{a} {s}: dates differ from H1"
        eq = np.cumprod(1 + base.R)
        eps = []
        pk = tr = 0
        for i in range(eq.size):
            if eq[i] >= eq[pk]:
                if 1 - eq[tr] / eq[pk] > 0.5:
                    eps.append((pk, tr))
                pk = tr = i
            elif eq[i] < eq[tr]:
                tr = i
        if 1 - eq[tr] / eq[pk] > 0.5:
            eps.append((pk, tr))
        for p, t in eps:
            sl = slice(p + 1, t + 1)
            row = [f"{float(np.prod(1 + base.R[sl])) - 1:+.0%}"]
            for s in ("H1", "H2", "H2raw"):
                row.append(f"{float(np.prod(1 + runs[s].P[sl])) - 1:+.0%}")
            row.append(f"{float(np.prod(1 + base.Pc[sl])) - 1:+.0%}")
            emit(f"| {a} | {base.dates[p]} | {base.dates[t]} | " + " | ".join(row) + " |")
    emit("- Дохідність від close піку до close дна; H1/H2/H2-raw і C на тих самих добах.")


def per_asset_section(D: Data, res: dict[str, Any]) -> None:
    emit("\n## Перевірка 5a: BTC проти ETH (H1)")
    emit("| точка | BTC (SE) | ETH (SE) | Q | p | I² |\n|---|---|---|---|---|---|")
    for t, k in (("T1", 100), ("T2", 100), ("dSR", 1)):
        e = [res[a]["b"][t]["stat"] for a in ("BTC", "ETH")]
        s = [res[a]["b"][t]["se"] for a in ("BTC", "ETH")]
        q = cochran_q(e, s)
        emit(f"| {t} | {f(e[0], k)} ({s[0] * k:.2f}) | {f(e[1], k)} ({s[1] * k:.2f}) | {q['q']:.2f} | {q['p']:.3f} | {q['i2']:.0%} |")
    emit("- Активів два: стійкість топ-активів на двох не рахується. Нижче — описова панель альтів.")
    alt_panel()


def alt_panel() -> None:
    u = alt_universe()
    emit(f"\n### Панель альтів Binance USDT-M (описово, не впливає на вердикт): {len(u)} символів, топ за обсягом 2021-10")
    emit("| символ | період | діб | c | T1, pp (SE) | T2, pp (SE) | ΔSR (SE) | MaxDD BH |\n|---|---|---|---|---|---|---|---|")
    est: dict[str, list[tuple[float, float]]] = {"T1": [], "T2": [], "dSR": []}
    vals, assets, clusters, dts = [], [], [], []
    t0 = time.perf_counter()
    for k, sym in enumerate(u):
        s = load_alt(sym)
        if len(s.dates) < MIN_N + 60:
            emit(f"| {sym} | — | {len(s.dates)} | — | замало даних | | | |")
            continue
        R = simple_returns(s.close)
        sig = signals(s.close)["H1"]
        run = make_run(s.dates, R, sig, s.dates[MIN_N + LAG], END)
        b = boots(run, reps=500)
        for t in est:
            est[t].append((b[t]["stat"], b[t]["se"] if b[t]["se"] else float("nan")))
        emit(
            f"| {sym} | {run.dates[0]}…{run.dates[-1]} | {len(run.dates)} | {run.c:.2f} | {f(b['T1']['stat'], 100, 1)} ({(b['T1']['se'] or float('nan')) * 100:.1f}) | "
            f"{f(b['T2']['stat'], 100, 1)} ({(b['T2']['se'] or float('nan')) * 100:.1f}) | {f(b['dSR']['stat'])} ({(b['dSR']['se'] or float('nan')):.2f}) | {pct(max_drawdown(run.R))} |"
        )
        d = run.P - run.Pc
        vals.extend(d.tolist())
        assets.extend([sym] * d.size)
        clusters.extend(run.dates)
        dts.extend(run.dates)
        if (k + 1) % 10 == 0:
            print(f"[alt {k + 1}/{len(u)} {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    emit("\n| точка | k | середнє по активах | частка > 0 | Q | p | I² | об'єднана (1/SE²) |\n|---|---|---|---|---|---|---|---|")
    for t, k in (("T1", 100), ("T2", 100), ("dSR", 1)):
        e = np.array([x for x, _ in est[t]])
        s = np.array([y for _, y in est[t]])
        q = cochran_q(e, s)
        emit(
            f"| {t} | {q['k']} | {f(float(np.nanmean(e)), k)} | {np.mean(e > 0):.0%} | {q['q']:.1f} | {q['p']:.3f} | {q['i2']:.0%} | {f(q['pooled'], k)} |"
        )
    all_d = sorted(set(dts))
    cut = all_d[int(len(all_d) * 0.75)]
    is_mask = [d < cut for d in dts]
    ps = persistence(vals, assets, clusters, is_mask, min_n_is=200, top_frac=0.25, n_boot=2000, min_n_oos_rho=100)
    emit(
        f"- стійкість (денна різниця P_H1 − P_C; IS до {cut}, верхня чверть {len(ps['top'])} з {ps['k']}): IS топ {ps['top_is_mean'] * 1e4:+.1f} б.п./добу, "
        f"решта {ps['rest_is_mean'] * 1e4:+.1f}; OOS топ {ps['top_oos_mean'] * 1e4:+.1f}, решта {ps['rest_oos_mean'] * 1e4:+.1f}; "
        f"різниця {ps['diff'] * 1e4:+.1f} б.п. CI {ci(ps['diff_ci'], 1e4, 1)}, p {ps['diff_p']:.3f}; Spearman IS/OOS {ps['rho']:+.2f} (n {ps['rho_n']}); топ: {', '.join(ps['top'])}"
    )
    emit("- Ціна перпа як проксі споту, fee 0.10 %, без funding; делістинги лишаються до останньої доби.")


def stage_report(D: Data, out: str) -> None:
    emit(f"# Сирий вивід verify_risk_overlay.py ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit(
        f"Bitstamp денні; λ {LAM}, σ* ковзне {WIN} діб (мін. {MIN_N}), кап {CAP}, лаг {LAG}, fee {FEE:.2%}; бутстреп {REPS} (доби блок {DBLOCK}, місяці блок {MBLOCK}), "
        f"плацебо {PL_REPS} перестановок блоків {PL_BLOCK} діб, seed {SEED}."
    )
    t0 = time.perf_counter()
    res = endpoint_table(D)
    print(f"[endpoints {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    sr_table(D, res)
    descriptive(D)
    print(f"[descr {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    robustness(D, res)
    print(f"[robust {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB]", flush=True)
    binance_checks(D)
    perp_check(D)
    bear_phases(D)
    per_asset_section(D, res)
    emit(f"\n_час {time.perf_counter() - t0:.0f} с, peak RSS {rss_mb():.0f} MB_")
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def stage_explore(D: Data) -> None:
    """Added after the registered report (exploratory): which weight H1 held in the months that make the tail."""
    emit("## Дослідницьке (після зареєстрованого прогону): вага H1 у найгірших місяцях C")
    emit("| актив | місяць | C | H1 | сер. вага H1 у місяці | σ̂ на початку місяця | σ* на початку |\n|---|---|---|---|---|---|---|")
    for a in ("BTC", "ETH"):
        run = D.run(a)
        st = month_index(run.mkeys)
        mP, mC = monthly(run.P, st), monthly(run.Pc, st)
        k = max(1, math.ceil(0.05 * mC.size))
        r = log_returns(D.bs[a].close)
        sh, ss = ewma_vol(r, LAM), rolling_vol(r, WIN, min_n=MIN_N)
        pos = {d: i for i, d in enumerate(D.bs[a].dates)}
        bounds = list(st) + [len(run.dates)]
        for i in np.argsort(mC)[:k]:
            lo, hi = bounds[i], bounds[i + 1]
            j = pos[run.dates[lo]] - LAG  # signal index behind the month's first position
            emit(
                f"| {a} | {run.dates[lo]:%Y-%m} | {mC[i]:+.1%} | {mP[i]:+.1%} | {run.held[lo:hi].mean():.2f} | {sh[j]:.2f} | {ss[j]:.2f} |"
            )
        lowest = np.argsort(run.held)[:1]
        emit(
            f"- {a}: частка діб з вагою H1 < 0.9: {np.mean(run.held < 0.9):.0%}, < 0.7: {np.mean(run.held < 0.7):.0%}; мінімум {run.held[lowest[0]]:.2f} ({run.dates[lowest[0]]})"
        )
        nxt = np.array([np.std(r[pos[d] + 1 : pos[d] + 31], ddof=1) * math.sqrt(365) for d in run.dates[:-31]])
        cur = np.array([sh[pos[d] - LAG] for d in run.dates[:-31]])
        emit(f"  - кор. σ̂ (на момент рішення) і реалізованої воли наступних 30 діб: {np.corrcoef(cur, nxt)[0, 1]:+.2f} (n {cur.size})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("prereg", "fetch", "sanity", "stage0", "report", "explore"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/risk_overlay")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg(cache)
    elif a.stage == "fetch":
        asyncio.run(fetch(cache))
    elif a.stage == "sanity":
        stage_sanity(Data(cache))
    elif a.stage == "stage0":
        stage0(Data(cache))
    elif a.stage == "explore":
        stage_explore(Data(cache))
    else:
        stage_report(Data(cache), a.out)
        return
    if a.out:
        Path(a.out).expanduser().write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
