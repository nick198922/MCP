#!/usr/bin/env python3
"""S28 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): H9 (mirrored shorts), H7 (structural stop under the
zone), H6 (1h confirmation entry after the touch), H5 (limit distance, Spearman with R) and H3 (FVG size in ATR,
Spearman with R), each against the S28 passport base, with the registered placebos (random limits of the same coin,
side and geometry; permuted stop distance / entry delay), Holm over 5, and the diagnostic D1 (R hedged with the
equal-weight top-100 basket). Registration: the card's section «Попередня реєстрація модулів S28».

    uv run python scripts/verify_s28_modules.py --stage sanity     # timing of 3 coins, row counts (no R printed)
    uv run python scripts/verify_s28_modules.py --stage run        # per-symbol rows (local caches, 4 workers)
    uv run python scripts/verify_s28_modules.py --stage placebo    # own placebos of H7 / H6 (permutations on V)
    uv run python scripts/verify_s28_modules.py --stage report --out <file>

No network: 1h bars from the S14 cache, BTC trend / regimes from the S28 passport cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s28_modules as m28  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import chi2_sf, cochran_q, risk_unit  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
BAR4 = 4 * HOUR_MS
HOLD = 7 * DAY_MS
END = vsp.END["4h"]
SETUP = "S28"
MODULES = ("H9", "H7", "H6", "H5", "H3")
INTEREST = {"H9": 0.10, "H7": 0.10, "H6": 0.10, "H5": 0.06, "H3": 0.06}
MDE_K = 3.17  # z(0.99) + z(0.80): the strictest Holm step over 5, 80 % power
K_PLC = 3
TOP_BASKET = 100
emit = vsp.emit


def guard(side: str, e: float, st: float, tg: float) -> bool:
    """The demo guard on a geometry in real prices: stop <= 10 % and R:R after taker fees >= 1.5."""
    return abs(e - st) / abs(e) <= vsp.MAX_STOP and reward_risk_at(side, e, st, tg) >= vsp.MIN_RR


def guard_fn(side: str, sg: float) -> Any:
    """``guard`` for a geometry in the trade's (long or mirrored) price space."""

    def ok(en: float, sl: float, tp: float) -> bool:
        return guard(side, sg * en, sg * sl, sg * tp)

    return ok


def draw(key: str, k: int, size: int) -> int:
    h = hashlib.sha256(f"s28plc|{key}|{k}".encode()).digest()
    return int.from_bytes(h[:8], "big") % size


