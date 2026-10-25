"""IDEA-20261025-gex-taker-flow-range: dealer gamma from Deribit taker flow vs next-day BTC range.

Stages (each caches its output under --cache-dir, default ~/.cache/tradedesk-agents/gex_taker_flow):
  build  one pass over put_skew/trades24 (2021-01-01 ... 2026-10-13): daily dealer-gamma snapshots at D 09:00 UTC
         -> daily_gamma.json, and the live-series flow at the end of the cache -> q_end.json      (local, minutes)
  gap    live Deribit BTC option OI (get_book_summary_by_currency) + all BTC option prints from the end of the
         cache up to that moment -> live_oi.json, gap_trades.json.gz                                (network)
  bars   Binance USDT-M BTCUSDT 1h klines for October 2026 (daily files) -> k1h_daily/              (network)
  gates  step 0: G1 coverage, G2 inventory vs live OI, G3 power (residual SD, regime persistence, MDE); no beta
  test   H1-H4, placebo, sensitivity (after pre-registration)

uv run python scripts/verify_gex_taker_flow.py --stage build [--limit-files 30]
uv run python scripts/verify_gex_taker_flow.py --stage gates --out ~/.cache/tradedesk-agents/gex_taker_flow/gates.txt
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import os
import resource
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.alt_downside_beta import ols_hac
from tradedesk.analytics.gex_flow import (
    HOUR_MS,
    FlowBook,
    dealer_gamma,
    expiry_flags,
    h2_day_stat,
    hac_se_indicator,
    next_day,
    parse_option,
    permute_within,
    pin_distance,
    rv_log,
    sign_persistence,
    trailing_z,
)
from tradedesk.analytics.per_asset import spearman
from tradedesk.analytics.put_skew import dvol_known_at_entry

CACHE_ROOT = Path.home() / ".cache/tradedesk-agents"
TRADES = CACHE_ROOT / "put_skew/trades24"
K1H = CACHE_ROOT / "course_s14_ema/k1h/BTCUSDT"
DVOL = CACHE_ROOT / "put_skew/dvol_BTC.json"
DELIVERY = CACHE_ROOT / "put_skew/delivery_btc_usd.json"

SNAP_FROM = date(2021, 9, 1)  # daily G from here (90-day z history before 2022)
START, SPLIT, END = date(2022, 1, 1), date(2025, 1, 1), date(2026, 10, 12)  # IS < SPLIT <= OOS
IV_AGE_H = 24  # registered N: own-print IV not older than 24 h, else same-expiry interpolation, else excluded
IV_AGE_SENS_H = 6
BLOCK_BTC = 100.0
HAC_LAG = 5
MIN_TRADES = 2000


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def t0_of(d: date) -> int:
    return day_ms(d) + 9 * HOUR_MS


def snap_dict(s: Any) -> dict[str, Any]:
    return {
        "g": s.gex,
        "gc": s.gex_call,
        "gp": s.gex_put,
        "n_live": s.n_live,
        "n_own": s.n_own_iv,
        "n_int": s.n_interp_iv,
        "n_x": s.n_excluded,
        "absq": s.live_abs_q,
        "absx": s.excluded_abs_q,
    }


# ----------------------------------------------------------------------------- build


def stage_build(cache: Path, limit: int | None) -> None:
    files = sorted(TRADES.glob("*.json.gz"))
    if limit:
        files = files[:limit]
    book, book_nb = FlowBook(), FlowBook(max_amount=BLOCK_BTC)
    prev_ids: set[str] = set()
    carry: list[Any] = []
    rows: list[dict[str, Any]] = []
    n_dup = n_after = 0
    t_start = time.perf_counter()
    for i, p in enumerate(files):
        d = date.fromisoformat(p.name[:10])
        t0 = t0_of(d)
        tr = json.loads(gzip.decompress(p.read_bytes()))["trades"]
        ids = {str(t["trade_id"]) for t in tr}
        new = [t for t in tr if str(t["trade_id"]) not in prev_ids]
        n_dup += len(tr) - len(new)
        prev_ids = ids
        new.sort(key=lambda t: int(t["timestamp"]))
        batch = carry + new
        carry = book.add(batch, t0)
        book_nb.add(batch, t0)
        n_after += len(carry)
        row: dict[str, Any] = {"d": d.isoformat(), "n_file": len(tr)}
        if d >= SNAP_FROM and book.spot > 0:
            prof_exp = t0 + 23 * HOUR_MS  # D+1 08:00: the expiry inside the response window (H4)
            sb = dealer_gamma(book, t0, iv_max_age_ms=IV_AGE_H * HOUR_MS, profile_expiry_ms=prof_exp)
            s6 = dealer_gamma(book, t0, iv_max_age_ms=IV_AGE_SENS_H * HOUR_MS)
            sn = dealer_gamma(book_nb, t0, iv_max_age_ms=IV_AGE_H * HOUR_MS)
            row.update(
                spot=sb.spot,
                spot_age_s=(t0 - book.spot_ts) / 1000,
                base=snap_dict(sb),
                age6=snap_dict(s6),
                nb=snap_dict(sn),
                prof={str(k): v for k, v in sb.profile.items()},
                strikes=list(sb.profile_strikes),
            )
        rows.append(row)
        book.drop_expired(t0)
        book_nb.drop_expired(t0)
        if i % 200 == 0 or i == len(files) - 1:
            print(
                f"  {d}: {len(tr)} prints, live series {len(book.series)}; {i + 1}/{len(files)} in "
                f"{time.perf_counter() - t_start:.0f}s, rss {rss_mb():.0f} MB",
                flush=True,
            )
    end_t0 = t0_of(date.fromisoformat(files[-1].name[:10]))
    q_end = {n: [s.q, s.gross] for n, s in book.series.items() if parse_option(n).expiry_ms > end_t0}  # type: ignore[union-attr]
    (cache / "daily_gamma.json").write_text(json.dumps(rows))
    (cache / "q_end.json").write_text(json.dumps({"t_end_ms": end_t0, "last_ts": book.last_ts, "q": q_end}))
    print(
        f"build done: {len(rows)} days, dup prints at file boundaries {n_dup}, prints after T0 carried {n_after}, "
        f"unparsed {book.n_unparsed}, blocks skipped (nb) {book_nb.n_block_skipped}, live at end {len(q_end)}; "
        f"rss {rss_mb():.0f} MB",
        flush=True,
    )


# ----------------------------------------------------------------------------- network stages


async def stage_gap(cache: Path) -> None:
    from tradedesk.server import build_server

    qe = json.loads((cache / "q_end.json").read_text())
    _, rt = build_server()
    try:
        f = await rt.deribit.book_summary("BTC", "option")
        oi_ms = int(time.time() * 1000)
        oi = {r["instrument_name"]: float(r.get("open_interest") or 0.0) for r in f.data or []}
        (cache / "live_oi.json").write_text(json.dumps({"ts_ms": oi_ms, "oi": oi}))
        print(f"live OI: {len(oi)} series at {datetime.fromtimestamp(oi_ms / 1000, UTC):%Y-%m-%d %H:%M} UTC", flush=True)
        cur, end = int(qe["t_end_ms"]) + 1, oi_ms
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        pages = 0
        t_start = time.perf_counter()
        while True:
            d = (await rt.deribit_history.last_trades_by_currency_and_time("BTC", "option", cur, end, count=1000)).data
            pages += 1
            for t in d["trades"]:
                if t["trade_id"] not in seen:
                    seen.add(t["trade_id"])
                    out.append({k: t.get(k) for k in ("trade_id", "instrument_name", "timestamp", "direction", "amount")})
            if not d["has_more"] or not d["trades"]:
                break
            nxt = int(d["trades"][-1]["timestamp"])
            cur = nxt if nxt > cur else cur + 1
            if pages % 25 == 0:
                print(f"  gap pages {pages}, prints {len(out)}, {time.perf_counter() - t_start:.0f}s", flush=True)
        tmp = cache / "gap_trades.json.gz.part"
        tmp.write_bytes(gzip.compress(json.dumps(out).encode(), 6))
        os.replace(tmp, cache / "gap_trades.json.gz")
        print(f"gap prints {len(out)} in {pages} pages", flush=True)
    finally:
        await rt.aclose()


async def stage_bars(cache: Path) -> None:
    from tradedesk.server import build_server

    out_dir = cache / "k1h_daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    try:
        d = date(2026, 10, 1)
        while d <= END + timedelta(days=2):
            p = out_dir / f"{d}.json"
            if not p.exists():
                f = await rt.binance_vision.um_klines_daily("BTCUSDT", "1h", d.isoformat())
                p.write_text(json.dumps([[r[0], r[1], r[2], r[3], r[4]] for r in f.data]))
                print(f"  bars {d}: {len(f.data)}", flush=True)
            d += timedelta(days=1)
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- dataset


def load_bars(cache: Path) -> dict[int, tuple[float, float, float, float]]:
    bars: dict[int, tuple[float, float, float, float]] = {}
    files = [p for p in sorted(K1H.glob("*.json")) if p.name >= "2021-08"] + sorted((cache / "k1h_daily").glob("*.json"))
    for p in files:
        for r in json.loads(p.read_text()) or []:
            bars[int(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    return bars


def build_dataset(cache: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [r for r in json.loads((cache / "daily_gamma.json").read_text()) if "base" in r]
    bars = load_bars(cache)
    dv = dvol_known_at_entry([(int(t), float(v)) for t, v in json.loads(DVOL.read_text())])
    # trailing z and the -1 SD threshold use the daily G series strictly before each day
    gser = [r["base"]["g"] for r in rows]
    z = trailing_z(gser, 90, 60)
    sd90: list[float | None] = []
    for i in range(len(gser)):
        past = gser[max(0, i - 90) : i]
        sd90.append(float(np.std(past, ddof=1)) if len(past) >= 60 else None)
    by_day = {r["d"]: i for i, r in enumerate(rows)}
    ds: list[dict[str, Any]] = []
    drop = {"no_bars": 0, "no_ctrl": 0, "no_dvol": 0}
    for i, r in enumerate(rows):
        d = date.fromisoformat(r["d"])
        if not (START <= d <= END):
            continue
        t0 = t0_of(d)
        resp = next_day(bars, t0)
        if resp is None:
            drop["no_bars"] += 1
            continue
        rv = [rv_log(bars, t0, k) for k in (1, 7, 30)]
        if any(v is None for v in rv):
            drop["no_ctrl"] += 1
            continue
        dvol = dv.get(day_ms(d))
        if not dvol:
            drop["no_dvol"] += 1
            continue
        lag7 = by_day.get((d - timedelta(days=7)).isoformat())
        mo, qu = expiry_flags(d)
        ds.append(
            {
                "d": d,
                "y": resp["y"],
                "rets": resp["rets"],
                "rv1": rv[0],
                "rv7": rv[1],
                "rv30": rv[2],
                "ldvol": math.log(dvol),
                "dow": d.weekday(),
                "mo": float(mo),
                "qu": float(qu),
                "g": r["base"]["g"],
                "g6": r["age6"]["g"],
                "gnb": r["nb"]["g"],
                "z": z[i],
                "sd90": sd90[i],
                "g_lag7": rows[lag7]["base"]["g"] if lag7 is not None else None,
                "spot": r["spot"],
                "prof": r.get("prof") or {},
                "strikes": r.get("strikes") or [],
                "excl_share": r["base"]["absx"] / r["base"]["absq"] if r["base"]["absq"] else 0.0,
            }
        )
    return ds, drop


def controls(ds: list[dict[str, Any]]) -> list[np.ndarray]:
    cols = [np.ones(len(ds))] + [np.array([r[k] for r in ds]) for k in ("rv1", "rv7", "rv30", "ldvol", "mo", "qu")]
    cols += [np.array([1.0 if r["dow"] == k else 0.0 for r in ds]) for k in range(1, 7)]
    return cols


def fit(ds: list[dict[str, Any]], extra: list[np.ndarray], lag: int = HAC_LAG) -> tuple[np.ndarray, np.ndarray]:
    y = np.array([r["y"] for r in ds])
    return ols_hac(y, controls(ds) + extra, lag)


def beta_line(label: str, ds: list[dict[str, Any]], x_of: Any, lag: int = HAC_LAG) -> tuple[str, float, float]:
    x = np.array([x_of(r) for r in ds], dtype=float)
    c, v = fit(ds, [x], lag)
    b, se = float(c[-1]), float(math.sqrt(v[-1, -1]))
    return (
        f"{label:<44} n={len(ds):5d} share={x.mean():.3f}  beta={b:+.4f}  se={se:.4f}  CI=[{b - 1.96 * se:+.4f}, {b + 1.96 * se:+.4f}]",
        b,
        se,
    )


# ----------------------------------------------------------------------------- gates


def stage_gates(cache: Path, L: list[str]) -> None:
    allrows = json.loads((cache / "daily_gamma.json").read_text())
    span = [r for r in allrows if START.isoformat() <= r["d"] <= END.isoformat()]
    n_days = (END - START).days + 1
    ok = sum(1 for r in span if r["n_file"] >= MIN_TRADES)
    L.append(
        f"G1 coverage: {ok}/{n_days} days with >= {MIN_TRADES} prints = {ok / n_days:.4f} (files present {len(span)}); "
        f"median prints {int(np.median([r['n_file'] for r in span]))}, min {min(r['n_file'] for r in span)}"
    )
    # G2
    qe = json.loads((cache / "q_end.json").read_text())
    q = {n: list(v) for n, v in qe["q"].items()}
    gap_p = cache / "gap_trades.json.gz"
    live_p = cache / "live_oi.json"
    if not (gap_p.exists() and live_p.exists()):
        L.append("G2: live OI / gap prints missing -> unverifiable")
    else:
        gap = json.loads(gzip.decompress(gap_p.read_bytes()))
        live = json.loads(live_p.read_text())
        for t in gap:
            if int(t["timestamp"]) <= int(qe["t_end_ms"]) or parse_option(t["instrument_name"]) is None:
                continue
            a = float(t["amount"] or 0.0)
            s = q.setdefault(t["instrument_name"], [0.0, 0.0])
            s[0] += a if t["direction"] == "buy" else -a
            s[1] += a
        names = [n for n, oi in live["oi"].items() if oi > 0 and parse_option(n) is not None]
        aq = [abs(q.get(n, [0.0, 0.0])[0]) for n in names]
        gr = [q.get(n, [0.0, 0.0])[1] for n in names]
        oi = [live["oi"][n] for n in names]
        rho_q, rho_g = spearman(aq, oi), spearman(gr, oi)
        no_flow = sum(1 for n in names if n not in q)
        over = sum(1 for a, o in zip(aq, oi, strict=True) if a > o)
        signs = [(n, q[n][0]) for n in names if n in q and q[n][0] != 0]
        heur = sum(1 for n, v in signs if (-v > 0) == parse_option(n).is_call)  # type: ignore[union-attr]
        L.append(
            f"G2 inventory vs live OI ({datetime.fromtimestamp(live['ts_ms'] / 1000, UTC):%Y-%m-%d %H:%M} UTC, gap prints {len(gap)}): "
            f"series with OI>0 n={len(names)}; Spearman(|Q|, OI)={rho_q:.3f}; reference Spearman(gross volume, OI)={rho_g:.3f}; "
            f"series without any print {no_flow}; |Q|>OI in {over}/{len(names)}; sum|Q|/sum OI={sum(aq) / sum(oi):.3f}"
        )
        L.append(
            f"   diagnostic (cond. 3): sign of -Q agrees with 'dealer long call / short put' in {heur}/{len(signs)} = {heur / max(1, len(signs)):.3f}"
        )
    # G3
    ds, drop = build_dataset(cache)
    y = np.array([r["y"] for r in ds])
    Z = controls(ds)
    Zm = np.column_stack(Z)
    e = y - Zm @ np.linalg.lstsq(Zm, y, rcond=None)[0]
    L.append(f"G3 dataset: n={len(ds)} days {ds[0]['d']} ... {ds[-1]['d']}; dropped {drop}")
    for label, sub in (("full", ds), ("IS", [r for r in ds if r["d"] < SPLIT]), ("OOS", [r for r in ds if r["d"] >= SPLIT])):
        ys = np.array([r["y"] for r in sub])
        Zs = np.column_stack(controls(sub))
        es = ys - Zs @ np.linalg.lstsq(Zs, ys, rcond=None)[0]
        x = np.array([1.0 if r["g"] < 0 else 0.0 for r in sub])
        se = hac_se_indicator(x, Zs, es, HAC_LAG)
        pers = sign_persistence([r["g"] < 0 for r in sub])
        p = pers["share"]
        mde_neff = 2.8 * float(np.std(es, ddof=Zs.shape[1])) / math.sqrt(pers["n_eff"] * p * (1 - p)) if 0 < p < 1 else float("inf")
        r2 = 1 - float(es @ es) / float(((ys - ys.mean()) ** 2).sum())
        L.append(
            f"G3 {label:<4}: n={len(sub)} SD(resid HAR+DVOL)={np.std(es, ddof=Zs.shape[1]):.4f} (R2 base {r2:.3f}); share G<0={p:.3f}; "
            f"sign changes {pers['changes']} = {pers['changes_per_year']:.1f}/yr; rho1={pers['rho1']:.3f}; n_eff={pers['n_eff']:.0f}; "
            f"SE_HAC(lag5)={se:.4f} -> MDE=2.8*SE={2.8 * se:.4f}; MDE(n_eff)={mde_neff:.4f}"
        )
    _ = e
    ex = [r["excl_share"] for r in ds]
    L.append(f"   IV rule N={IV_AGE_H}h: excluded |Q| share per day median {np.median(ex):.4f}, p95 {np.percentile(ex, 95):.4f}")


# ----------------------------------------------------------------------------- test


def stage_test(cache: Path, L: list[str], n_perm: int, n_boot: int) -> None:
    rng = np.random.default_rng(20261025)
    card = (Path(__file__).resolve().parents[1] / "docs/ideas/IDEA-20261025-gex-taker-flow-range.md").read_text()
    sec = card[card.index("## Попередня реєстрація") : card.index("## Вердикт")]
    L.append(f"pre-registration section sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")
    ds, drop = build_dataset(cache)
    IS = [r for r in ds if r["d"] < SPLIT]
    OOS = [r for r in ds if r["d"] >= SPLIT]
    neg = lambda r: 1.0 if r["g"] < 0 else 0.0  # noqa: E731
    L.append(f"dataset n={len(ds)} (IS {len(IS)}, OOS {len(OOS)}), dropped {drop}")
    L.append("== H1: y = ln Parkinson vol next 24h ~ HAR(ln RV1,7,30) + ln DVOL(T0) + DOW + monthly/quarterly expiry + 1[G<0]; NW lag 5")
    res: dict[str, tuple[float, float]] = {}
    for lab, sub in (("full 2022-01-01..2026-10-12", ds), ("IS 2022..2024", IS), ("OOS 2025..2026-10-12", OOS)):
        s, b, se = beta_line(f"H1 {lab}", sub, neg)
        res[lab.split()[0]] = (b, se)
        L.append(s)
    bF, seF = res["full"]
    bI, bO = res["IS"][0], res["OOS"][0]
    s20, _, se20 = beta_line("H1 full, NW lag 20 (added condition)", ds, neg, 20)
    L.append(s20)
    h1_dir = bF - 1.96 * seF > 0 and bF - 1.96 * se20 > 0 and np.sign(bI) == np.sign(bO) and bO >= 0.04
    h1 = "PASS" if h1_dir and bF >= 0.08 else ("DIRECTIONAL (beta < 0.08)" if h1_dir else "FAIL")
    underpowered = h1 == "FAIL" and bF - 1.96 * seF <= 0 < bF + 1.96 * seF and bF + 1.96 * seF >= 0.08
    L.append(f"H1 verdict: {h1}  (needs CI_lo>0 full at lag 5 and 20, sign IS=OOS, OOS>=0.04, full beta>=0.08)")
    L.append(f"H1 underpowered rule (CI contains 0 and CI_hi >= 0.08): {underpowered}; CI_hi full = {bF + 1.96 * seF:+.4f}")
    # OOS R2 gain
    y_is, y_oos = np.array([r["y"] for r in IS]), np.array([r["y"] for r in OOS])
    Zi, Zo = np.column_stack(controls(IS)), np.column_stack(controls(OOS))
    xi, xo = np.array([neg(r) for r in IS]), np.array([neg(r) for r in OOS])
    cb = np.linalg.lstsq(Zi, y_is, rcond=None)[0]
    cg = np.linalg.lstsq(np.column_stack([Zi, xi]), y_is, rcond=None)[0]
    sst = float(((y_oos - y_oos.mean()) ** 2).sum())
    r2b = 1 - float(((y_oos - Zo @ cb) ** 2).sum()) / sst
    r2g = 1 - float(((y_oos - np.column_stack([Zo, xo]) @ cg) ** 2).sum()) / sst
    L.append(f"OOS R2 (fit on IS): baseline {r2b:.4f}, +1[G<0] {r2g:.4f}, gain {100 * (r2g - r2b):+.2f} pp (card: >= +1 pp)")
    # H3
    L.append("== H3 placebo")
    y = np.array([r["y"] for r in ds])
    Zf = np.column_stack(controls(ds))
    x = np.array([neg(r) for r in ds])
    months = [(r["d"].year, r["d"].month) for r in ds]
    perm = []
    for _ in range(n_perm):
        xp = permute_within(x, months, rng)
        perm.append(float(np.linalg.lstsq(np.column_stack([Zf, xp]), y, rcond=None)[0][-1]))
    pa = np.array(perm)
    p_two = float((np.sum(np.abs(pa) >= abs(bF)) + 1) / (n_perm + 1))
    L.append(
        f"H3a within-month permutation ({n_perm}): beta_perm 95% interval [{np.percentile(pa, 2.5):+.4f}, {np.percentile(pa, 97.5):+.4f}], "
        f"mean {pa.mean():+.4f}; observed {bF:+.4f}; two-sided p={p_two:.4f}"
    )
    dsl = [r for r in ds if r["g_lag7"] is not None]
    s, bS, seS = beta_line("H3b shifted regime 1[G(D-7)<0] alone", dsl, lambda r: 1.0 if r["g_lag7"] < 0 else 0.0)
    L.append(s)
    cj, vj = fit(dsl, [np.array([neg(r) for r in dsl]), np.array([1.0 if r["g_lag7"] < 0 else 0.0 for r in dsl])])
    bj, sej = float(cj[-2]), math.sqrt(float(vj[-2, -2]))
    L.append(
        f"H3b joint: beta_neg={bj:+.4f} se={sej:.4f} CI=[{bj - 1.96 * sej:+.4f}, {bj + 1.96 * sej:+.4f}]; beta_shift={float(cj[-1]):+.4f} se={math.sqrt(float(vj[-1, -1])):.4f}"
    )
    agree = float(np.mean([(r["g"] < 0) == (r["g_lag7"] < 0) for r in dsl]))
    h3b = (bF > 0 and bS < 0.5 * bF) or (bj - 1.96 * sej > 0)
    h3 = "PASS" if p_two < 0.05 and h3b else "FAIL"
    L.append(f"H3 verdict: {h3} (perm p<0.05 and [beta_shift < 0.5*beta_neg or joint CI_lo>0]); regime agreement D vs D-7: {agree:.3f}")
    # H2 (exploratory)
    L.append("== H2 (exploratory): normalised lag-1 autocovariance of hourly returns, v0 = RV1(T0)^2/24")
    for lab, sub in (("full", ds), ("IS", IS), ("OOS", OOS)):
        a_pos = np.array([h2_day_stat(r["rets"], math.exp(2 * r["rv1"]) / 24) for r in sub if r["g"] >= 0])
        a_neg = np.array([h2_day_stat(r["rets"], math.exp(2 * r["rv1"]) / 24) for r in sub if r["g"] < 0])
        diff = a_pos.mean() - a_neg.mean()
        bs = [rng.choice(a_pos, len(a_pos)).mean() - rng.choice(a_neg, len(a_neg)).mean() for _ in range(n_boot)]
        rho = {}
        for k, cond in (("pos", lambda r: r["g"] >= 0), ("neg", lambda r: r["g"] < 0)):
            num = sum(sum(rr[i] * rr[i - 1] for i in range(1, 24)) for rr in (r["rets"] for r in sub if cond(r)))
            den = sum(sum(v * v for v in rr) for rr in (r["rets"] for r in sub if cond(r)))
            rho[k] = num / den
        L.append(
            f"H2 {lab:<4}: days +{len(a_pos)} / -{len(a_neg)}; a_pos={a_pos.mean():+.4f} a_neg={a_neg.mean():+.4f} diff={diff:+.4f} "
            f"boot95=[{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]; pooled rho pos={rho['pos']:+.4f} neg={rho['neg']:+.4f} (diff {rho['pos'] - rho['neg']:+.4f})"
        )
    # H4 (exploratory)
    deliv = {str(r.get("date")): float(r.get("delivery_price")) for r in json.loads(DELIVERY.read_text()) if r.get("delivery_price")}
    pairs = []
    for r in ds:
        st = deliv.get((r["d"] + timedelta(days=1)).isoformat())
        if st is None:
            continue
        pd_ = pin_distance({float(k): v for k, v in r["prof"].items()}, r["strikes"], r["spot"], st)
        if pd_:
            pairs.append(pd_)
    if pairs:
        dd = np.array([a - b for a, b in pairs])
        bs = [rng.choice(dd, len(dd)).mean() for _ in range(n_boot)]
        L.append(
            f"== H4 (exploratory) pinning: n={len(dd)} expiries; mean |settle-K*|/S={np.mean([a for a, _ in pairs]):.4f} vs random in-band strike "
            f"{np.mean([b for _, b in pairs]):.4f}; diff={dd.mean():+.4f} boot95=[{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]; share K* closer {np.mean(dd < 0):.3f}"
        )
    # sensitivity / robustness (descriptive)
    L.append("== Sensitivity and robustness (descriptive, not in the verdict)")
    var = [
        ("IV age N=6h", lambda r: 1.0 if r["g6"] < 0 else 0.0, ds),
        ("no prints >= 100 BTC", lambda r: 1.0 if r["gnb"] < 0 else 0.0, ds),
        ("regime G < -1 SD(90d)", lambda r: 1.0 if r["g"] < -r["sd90"] else 0.0, [r for r in ds if r["sd90"]]),
        ("continuous z(G, 90d) (beta per 1 z)", lambda r: r["z"], [r for r in ds if r["z"] is not None]),
        ("HAC lag 10", neg, ds),
        ("without 2022 (burn-in)", neg, [r for r in ds if r["d"].year >= 2023]),
        ("days with excluded |Q| share < 5%", neg, [r for r in ds if r["excl_share"] < 0.05]),
    ]
    for lab, xf, sub in var:
        L.append(beta_line(lab, sub, xf, 10 if "lag 10" in lab else HAC_LAG)[0])
        if lab.startswith(("IV age", "no prints")):
            for l2, s2 in (("IS", [r for r in sub if r["d"] < SPLIT]), ("OOS", [r for r in sub if r["d"] >= SPLIT])):
                L.append("   " + beta_line(f"{lab} {l2}", s2, xf)[0])
    e = y - Zf @ np.linalg.lstsq(Zf, y, rcond=None)[0]
    cut = np.percentile(np.abs(e), 98)
    L.append(beta_line("without top 2% |baseline residual| days", [r for r, ee in zip(ds, e, strict=True) if abs(ee) <= cut], neg)[0])
    for yr in range(2022, 2027):
        sub = [r for r in ds if r["d"].year == yr]
        L.append(beta_line(f"year {yr}", sub, neg)[0])
    L.append(
        f"corr(1[G<0], ln DVOL) = {np.corrcoef(x, np.array([r['ldvol'] for r in ds]))[0, 1]:+.3f}; corr(1[G<0], ln RV7) = "
        f"{np.corrcoef(x, np.array([r['rv7'] for r in ds]))[0, 1]:+.3f}; corr(1[G<0], monthly flag) = {np.corrcoef(x, np.array([r['mo'] for r in ds]))[0, 1]:+.3f}"
    )
    decision = (
        "confirmed" if h1 == "PASS" and h3 == "PASS" else ("inconclusive" if h1.startswith("DIRECTIONAL") or underpowered else "rejected")
    )
    L.append(f"verdicts: H1 {h1}; H3 {h3}; registered decision: {decision}")


# ----------------------------------------------------------------------------- robust (post-run)


def _acf(a: np.ndarray, k: int) -> float:
    a = a - a.mean()
    return float(a[k:] @ a[:-k] / (a @ a))


def stage_robust(cache: Path, L: list[str], n_boot: int, seed: int = 7) -> None:
    """Robust-SE check of the full-sample H1 beta. ADDED AFTER THE RUN (critic review B1); not part of the verdict."""
    L.append("NOTE: added after the registered --stage test run (review 2026-10-25, B1); descriptive, not in the verdict")
    ds, _ = build_dataset(cache)
    neg = np.array([1.0 if r["g"] < 0 else 0.0 for r in ds])
    y = np.array([r["y"] for r in ds])
    zc = controls(ds)
    Z = np.column_stack(zc)
    X = np.column_stack([Z, neg])
    c = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ c
    b = float(c[-1])
    n = len(ds)
    L.append(f"H1 full n={n} beta={b:+.4f}")
    xt = neg - Z @ np.linalg.lstsq(Z, neg, rcond=None)[0]
    u = xt * e
    L.append("acf residual e, lags 1/2/5/10/20: " + " / ".join(f"{_acf(e, k):+.3f}" for k in (1, 2, 5, 10, 20)))
    L.append("acf regime x~ (after controls), lags 1/5/20/60: " + " / ".join(f"{_acf(xt, k):+.3f}" for k in (1, 5, 20, 60)))
    L.append("acf score u = x~*e, lags 1/5/20: " + " / ".join(f"{_acf(u, k):+.3f}" for k in (1, 5, 20)))
    for lag in (5, 20, 60, 120):
        _, vv = ols_hac(y, zc + [neg], lag)
        se = math.sqrt(vv[-1, -1])
        L.append(f"Newey-West lag {lag:3d}: se={se:.4f}  CI=[{b - 1.96 * se:+.4f}, {b + 1.96 * se:+.4f}]")
    rng = np.random.default_rng(seed)
    for blk in (30, 60, 120):
        bs = []
        for _ in range(n_boot):
            starts = rng.integers(0, n - blk + 1, size=math.ceil(n / blk))
            idx = np.concatenate([np.arange(s, s + blk) for s in starts])[:n]
            if X[idx, -1].std() == 0:
                continue
            bs.append(np.linalg.lstsq(X[idx], y[idx], rcond=None)[0][-1])
        arr = np.array(bs)
        sd = float(arr.std(ddof=1))
        L.append(
            f"moving-block bootstrap block {blk:3d}d ({len(arr)} reps, seed {seed}): se={sd:.4f}  "
            f"pct95=[{np.percentile(arr, 2.5):+.4f}, {np.percentile(arr, 97.5):+.4f}]  normal CI_hi={b + 1.96 * sd:+.4f}"
        )
    runs: list[int] = []
    cur = 0
    years: dict[str, int] = {}
    for r in ds:
        if r["g"] >= 0:
            cur += 1
            yr = str(r["d"].year)
            years[yr] = years.get(yr, 0) + 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    L.append(
        f"G>=0 days {sum(runs)} in {len(runs)} episodes (median length {np.median(runs):.0f}, max {max(runs)}); "
        "by year " + ", ".join(f"{k}: {years.get(k, 0)}" for k in ("2022", "2023", "2024", "2025", "2026"))
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["build", "gap", "bars", "gates", "test", "robust"])
    ap.add_argument("--cache-dir", type=Path, default=CACHE_ROOT / "gex_taker_flow")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--limit-files", type=int)
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    if a.stage == "build":
        stage_build(cache, a.limit_files)
        return
    if a.stage == "gap":
        asyncio.run(stage_gap(cache))
        return
    if a.stage == "bars":
        asyncio.run(stage_bars(cache))
        return
    L: list[str] = [f"# verify_gex_taker_flow --stage {a.stage}  {datetime.now(UTC).isoformat(timespec='seconds')}"]
    if a.stage == "gates":
        stage_gates(cache, L)
    elif a.stage == "robust":
        stage_robust(cache, L, 1000)
    else:
        stage_test(cache, L, a.perm, a.boot)
    L.append(f"elapsed {time.perf_counter() - t:.0f}s, rss {rss_mb():.0f} MB")
    txt = "\n".join(L) + "\n"
    print(txt, flush=True)
    if a.out:
        a.out.expanduser().write_text(txt)


if __name__ == "__main__":
    main()