def pct(r: float, e: float, st: float) -> float:
    """R as a fraction of the entry price (long-space prices)."""
    return r * risk_unit(e, st) / abs(e) if math.isfinite(r) else math.nan


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, pp_dir, out_dir = args
    t0w = time.time()
    s1c, dd, pp = Path(s1_cache), Path(data_dir), Path(pp_dir)
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    btc = json.loads((pp / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    tb = resample(h1, 4)
    pos = segment_pos(tb.t)
    a_arr = atr(tb, 14, pos)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    close_t = tb.t + tb.bar_ms
    pool = np.flatnonzero((pos >= ss.S28_WARMUP) & (close_t >= vsp.START_MS) & (close_t < END))

    def ctx(t_in: int, side: str, e: float, st: float, tg: float) -> dict[str, Any] | None:
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            return None
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at(side, e, st, tg)
        sc = sp.score_points(side, rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC)
        return {
            "sym": s,
            "side": side,
            "t_in": t_in,
            "top30": int(top),
            "turn": turn,
            "grp": grp,
            "rr": rr,
            "stop_pct": abs(e - st) / abs(e),
            "score": sc,
        }

    for side, kind in (("long", "base"), ("short", "H9")):
        bb = tb if side == "long" else tb.mirrored()
        path = h1 if side == "long" else h1.mirrored()
        sg = 1.0 if side == "long" else -1.0
        sigs = ss.s28_all(tb, "4h") if side == "long" else m28.mirror_signals(tb, "4h")
        for sig in sigs:  # entry / stop / target in the trade's (long or mirrored) space
            why[f"{kind}:signal"] += 1
            j = int(np.searchsorted(bb.t, sig.signal_ts))
            t0 = sig.signal_ts + bb.bar_ms
            if not (vsp.START_MS <= t0 < END):
                continue
            t1 = int(sig.indicators["rest_until_ms"])
            e, st, tg = sig.entry, sig.stop, sig.target
            key = f"{s}|{side}|{t0}"
            c_sig = float(bb.c[j])
            for k in range(K_PLC):  # random limits of the same coin, side and geometry (before any filter)
                if pool.size == 0:
                    break
                r = int(pool[draw(key, k, pool.size)])
                c_r = float(bb.c[r])
                e_r, st_r = c_r * e / c_sig, c_r * st / c_sig
                tg_r = e_r + 2.0 * (e_r - st_r)
                tr0 = int(bb.t[r]) + bb.bar_ms
                x = ctx(tr0, side, sg * e_r, sg * st_r, sg * tg_r)
                if x is None or not (st_r < e_r < tg_r):
                    continue
                o, tk = sp.limit_trade(path, tr0, tr0 + (t1 - t0), e_r, st_r, tg_r, HOLD, vsp.RULE_B, vsp.F1)
                if o is None and tk >= 0:
                    continue
                plc.append(
                    {
                        **x,
                        "k": key,
                        "kind": kind,
                        "filled": int(o is not None),
                        "B": o.r_net if o else None,
                        "dist": m28.limit_distance(c_r, e_r, float(a_arr[r])),
                    }
                )
            x = ctx(t0, side, sg * e, sg * st, sg * tg)
            if x is None:
                why[f"{kind}:under_1m_or_unknown"] += 1
                continue
            if not c_sig > e:
                why[f"{kind}:at_level"] += 1
                continue
            b, tk = sp.limit_trade(path, t0, t1, e, st, tg, HOLD, vsp.RULE_B, vsp.F1)
            if b is None and tk >= 0:
                why[f"{kind}:no_path"] += 1
                continue
            row: dict[str, Any] = {
                **x,
                "k": key,
                "kind": kind,
                "e": e,
                "st": st,
                "filled": int(b is not None),
                "t_fill": tk,
                "B": b.r_net if b else None,
                "B_k": b.exit_kind if b else "unfilled",
                "B_x": b.exit_ms if b else t1,
            }
            if b is not None:
                b15, _ = sp.limit_trade(path, t0, t1, e, st, tg, HOLD, vsp.RULE_B, vsp.F15)
                row["B15"] = b15.r_net if b15 else None
            if kind == "base":
                i_ob = int(np.searchsorted(bb.t, int(sig.indicators["ob_ts"])))
                ie = i_ob + 1
                atr_e = float(sig.indicators["atr14"])
                row["dist"] = m28.limit_distance(c_sig, e, float(a_arr[j]))
                row["fvg"] = m28.fvg_size(bb, ie) / atr_e
                row["age"] = j - ie
                row["atr_e"] = atr_e
                row["t1"] = t1
                if b is not None:
                    # H7: structural stop (the base stop if the guard rejects it)
                    st7 = m28.structural_stop(float(sig.indicators["ob_low"]), float(bb.low[ie]), atr_e)
                    tg7 = e + 2.0 * (e - st7)
                    if not guard(side, sg * e, sg * st7, sg * tg7):
                        st7, tg7 = st, tg
                    o7, _ = sp.limit_trade(path, t0, t1, e, st7, tg7, HOLD, vsp.RULE_B, vsp.F1)
                    o715, _ = sp.limit_trade(path, t0, t1, e, st7, tg7, HOLD, vsp.RULE_B, vsp.F15)
                    row.update(R7=o7.r_net if o7 else None, R7_15=o715.r_net if o715 else None, d7=(e - st7) / atr_e, st7=st7)

                    # H6: entry on a 1h close above the level and the touch bar's high
                    ok = guard_fn(side, sg)
                    o6, c6, _ = m28.confirm_trade(path, t0, t1, e, atr_e, HOLD, vsp.RULE_B, vsp.F1, ok)
                    o615, _, _ = m28.confirm_trade(path, t0, t1, e, atr_e, HOLD, vsp.RULE_B, vsp.F15, ok)
                    row.update(
                        R6=o6.r_net if o6 else 0.0,
                        R6_15=o615.r_net if o615 else 0.0,
                        delay6=(c6.m - c6.k) if (o6 is not None and c6 is not None) else -1,
                        P6=pct(o6.r_net, c6.entry, c6.stop) if (o6 is not None and c6 is not None) else 0.0,
                    )
            rows.append(row)
            why[f"{kind}:cand"] += 1
            why[f"{kind}:filled"] += int(b is not None)
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "plc": plc, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0w, "rss": vsp.rss_mb()}


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / SETUP), str(out / "sanity")))
        emit(f"timing {s}: {r['sec']:.1f}s; why {r['why']}; rss {r['rss']:.0f} MB")
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.1f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [
        (s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / SETUP), str(out))
        for s in syms
        if a.force or not (out / "rows" / f"{s}.json").exists()
    ]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- samples


STR_KEYS = ("k", "sym", "side", "kind", "grp", "B_k")


def table(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    keys = sorted({k for r in rows for k in r})
    T: dict[str, np.ndarray] = {}
    for k in keys:
        col = [r.get(k) for r in rows]
        if k in STR_KEYS:
            T[k] = np.array(["" if x is None else str(x) for x in col])
        elif k in ("t_in", "B_x", "t_fill", "t1"):
            T[k] = np.array([-1 if x is None else int(x) for x in col], dtype=np.int64)
        else:
            T[k] = np.array([math.nan if x is None else float(x) for x in col])
    T["score"] = T["score"].astype(np.int64)
    T["filled"] = T["filled"] == 1.0
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = (T["kind"] != "base").astype(np.int64)
    return T


def load(out: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for p in sorted((out / "rows").glob("*.json")):
        d = json.loads(p.read_text())
        rows.extend(d["rows"])
        plc.extend(d["plc"])
        why.update(d["why"])
    return table(rows), table(plc), dict(why)


def selection(T: dict[str, np.ndarray], top: dict[int, set[str]], ranked: bool) -> np.ndarray:
    m = (
        (T["t_in"] >= vsp.PASSPORT_MS)
        & (T["rr"] >= vsp.MIN_RR)
        & (T["stop_pct"] <= vsp.MAX_STOP)
        & (T["score"] >= vsp.MIN_SCORE)
        & (np.nan_to_num(T["turn"], nan=-1) >= 5e6)
    )
    return m & vsp.in_prev_day_top(T, top) if ranked else m


def samples(
    a: argparse.Namespace, T: dict[str, np.ndarray]
) -> tuple[dict[str, np.ndarray], dict[int, set[str]], dict[str, list[tuple[int, float]]]]:
    elig = vsp.s1.eligible_syms(a.data_dir)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top = vsp.top_n_days(qv, vsp.TOP_N)
    v_ok, p_ok = selection(T, top, True), selection(T, top, False)
    base, h9 = T["kind"] == "base", T["kind"] == "H9"
    F = T["filled"]
    S = {"V": vsp.chain(T, base & v_ok) & F, "P": vsp.chain(T, base & p_ok) & F}
    S["V_H9"] = vsp.chain(T, (base | h9) & v_ok)
    S["P_H9"] = vsp.chain(T, (base | h9) & p_ok)
    S["add"] = S["V_H9"] & h9 & F
    S["addP"] = S["P_H9"] & h9 & F
    S["Vmod_base"] = S["V_H9"] & base & F
    return S, top, qv


# ---------------------------------------------------------------- own placebos (H7, H6)


def placebo_symbol(args: tuple[str, str, str, str, list[list[Any]]]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, jobs = args
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    res = []
    for job in jobs:
        mode, key, t0, t1, e, atr_e, v = job[0], job[1], int(job[2]), int(job[3]), float(job[4]), float(job[5]), job[6]
        if mode == "own7":  # the structural-stop distance (in ATR) of another trade
            st = e - float(v) * atr_e
            tg = e + 2.0 * (e - st)
            st0 = float(job[7])
            if not guard("long", e, st, tg):
                st, tg = st0, e + 2.0 * (e - st0)
            o, _ = sp.limit_trade(h1, t0, t1, e, st, tg, HOLD, vsp.RULE_B, vsp.F1)
            res.append({"m": mode, "k": key, "R": o.r_net if o else None})
        else:  # own6: the confirmation delay of another trade, entry at that close without the close condition
            d = int(v)
            if d < 0:
                res.append({"m": mode, "k": key, "R": 0.0})
                continue
            o, _, _ = m28.confirm_trade(h1, t0, t1, e, atr_e, HOLD, vsp.RULE_B, vsp.F1, guard_fn("long", 1.0), delay=d)
            res.append({"m": mode, "k": key, "R": o.r_net if o else 0.0})
    outp = Path(out_dir) / "placebo"
    outp.mkdir(parents=True, exist_ok=True)
    (outp / f"{s}.json").write_text(json.dumps(res, separators=(",", ":")))
    return {"sym": s, "n": len(res), "rss": vsp.rss_mb()}


def stage_placebo(a: argparse.Namespace, out: Path) -> None:
    T, _, _ = load(out)
    S, _, _ = samples(a, T)
    by: dict[str, list[list[Any]]] = defaultdict(list)
    idx = np.flatnonzero(S["V"])  # V 2021+ filled base trades (the registered permutation set)
    d7 = sp_perm(T["d7"][idx])
    d6 = sp_perm(T["delay6"][idx])
    for i, dd7, dd6 in zip(idx, d7, d6, strict=True):
        base = [str(T["k"][i]), int(T["t_in"][i]), int(T["t1"][i]), float(T["e"][i]), float(T["atr_e"][i])]
        by[str(T["sym"][i])].append(["own7", *base, float(dd7), float(T["st"][i])])
        by[str(T["sym"][i])].append(["own6", *base, int(dd6)])
    emit(f"[placebo] jobs {sum(len(v) for v in by.values())}, coins {len(by)}")
    jobs = [(c, str(a.s1_cache), str(a.data_dir), str(out), v) for c, v in sorted(by.items())]
    v38.pool_run(placebo_symbol, jobs, a.workers, "placebo")


def sp_perm(v: np.ndarray) -> np.ndarray:
    return np.asarray(v)[np.random.default_rng(vsp.SEED).permutation(np.asarray(v).size)]


# ---------------------------------------------------------------- report helpers


def mci(v: np.ndarray, wk: np.ndarray) -> dict[str, float]:
    ok = np.isfinite(v)
    return sp.mean_ci(v[ok], wk[ok], n_boot=vsp.N_BOOT, seed=vsp.SEED)


def f(r: dict[str, float], key: str = "m") -> str:
    if not r or r.get("n", r.get("n1", 0)) == 0 or not math.isfinite(r.get(key, math.nan)):
        return "—"
    lo, hi = r.get("lo", math.nan), r.get("hi", math.nan)
    n = int(r.get("n", r.get("n1", 0)))
    ci = f" [{lo:+.3f}; {hi:+.3f}]" if math.isfinite(lo) else ""
    return f"{r[key]:+.3f}{ci}, n {n}"


def vs_placebo(v: np.ndarray, wk: np.ndarray, q: np.ndarray, q_wk: np.ndarray) -> dict[str, float]:
    """Mean of the signal trades minus the mean of their placebo trades, clustered by the signal's week (both
    samples resampled together)."""
    ina = np.concatenate([np.ones(v.size, bool), np.zeros(q.size, bool)])
    return sp.diff_ci(np.concatenate([v, q]), ina, ~ina, np.concatenate([wk, q_wk]), n_boot=vsp.N_BOOT, seed=vsp.SEED)


def p1(r: dict[str, float], key: str = "m") -> float:
    return m28.one_sided(r[key], r["p"])


def label(name: str, r: dict[str, float], passed: bool) -> str:
    if passed:
        return "ПРОЙШОВ"
    mde = MDE_K * r["se"]
    if r["hi"] < INTEREST[name]:
        return f"✗ (верхня межа {r['hi']:+.3f} < {INTEREST[name]:.2f}: корисний ефект виключено; MDE₈₀ {mde:.3f})"
    if mde > INTEREST[name]:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {INTEREST[name]:.2f})"
    return f"✗ (MDE₈₀ {mde:.3f})"


def drop_top5_mean(v: np.ndarray, sym: np.ndarray) -> float:
    ok = np.isfinite(v)
    v, sym = v[ok], sym[ok]
    sums = defaultdict(float)
    for x, s in zip(v, sym, strict=True):
        sums[str(s)] += float(x)
    top = {s for s, _ in sorted(sums.items(), key=lambda kv: -kv[1])[:5]}
    keep = ~np.isin(sym, list(top))
    return float(v[keep].mean()) if keep.any() else math.nan


def drop_top5_rho(x: np.ndarray, y: np.ndarray, sym: np.ndarray, sign: float) -> float:
    rx, ry = m28.ranks(x), m28.ranks(y)
    c = (rx - rx.mean()) * (ry - ry.mean()) * sign
    sums = defaultdict(float)
    for v, s in zip(c, sym, strict=True):
        sums[str(s)] += float(v)
    top = {s for s, _ in sorted(sums.items(), key=lambda kv: -kv[1])[:5]}
    keep = ~np.isin(sym, list(top))
    return sign * m28.spearman(x[keep], y[keep])


def per_asset_mean(name: str, v: np.ndarray, sym: np.ndarray, wk: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    syms = sorted(set(sym.tolist()))
    means, ses = [], []
    for s in syms:
        x = v[(sym == s) & np.isfinite(v)]
        if x.size >= 10 and x.std(ddof=1) > 0:
            means.append(x.mean())
            ses.append(x.std(ddof=1) / math.sqrt(x.size))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    rank = []
    for s in syms:
        x = v[(sym == s) & IS & np.isfinite(v)]
        if x.size >= 10:
            rank.append((x.mean(), s))
    rank.sort(reverse=True)
    top = {s for _, s in rank[:10]}
    rest = {s for _, s in rank} - top
    mt, mr = OOS & np.isin(sym, list(top)), OOS & np.isin(sym, list(rest))
    d = (
        sp.diff_ci(np.nan_to_num(v), mt, mr, wk, n_boot=vsp.N_BOOT, seed=vsp.SEED)
        if mt.any() and mr.any()
        else {"d": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan}
    )
    emit(
        f"5a {name}: монет з ≥ 10 угод {len(means)}; Cochran Q {q:.1f}, df {df}, p {chi2_sf(q, df):.3f}, I² {i2:.2f}; "
        f"топ-10 за IS (з {len(rank)}) на OOS {f(mci(v[mt], wk[mt]))} проти решти {f(mci(v[mr], wk[mr]))}; різниця {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}"
    )


def per_asset_rho(name: str, x: np.ndarray, y: np.ndarray, sym: np.ndarray, sign: float, IS: np.ndarray, OOS: np.ndarray) -> None:
    syms = sorted(set(sym.tolist()))
    zs, ses = [], []
    for s in syms:
        m = sym == s
        if m.sum() >= 10:
            r = sign * m28.spearman(x[m], y[m])
            if math.isfinite(r) and abs(r) < 1:
                zs.append(math.atanh(r))
                ses.append(1 / math.sqrt(m.sum() - 3))
    q, df, i2 = cochran_q(np.array(zs), np.array(ses))
    rank = []
    for s in syms:
        m = (sym == s) & IS
        if m.sum() >= 10:
            r = sign * m28.spearman(x[m], y[m])
            if math.isfinite(r):
                rank.append((r, s))
    rank.sort(reverse=True)
    top = {s for _, s in rank[:10]}
    rest = {s for _, s in rank} - top
    mt, mr = OOS & np.isin(sym, list(top)), OOS & np.isin(sym, list(rest))
    emit(
        f"5a {name}: монет з ≥ 10 угод {len(zs)}; Cochran Q (z Фішера) {q:.1f}, df {df}, p {chi2_sf(q, df):.3f}, I² {i2:.2f}; "
        f"топ-10 за IS (з {len(rank)}) на OOS ρ_adj {sign * m28.spearman(x[mt], y[mt]):+.3f} (n {int(mt.sum())}) проти решти {sign * m28.spearman(x[mr], y[mr]):+.3f} (n {int(mr.sum())})"
    )


# ---------------------------------------------------------------- report


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T, Q, why = load(out)
    S, top, qv = samples(a, T)
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    wk, sym = T["week"], T["sym"]
    yrs_all = (END - vsp.PASSPORT_MS) / (365.25 * DAY_MS)
    yrs_oos = (END - vsp.OOS_MS) / (365.25 * DAY_MS)
    V, P = S["V"], S["P"]
    emit(f"# Модулі S28: рядків {T['B'].size} ({dict(Counter(T['kind'].tolist()))}); плацебо-ліміток {Q['B'].size}; why {why}")
    # base reproduced to the trade
    pp: dict[tuple[str, int], list[float]] = defaultdict(list)
    for p in sorted((a.passport_cache / SETUP / "cands").glob("*.json")):
        for r in json.loads(p.read_text())["rows"]:
            if r.get("filled"):
                pp[(r["sym"], int(r["t_in"]))].append(float(r["B"]))
    idx = np.flatnonzero(V)
    miss = [i for i in idx if not any(abs(x - float(T["B"][i])) < 1e-9 for x in pp.get((str(sym[i]), int(T["t_in"][i])), []))]
    emit(f"база = паспорт: {idx.size - len(miss)} / {idx.size} угод V 2021+ (R збігся до 1e-9; ключ монета + час розміщення)")
    for i in miss:
        key = (str(sym[i]), int(T["t_in"][i]))
        emit(f"  розбіжність {key[0]} {vsp.iso(key[1])}: тут {float(T['B'][i]):+.4f}, паспорт {pp.get(key)}")
    dup = sum(1 for i in idx if len(pp.get((str(sym[i]), int(T["t_in"][i])), [])) > 1)
    emit(f"  угод V, у яких у паспорті кілька заповнених розміщень з тим самим ключем: {dup}")
    rV, rI, rO = mci(T["B"][V], wk[V]), mci(T["B"][V & IS], wk[V & IS]), mci(T["B"][V & OOS], wk[V & OOS])
    emit(
        f"база V: 2021+ {f(rV)}; IS {f(rI)}; OOS {f(rO)}; P 2021+ {f(mci(T['B'][P], wk[P]))}; сума R/рік V {np.nansum(T['B'][V]) / yrs_all:+.1f}"
    )
    res: dict[str, dict[str, Any]] = {}

    # ---- H9: added mirrored shorts
    A, AP = S["add"], S["addP"]
    B = T["B"]
    r9 = mci(B[A], wk[A])
    qk = set(T["k"][A].tolist())
    qsel = selection(Q, top, True) & Q["filled"] & (Q["kind"] == "H9") & np.isin(Q["k"], list(qk))
    # placebo rows clustered by their signal's week
    sig_week = {str(k): int(w) for k, w in zip(T["k"], wk, strict=True)}
    q_wk = np.array([sig_week.get(str(k), -1) for k in Q["k"]])
    ao = A & OOS
    qsel_o = qsel & np.isin(Q["k"], list(set(T["k"][ao].tolist())))
    d9, d9o = (vs_placebo(B[sm], wk[sm], Q["B"][qm], q_wk[qm]) for sm, qm in ((A, qsel), (ao, qsel_o)))
    vmod = S["V_H9"] & T["filled"]
    res["H9"] = {
        "main": r9,
        "plc": d9["d"],
        "plc_p": m28.one_sided(d9["d"], d9["p"]),
        "IS": mci(B[A & IS], wk[A & IS])["m"],
        "OOS": mci(B[ao], wk[ao]),
        "y25": float(np.nanmean(B[ao & (T["year"] == 2025)])),
        "y26": float(np.nanmean(B[ao & (T["year"] == 2026)])),
        "drop5": drop_top5_mean(B[A], sym[A]),
        "P": float(np.nanmean(B[AP])),
        "x15": float(np.nanmean(T["B15"][ao])),
    }
    emit("\n## H9 дзеркальні шорти (додані на V_mod)")
    emit(f"додані 2021+ {f(r9)}; IS {f(mci(B[A & IS], wk[A & IS]))}; OOS {f(res['H9']['OOS'])}; P {f(mci(B[AP], wk[AP]))}")
    emit(
        f"розміщень H9 у V_mod {int((S['V_H9'] & (T['kind'] == 'H9')).sum())}, заповнено {int(A.sum())}; угод бази V витіснено {int((V & ~S['Vmod_base']).sum())}, нових угод бази в V_mod {int((S['Vmod_base'] & ~V).sum())}"
    )
    emit(
        f"угод на рік: база {V.sum() / yrs_all:.0f}, додані {A.sum() / yrs_all:.0f}; сума R/рік V {np.nansum(B[V]) / yrs_all:+.1f} → V_mod {np.nansum(B[vmod]) / yrs_all:+.1f}; OOS {np.nansum(B[V & OOS]) / yrs_oos:+.1f} → {np.nansum(B[vmod & OOS]) / yrs_oos:+.1f}"
    )
    emit(
        f"проти плацебо-шортів тієї ж геометрії (заповнених {int(qsel.sum())}): 2021+ {d9['d']:+.3f} [{d9['lo']:+.3f}; {d9['hi']:+.3f}] p₁ {res['H9']['plc_p']:.3f}; OOS {d9o['d']:+.3f} [{d9o['lo']:+.3f}; {d9o['hi']:+.3f}]; E[R] плацебо 2021+ {np.nanmean(Q['B'][qsel]):+.3f}"
    )
    emit(
        "роки (додані): "
        + ", ".join(f"{y} {np.nanmean(B[A & (T['year'] == y)]):+.3f} (n {int((A & (T['year'] == y)).sum())})" for y in range(2021, 2027))
    )
    emit(
        f"×1.5 OOS {res['H9']['x15']:+.3f}; % ціни OOS {np.nanmean([pct(r, e, s_) for r, e, s_ in zip(B[ao], T['e'][ao], T['st'][ao], strict=True)]) * 100:+.3f} %"
    )
    per_asset_mean("H9", B[A], sym[A], wk[A], IS[A], OOS[A])

    # ---- H7 / H6: paired on V
    for name, col, col15 in (("H7", "R7", "R7_15"), ("H6", "R6", "R6_15")):
        D = T[col] - B
        r = mci(D[V], wk[V])
        plc_rows = [json.loads(p.read_text()) for p in sorted((out / "placebo").glob("*.json"))]
        own = {x["k"]: x["R"] for rows in plc_rows for x in rows if x["m"] == ("own7" if name == "H7" else "own6")}
        R_plc = np.array([math.nan if own.get(str(k)) is None else float(own[str(k)]) for k in T["k"]])
        dp = T[col] - R_plc
        rp = mci(dp[V], wk[V])
        res[name] = {
            "main": r,
            "plc": rp["m"],
            "plc_p": p1(rp),
            "IS": mci(D[V & IS], wk[V & IS])["m"],
            "OOS": mci(D[V & OOS], wk[V & OOS]),
            "y25": float(np.nanmean(D[V & OOS & (T["year"] == 2025)])),
            "y26": float(np.nanmean(D[V & OOS & (T["year"] == 2026)])),
            "drop5": drop_top5_mean(D[V], sym[V]),
            "P": float(np.nanmean(D[P])),
            "x15": float(np.nanmean(T[col15][V & OOS])),
        }
        emit(f"\n## {name} (попарно на V)")
        emit(f"Δ 2021+ {f(r)}; IS {res[name]['IS']:+.3f}; OOS {f(res[name]['OOS'])}; P {res[name]['P']:+.3f}")
        emit(
            f"E[R] модуля 2021+ {np.nanmean(T[col][V]):+.3f}, OOS {np.nanmean(T[col][V & OOS]):+.3f} (база {np.nanmean(B[V & OOS]):+.3f}); ×1.5 OOS {res[name]['x15']:+.3f} (база {np.nanmean(T['B15'][V & OOS]):+.3f})"
        )
        emit(
            f"сума R/рік V 2021+ {np.nansum(B[V]) / yrs_all:+.1f} → {np.nansum(T[col][V]) / yrs_all:+.1f}; OOS {np.nansum(B[V & OOS]) / yrs_oos:+.1f} → {np.nansum(T[col][V & OOS]) / yrs_oos:+.1f}"
        )
        emit(f"проти власного плацебо: {f(rp)}, p₁ {res[name]['plc_p']:.3f}; E[R] плацебо 2021+ {np.nanmean(R_plc[V]):+.3f}")
        emit("роки Δ: " + ", ".join(f"{y} {np.nanmean(D[V & (T['year'] == y)]):+.3f}" for y in range(2021, 2027)))
        base_pct = np.array([pct(x, e, s_) for x, e, s_ in zip(B[V], T["e"][V], T["st"][V], strict=True)])
        if name == "H7":
            mod_pct = np.array([pct(x, e, s_) for x, e, s_ in zip(T["R7"][V], T["e"][V], T["st7"][V], strict=True)])
            acts = T["st7"][V] != T["st"][V]
            emit(
                f"діє (стоп змінено) на {acts.mean() * 100:.1f} % угод; d7 медіана {np.nanmedian(T['d7'][V]):.2f} ATR (база 1.00); частка стопів база {np.mean(T['B_k'][V] == 'stop') * 100:.1f} %"
            )
        else:
            mod_pct = T["P6"][V]
            conf = T["delay6"][V] >= 0
            emit(
                f"підтверджено (угода H6) {conf.mean() * 100:.1f} % заповнених; затримка медіана {np.median(T['delay6'][V][conf]) if conf.any() else math.nan:.0f} год; E[R] H6 на підтверджених {np.nanmean(T['R6'][V][conf]):+.3f} проти бази на них {np.nanmean(B[V][conf]):+.3f}, база без підтвердження {np.nanmean(B[V][~conf]):+.3f}"
            )
        emit(f"% ціни на угоду 2021+: база {np.nanmean(base_pct) * 100:+.3f} %, модуль {np.nanmean(mod_pct) * 100:+.3f} %")
        per_asset_mean(name, D[V], sym[V], wk[V], IS[V], OOS[V])
    d6, d7 = (T["R6"] - B)[V], (T["R7"] - B)[V]
    ok = np.isfinite(d6) & np.isfinite(d7)
    emit(f"\nперекриття H6 × H7: кореляція Δ {np.corrcoef(d6[ok], d7[ok])[0, 1]:+.2f} (n {int(ok.sum())})")

    # ---- H5 / H3: Spearman on V
    qsel_l = selection(Q, top, True) & Q["filled"] & (Q["kind"] == "base") & np.isin(Q["k"], list(set(T["k"][V].tolist())))
    fvg_of = {str(k): float(v) for k, v in zip(T["k"], T["fvg"], strict=True)}
    q_fvg = np.array([fvg_of.get(str(k), math.nan) for k in Q["k"]])
    meds = {}
    for name, feat, sign in (("H5", "dist", -1.0), ("H3", "fvg", 1.0)):
        x = T[feat]
        m = V & np.isfinite(x)
        med = float(np.median(x[m & IS]))
        meds[name] = med
        r = m28.spearman_ci(x[m], B[m], wk[m], n_boot=vsp.N_BOOT, seed=vsp.SEED)
        r = {**r, "m": sign * r["m"], "lo": sign * (r["hi"] if sign < 0 else r["lo"]), "hi": sign * (r["lo"] if sign < 0 else r["hi"])}
        ro = m28.spearman_ci(x[m & OOS], B[m & OOS], wk[m & OOS], n_boot=vsp.N_BOOT, seed=vsp.SEED)
        ro = {
            **ro,
            "m": sign * ro["m"],
            "lo": sign * (ro["hi"] if sign < 0 else ro["lo"]),
            "hi": sign * (ro["lo"] if sign < 0 else ro["hi"]),
        }
        qx = Q["dist"] if name == "H5" else q_fvg
        qm = qsel_l & np.isfinite(qx)
        rho_q = sign * m28.spearman(qx[qm], Q["B"][qm])
        dq = m28.spearman_diff_ci(x[m], B[m], wk[m], qx[qm], Q["B"][qm], q_wk[qm], n_boot=vsp.N_BOOT, seed=vsp.SEED)
        dq_m = sign * dq["d"]
        dq_lo, dq_hi = (sign * dq["hi"], sign * dq["lo"]) if sign < 0 else (dq["lo"], dq["hi"])
        dq_p = m28.one_sided(dq_m, dq["p"])
        fav = (x <= med) if name == "H5" else (x >= med)
        res[name] = {
            "main": r,
            "plc": rho_q if name == "H5" else dq_m,
            "plc_p": math.nan if name == "H5" else dq_p,
            "IS": sign * m28.spearman(x[m & IS], B[m & IS]),
            "OOS": ro,
            "y25": sign * m28.spearman(x[m & OOS & (T["year"] == 2025)], B[m & OOS & (T["year"] == 2025)]),
            "y26": sign * m28.spearman(x[m & OOS & (T["year"] == 2026)], B[m & OOS & (T["year"] == 2026)]),
            "drop5": drop_top5_rho(x[m], B[m], sym[m], sign),
            "P": sign * m28.spearman(x[P & np.isfinite(x)], B[P & np.isfinite(x)]),
            "x15": float(np.nanmean(T["B15"][V & OOS & fav])),
        }
        emit(f"\n## {name} ({feat}, ρ_adj = {'−' if sign < 0 else '+'}Спірмен з R; медіана IS {med:.3f})")
        emit(
            f"ρ_adj 2021+ {f(r)} p₁ {p1(r):.3f}; IS {res[name]['IS']:+.3f}; OOS {f(ro)}; P {res[name]['P']:+.3f}; 2025 {res[name]['y25']:+.3f}, 2026 {res[name]['y26']:+.3f}; без топ-5 монет {res[name]['drop5']:+.3f}"
        )
        emit(
            f"плацебо-лімітки (заповнені, n {int(qm.sum())}): ρ_adj {rho_q:+.3f}; різниця сигнал − плацебо {dq_m:+.3f} [{dq_lo:+.3f}; {dq_hi:+.3f}] p₁ {dq_p:.3f}"
        )
        for lab, mm in (("2021+", V), ("OOS", V & OOS)):
            fa, un = mm & fav & np.isfinite(x), mm & ~fav & np.isfinite(x)
            emit(
                f"  {lab}: сприятлива половина {f(mci(B[fa], wk[fa]))}; інша {f(mci(B[un], wk[un]))}; ×1.5 сприятлива {np.nanmean(T['B15'][fa]):+.3f}"
            )
        q5 = np.quantile(x[m & IS], [0.2, 0.4, 0.6, 0.8])
        qi = np.searchsorted(q5, x[m])
        emit(
            "  квінтилі (межі IS), E[R] 2021+: "
            + ", ".join(f"Q{k + 1} {np.nanmean(B[m][qi == k]):+.3f} (n {int((qi == k).sum())})" for k in range(5))
        )
        per_asset_rho(name, x[m], B[m], sym[m], sign, IS[m], OOS[m])
    age = T["age"]
    emit(
        f"описово H5: вік j − e медіана {np.median(age[V]):.0f} свічок, ρ(вік, R) 2021+ {m28.spearman(age[V], B[V]):+.3f}; затримка заповнення ρ {m28.spearman(((T['t_fill'] - T['t_in']) / HOUR_MS)[V], B[V]):+.3f}"
    )
    emit(
        "H5 × H3 (2 × 2 за медіанами IS): "
        + "; ".join(
            f"{a_}/{b_}: 2021+ {np.nanmean(B[V & ma & mb]):+.3f} (n {int((V & ma & mb).sum())}), OOS {np.nanmean(B[V & OOS & ma & mb]):+.3f} (n {int((V & OOS & ma & mb).sum())})"
            for a_, ma in (("близька", T["dist"] <= meds["H5"]), ("далека", T["dist"] > meds["H5"]))
            for b_, mb in (("FVG≥мед", T["fvg"] >= meds["H3"]), ("FVG<мед", T["fvg"] < meds["H3"]))
        )
    )

    # ---- Holm and verdicts
    pvals = [p1(res[n_]["main"]) for n_ in MODULES]
    adj = holm(pvals)
    emit("\n## Вердикт (Холм на 5; основний період V 2021+)")
    emit("| Модуль | Основна 2021+ | p₁ / Холм | Плацебо (p₁) | IS / OOS | 2025 / 2026 / без топ-5 | P | ×1.5 OOS | Позначка |")
    emit("|---|---|---|---|---|---|---|---|---|")
    passed = []
    for n_, pv, pa in zip(MODULES, pvals, adj, strict=True):
        r = res[n_]
        oos_m = r["OOS"]["m"]
        c2 = r["plc"] > 0 if n_ == "H5" else r["plc_p"] < 0.05
        ok_ = pa < 0.05 and c2 and min(r["IS"], oos_m, r["y25"], r["y26"], r["drop5"], r["P"]) > 0
        if ok_:
            passed.append(n_)
        tag = label(n_, r["main"], ok_) + ("" if not ok_ or r["x15"] > 0 else " — менший збиток (×1.5 OOS ≤ 0)")
        plc = f"{r['plc']:+.3f}" + ("" if n_ == "H5" else f" ({r['plc_p']:.3f})")
        emit(
            f"| {n_} | {f(r['main'])} | {pv:.3f} / {pa:.3f} | {plc} | {r['IS']:+.3f} / {oos_m:+.3f} | {r['y25']:+.3f} / {r['y26']:+.3f} / {r['drop5']:+.3f} | {r['P']:+.3f} | {r['x15']:+.3f} | {tag} |"
        )
    emit(f"пройшли: {passed or 'жоден'}; комбінація: {'потрібна (≥ 2)' if len(passed) >= 2 else 'немає (пройшло < 2)'}")

    # ---- D1 (diagnostic, outside Holm)
    d1(a, T, S, top, qv)


def d1(
    a: argparse.Namespace,
    T: dict[str, np.ndarray],
    S: dict[str, np.ndarray],
    top: dict[int, set[str]],
    qv: dict[str, list[tuple[int, float]]],
) -> None:
    emit("\n## Д1 (діагностика, поза Холмом): R мінус рівноваговий кошик топ-100 (β = 1)")
    t0 = time.time()
    closes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for s in vsp.syms_with_1h(a.s1_cache, a.data_dir):
        h1, _ = vsp.load_1h_qv(a.s1_cache, a.data_dir, s)
        closes[s] = (h1.t.copy(), h1.c.copy())
    emit(f"1h-закриття {len(closes)} монет за {time.time() - t0:.0f}s; rss {vsp.rss_mb():.0f} MB")
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    need = S["V"] | S["P"] | S["add"] | S["addP"]
    wk = T["week"]
    # registered: from the 1h close before the fill bar; exploratory (added after the results, labelled so): from the
    # close of the fill bar, so the basket's move inside the fill hour (adversely selected: the coin traded through
    # the level in it, while the trade's R starts at the level) is not in the hedge
    for shift, lab0 in (
        (0, "зареєстровано: від закриття перед свічкою заповнення"),
        (HOUR_MS, "розвідувально: від закриття свічки заповнення"),
    ):
        H = np.full(T["B"].size, math.nan)
        cnt = []
        for i in np.flatnonzero(need):
            tf, tx = int(T["t_fill"][i]), int(T["B_x"][i])
            prev = (tf // DAY_MS) * DAY_MS - DAY_MS
            mem = sorted(top.get(prev, set()))[:TOP_BASKET]
            ret, n = m28.basket_return(closes, mem, tf + shift, tx) if tx > tf + shift else (0.0, 0)
            cnt.append(n)
            if math.isfinite(ret):
                sg = 1.0 if T["side"][i] == "long" else -1.0
                H[i] = m28.hedged_r(float(T["B"][i]), ret, sg * float(T["e"][i]), sg * float(T["st"][i]), str(T["side"][i]))
        emit(f"### {lab0}; монет у кошику на угоду: медіана {np.median(cnt):.0f}")
        for lab, m in (("V лонги", S["V"]), ("P лонги", S["P"]), ("H9 шорти (додані, V_mod)", S["add"]), ("H9 шорти (P_mod)", S["addP"])):
            cells = []
            for pl, pm in (("IS", IS), ("OOS", OOS)):
                mm = m & pm & np.isfinite(H)
                cells.append(f"{pl}: R {f(mci(T['B'][mm], wk[mm]))}; хедж. {f(mci(H[mm], wk[mm]))}")
            emit(f"{lab} — " + " | ".join(cells))
        mm_i, mm_o = S["V"] & IS & np.isfinite(H), S["V"] & OOS & np.isfinite(H)
        d = vs_placebo(H[mm_o], wk[mm_o], H[mm_i], wk[mm_i])
        emit(f"V хеджований OOS − IS {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}]")
    days = np.arange(vsp.PASSPORT_MS, END - 7 * DAY_MS, DAY_MS)
    r7 = []
    for t in days:
        mem = sorted(top.get(int(t) - DAY_MS, set()))
        r, _ = m28.basket_return(closes, mem, int(t), int(t) + 7 * DAY_MS)
        r7.append(r)
    r7a = np.array(r7)
    emit(
        f"7-денний ret кошика (щодня 00:00 UTC, топ-100 попереднього дня): IS {np.nanmean(r7a[days < vsp.OOS_MS]) * 100:+.2f} %, OOS {np.nanmean(r7a[days >= vsp.OOS_MS]) * 100:+.2f} %"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("sanity", "run", "placebo", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s28_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--out", type=Path, default=None, help="report file (default <cache-dir>/report.txt)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    target = a.out if (a.stage == "report" and a.out) else out / f"{a.stage}.txt"
    with target.open("w") as fh:
        vsp.OUT["fh"] = fh
        t0 = time.time()
        {"sanity": stage_sanity, "run": stage_run, "placebo": stage_placebo, "report": stage_report}[a.stage](a, out)
        emit(f"[{a.stage}] {time.time() - t0:.0f}s; peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
