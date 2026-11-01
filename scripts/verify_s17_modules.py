"""S17 modules (item 8 / 7b of IDEA-20261030-demo-setups-depth): H1 depth, H5 reclaim entry, H6 limit from the 2nd bar.

Registered in the card section «Попередня реєстрація модулів S17» before the first run (Holm over 3; 1d verdict,
4h a sign row; H8 / H10 diagnostics outside Holm). The base repeats ``verify_setup_passport.limit_rows`` for S17
(same detector, limit model, placebo generator and chain) and is checked against the passport's cache to the trade.

Stages: ``sanity`` (3 symbols, timing), ``run`` (every symbol -> ``<cache>/S17/rows/<sym>.json``), ``report``.
R is in units of the trade's risk (not capital); fees ``Fees()`` and x1.5.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402
import verify_s28_modules as v28  # noqa: E402
import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s17_modules as m17  # noqa: E402
from tradedesk.analytics import s28_modules as m28  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

emit = vsp.emit
F1, F15, RULE_B = vsp.F1, vsp.F15, vsp.RULE_B
DAY_MS = vsp.DAY_MS
TFS = ("1d", "4h")
MODULES = ("H1", "H5", "H6")
INTEREST = {"H1": 0.05, "H5": 0.05, "H6": 0.05}
MDE_K = 2.97  # z(1 - 0.05/3) + z(0.80): the strictest Holm step over 3
NB_DESC = 2_000
KNOWN = {"n21": 1448, "m21": 0.025, "n_oos": 284, "place_oos": 320, "m_oos": 0.027, "late_oos": 0.123}
Y2023 = s1.ms("2023-01-01T00:00")
Y2025 = s1.ms("2025-01-01T00:00")
Y2026 = s1.ms("2026-01-01T00:00")


# ---------------------------------------------------------------- per symbol


def h5_fields(path: Any, tk: int, e: float, st: float, side: str, sg: float, hold: int, ctx: Any) -> dict[str, Any]:
    """H5 on a filled limit (long frame): reclaim entry at the close of the first 1h bar back at the level."""
    rc = m17.reclaim_entry(path, int(np.searchsorted(path.t, tk)), e, st)
    if rc is None:
        return {"rc": False}
    mi, e2 = rc
    t2 = int(path.t[mi]) + HOUR_MS
    tg2 = e2 + 2.0 * (e2 - st)
    o = simulate_stop_rule(path, t2, e2, st, tg2, hold, RULE_B, fees=F1)
    o15 = simulate_stop_rule(path, t2, e2, st, tg2, hold, RULE_B, fees=F15)
    x2 = ctx(t2, side, sg * e2, sg * st, sg * tg2)
    return {
        "rc": True,
        "t2": t2,
        "R5": o.r_net - m17.taker_entry_cost(e2, st, F1) if o else None,
        "R515": o15.r_net - m17.taker_entry_cost(e2, st, F15) if o15 else None,
        "rr2": x2["rr"] if x2 else None,
        "sp2": x2["stop_pct"] if x2 else None,
        "sc2": x2["score"] if x2 else None,
        "turn2": x2["turn"] if x2 else None,
    }


def run_symbol(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, pp_dir = args
    t_start = time.time()
    s1c, dd, out, pp = Path(s1_cache), Path(data_dir), Path(out_dir), Path(pp_dir)
    member = s1.Membership(s1.members(s1c))
    btc = json.loads((pp / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()

    def ctx(t_in: int, side: str, e: float, st: float, tg: float) -> dict[str, Any] | None:
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            return None
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at(side, e, st, tg)
        sc = sp.score_points(side, rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC)
        return {"turn": turn, "rr": rr, "stop_pct": abs(e - st) / abs(e), "score": sc}

    rows: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    for tf in TFS:
        tb = resample(h1, vsp.HOURS[tf])
        sigs = ss.s17_all(tb, tf)
        rng = np.random.default_rng(vsp.SEED + zlib.crc32(f"{s}S17".encode()))  # the passport's generator
        pos = segment_pos(tb.t)
        a_arr = atr(tb, 14, pos)
        close_t = tb.t + tb.bar_ms
        pool = np.flatnonzero((pos >= ss.S17_WARMUP) & (close_t >= vsp.START_MS) & (close_t < vsp.END[tf]))
        hold = ss.S17_HOLD_BARS * tb.bar_ms
        bar = tb.bar_ms
        for sig in sigs:
            why[f"{tf}:signal"] += 1
            i = int(np.searchsorted(tb.t, sig.signal_ts))
            t0 = sig.signal_ts + bar
            if not (vsp.START_MS <= t0 < vsp.END[tf]):
                continue
            t1 = int(sig.indicators["rest_until_ms"])
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            bb = tb if sig.side == "long" else tb.mirrored()
            path = h1 if sig.side == "long" else h1.mirrored()
            if not (st < e < tg):
                why[f"{tf}:levels"] += 1
                continue
            c_sig = float(bb.c[i])
            key = f"{s}|{tf}|{sig.side}|{t0}"
            for _ in range(vsp.N_PLACEBO):  # same draws as the passport (drawn before any filter)
                if pool.size == 0:
                    break
                r = int(pool[rng.integers(pool.size)])
                c_r = float(bb.c[r])
                e_r, st_r = c_r * e / c_sig, c_r * st / c_sig
                tg_r = e_r + 2.0 * (e_r - st_r)
                tr0 = int(tb.t[r]) + bar
                x = ctx(tr0, sig.side, sg * e_r, sg * st_r, sg * tg_r)
                if (
                    x is None
                    or x["turn"] < 5e6
                    or x["rr"] < vsp.MIN_RR
                    or x["stop_pct"] > vsp.MAX_STOP
                    or x["score"] < vsp.MIN_SCORE
                    or not (st_r < e_r < tg_r)
                ):
                    continue
                o, tk = sp.limit_trade(path, tr0, tr0 + (t1 - t0), e_r, st_r, tg_r, hold, RULE_B, F1)
                if o is None and tk >= 0:
                    continue
                late = r + 1 < len(bb) and int(bb.t[r + 1]) - int(bb.t[r]) == bar and float(bb.c[r + 1]) > e_r
                lo = sp.limit_trade(path, tr0 + bar, tr0 + bar + (t1 - t0), e_r, st_r, tg_r, hold, RULE_B, F1)[0] if late else None
                row = {
                    "sym": s,
                    "tf": tf,
                    "side": sig.side,
                    "t_in": tr0,
                    **x,
                    "filled": o is not None,
                    "B": o.r_net if o else None,
                    "B_x": o.exit_ms if o else tr0 + (t1 - t0),
                    "L": lo.r_net if lo else None,
                    "pkey": key,
                    "pt0": t0,
                }
                if o is not None:
                    row.update(h5_fields(path, tk, e_r, st_r, sig.side, sg, hold, ctx))
                plc.append(row)
            x = ctx(t0, sig.side, sig.entry, sig.stop, sig.target)
            if x is None:
                why[f"{tf}:under_1m_or_unknown"] += 1
                continue
            if not c_sig > e:
                why[f"{tf}:at_level"] += 1
                continue
            b, tk = sp.limit_trade(path, t0, t1, e, st, tg, hold, RULE_B, F1)
            if b is None and tk >= 0:
                why[f"{tf}:no_path"] += 1
                continue
            filled = b is not None
            b15 = sp.limit_trade(path, t0, t1, e, st, tg, hold, RULE_B, F15)[0] if filled else None
            late = i + 1 < len(bb) and int(bb.t[i + 1]) - int(bb.t[i]) == bar and float(bb.c[i + 1]) > e
            lb = lb15 = None
            if late:
                lb = sp.limit_trade(path, t0 + bar, t1 + bar, e, st, tg, hold, RULE_B, F1)[0]
                lb15 = sp.limit_trade(path, t0 + bar, t1 + bar, e, st, tg, hold, RULE_B, F15)[0]
            p = int(np.searchsorted(tb.t, int(sig.indicators["pob_ts"])))
            q = int(np.searchsorted(tb.t, int(sig.indicators["impulse_ts"])))
            depth, lag = m17.trough_depth(bb, a_arr, p, q, i)
            a_p = float(a_arr[p])
            row = {
                "sym": s,
                "tf": tf,
                "side": sig.side,
                "t_in": t0,
                **x,
                "key": key,
                "filled": filled,
                "t_fill": tk,
                "B": b.r_net if b else None,
                "B15": b15.r_net if b15 else None,
                "B_x": b.exit_ms if b else t1,
                "late_placed": bool(late),
                "L": lb.r_net if lb else None,
                "L15": lb15.r_net if lb15 else None,
                "depth": depth,
                "depth_q": (float(bb.c[p]) - float(bb.low[q])) / a_p if a_p > 0 else None,
                "speed": depth / lag if lag > 0 else None,
                "t_reach": m17.first_reach(path, t0, tk if filled else t1, tg),
                "t1": t1,
            }
            if filled:
                row.update(h5_fields(path, tk, e, st, sig.side, sg, hold, ctx))
            rows.append(row)
            why[f"{tf}:cand"] += 1
            why[f"{tf}:filled"] += int(filled)
    (out / "rows").mkdir(parents=True, exist_ok=True)
    clean = [{k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in r.items()} for r in rows]
    (out / "rows" / f"{s}.json").write_text(json.dumps({"rows": clean, "plc": plc, "why": dict(why)}))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t_start, "rss": vsp.rss_mb()}


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((s, str(a.s1_cache), str(a.data_dir), str(out / "sanity"), str(a.passport_cache / "S17")))
        emit(f"timing {s}: {r['sec']:.1f}s; {r['why']}; rss {r['rss']:.0f} MB")
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.1f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    pp = str(a.passport_cache / "S17")
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(out), pp) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- tables

STR = ("sym", "tf", "side", "key", "pkey")
INT = ("t_in", "B_x", "t_fill", "t2", "t_reach", "pt0", "t1", "score", "sc2")
BOOL = ("filled", "late_placed", "rc")


def table(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    keys = sorted({k for r in rows for k in r})
    T: dict[str, np.ndarray] = {}
    for k in keys:
        col = [r.get(k) for r in rows]
        if k in STR:
            T[k] = np.array(["" if v is None else str(v) for v in col])
        elif k in INT:
            T[k] = np.array([-1 if v is None else int(v) for v in col], dtype=np.int64)
        elif k in BOOL:
            T[k] = np.array([bool(v) for v in col])
        else:
            T[k] = np.array([math.nan if v is None else float(v) for v in col])
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = (T["side"] == "short").astype(np.int64)
    return T


def sub(T: dict[str, np.ndarray], m: np.ndarray) -> dict[str, np.ndarray]:
    return {k: v[m] for k, v in T.items()}


def load(out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    plc: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for p in sorted((out / "rows").glob("*.json")):
        d = json.loads(p.read_text())
        rows.extend(d["rows"])
        plc.extend(d["plc"])
        why.update(d["why"])
    return rows, plc, why


# ---------------------------------------------------------------- report helpers


def mci(v: np.ndarray, wk: np.ndarray, nb: int = vsp.N_BOOT) -> dict[str, float]:
    ok = np.isfinite(v)
    return sp.mean_ci(v[ok], wk[ok], n_boot=nb, seed=vsp.SEED)


def f(r: dict[str, float], key: str = "m") -> str:
    lo, hi = r.get("lo", math.nan), r.get("hi", math.nan)
    n = int(r.get("n", r.get("n1", 0)))
    if not math.isfinite(r.get(key, math.nan)):
        return f"—, n {n}"
    ci = f" [{lo:+.3f}; {hi:+.3f}]" if math.isfinite(lo) else ""
    return f"{r[key]:+.3f}{ci}, n {n}"


def p1(r: dict[str, float], key: str = "m") -> float:
    return m28.one_sided(r[key], r["p"])


def pair_vs(v: np.ndarray, wk: np.ndarray, q: np.ndarray, q_wk: np.ndarray, nb: int = vsp.N_BOOT) -> dict[str, float]:
    ina = np.concatenate([np.ones(v.size, bool), np.zeros(q.size, bool)])
    return sp.diff_ci(np.concatenate([v, q]), ina, ~ina, np.concatenate([wk, q_wk]), n_boot=nb, seed=vsp.SEED)


def label(name: str, r: dict[str, float], key: str, passed: bool) -> str:
    if passed:
        return "ПРОЙШОВ"
    mde = MDE_K * r["se"]
    if r["hi"] < INTEREST[name]:
        return f"✗ (верхня межа {r['hi']:+.3f} < {INTEREST[name]:.2f}: корисний ефект виключено; MDE₈₀ {mde:.3f})"
    if mde > INTEREST[name]:
        return f"◯ невимірно (MDE₈₀ {mde:.3f} > {INTEREST[name]:.2f})"
    return f"✗ (MDE₈₀ {mde:.3f})"


def rho(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    return m28.spearman(x[ok], y[ok]) if ok.sum() >= 3 else math.nan


def samples(T: dict[str, np.ndarray], Q: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> dict[str, np.ndarray]:
    """Masks of the registered samples: VP / PP placements (chains), V / P filled, the placebo of VP's signals."""
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    VP = vsp.chain(T, period & demo & turn5 & vsp.in_prev_day_top(T, top_days))
    PP = vsp.chain(T, period & demo & turn5)
    # The parent of a placebo row is the VP signal with the same (key, stop_pct): key alone is shared by siblings
    # (several POBs broken by one bar). Fix of critic B's finding 1 (2026-11-01); Q["par"] is the parent's row in T.
    uid = [m17.parent_uid(k, x) for k, x in zip(T["key"], T["stop_pct"], strict=True)]
    Q["par"] = m17.link_parents(uid, VP.tolist(), [m17.parent_uid(k, x) for k, x in zip(Q["pkey"], Q["stop_pct"], strict=True)])
    q_par = Q["par"] >= 0
    QP = vsp.chain(Q, (Q["t_in"] >= vsp.PASSPORT_MS) & vsp.in_prev_day_top(Q, top_days) & q_par)
    return {"VP": VP, "PP": PP, "V": VP & T["filled"], "P": PP & T["filled"], "QP": QP, "Q": QP & Q["filled"]}


H5_CHECKS = ("стоп > 10 %", "бал < 2", "не в топ-100", "оборот < $5M", "R:R < 1.5")


def h5_checks(T: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> dict[str, np.ndarray]:
    """Failure masks of the demo list and selection at t2 (data up to t2 only), named as in ``H5_CHECKS``."""
    t2 = np.where(T["t2"] > 0, T["t2"], T["t_in"])
    top2 = vsp.in_prev_day_top({"t_in": t2, "sym": T["sym"]}, top_days)
    fails = (
        np.nan_to_num(T["sp2"], nan=9) > vsp.MAX_STOP,
        T["sc2"] < vsp.MIN_SCORE,
        ~top2,
        np.nan_to_num(T["turn2"], nan=-1) < 5e6,
        np.nan_to_num(T["rr2"], nan=-1) < vsp.MIN_RR,
    )
    return dict(zip(H5_CHECKS, fails, strict=True))


def h5_ok(T: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> np.ndarray:
    """The reclaim trade exists and passes the demo list and selection at t2 (data up to t2 only)."""
    bad = np.zeros(T["t_in"].size, dtype=bool)
    for m in h5_checks(T, top_days).values():
        bad |= m
    return T["rc"] & np.isfinite(T["R5"]) & ~bad


# ---------------------------------------------------------------- report


def stage_report(a: argparse.Namespace, out: Path) -> None:
    rows, plc, why = load(out)
    emit(f"rows {len(rows)}, placebo {len(plc)}; {dict(sorted(why.items()))}")
    elig = s1.eligible_syms(a.data_dir)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top_days = vsp.top_n_days(qv, vsp.TOP_N)
    TT, QQ = table(rows), table(plc)
    res: dict[str, Any] = {}
    for tf in TFS:
        T, Q = sub(TT, TT["tf"] == tf), sub(QQ, QQ["tf"] == tf)
        T["ok5"] = h5_ok(T, top_days)
        Q["ok5"] = h5_ok(Q, top_days)
        res[tf] = (T, Q, samples(T, Q, top_days))
    T, Q, S = res["1d"]
    check_passport(a, T)
    link_report(T, Q, S)
    base(T, S)
    out_h1 = h1(T, Q, S)
    out_h5 = h5(T, Q, S, top_days)
    chain_bounds(T, S, top_days)
    out_h6 = h6(T, Q, S)
    pr = [out_h1["p1"], out_h5["p1"], out_h6["p1"]]
    adj = holm(pr)
    emit("\n## Холм на 3 і вердикти (1d)")
    emit("| модуль | основна метрика | p₁ | Холм | плацебо p₁ | умови 3–5 | позначка |\n|---|---|---|---|---|---|---|")
    for name, o, pa in zip(MODULES, (out_h1, out_h5, out_h6), adj, strict=True):
        passed = pa < 0.05 and o["pq"] < 0.05 and o["cond"]
        emit(
            f"| {name} | {o['txt']} | {o['p1']:.3f} | {pa:.3f} | {o['pq']:.3f} | {'так' if o['cond'] else 'ні'} | {label(name, o['r'], o['key'], passed)} |"
        )
    sign_rows(res["4h"])
    diagnostics(T, S)
    overlap(T, S)


def _rnd(x: Any) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else round(float(x), 9)


def check_passport(a: argparse.Namespace, T: dict[str, np.ndarray]) -> None:
    """Base reproduced to the trade: multisets of the full 1d candidate rows (sym, side, t_in, stop_pct, B, L, filled)
    of the passport and ours (siblings share (sym, side, t_in), so a dict on that key would compare one row per key)."""
    ref: Counter[tuple[Any, ...]] = Counter()
    for p in sorted((a.passport_cache / "S17" / "cands").glob("*.json")):
        for r in json.loads(p.read_text())["rows"]:
            if r["tf"] == "1d":
                ref[(r["sym"], r["side"], int(r["t_in"]), _rnd(r["stop_pct"]), _rnd(r["B"]), _rnd(r["L"]), bool(r["filled"]))] += 1
    mine: Counter[tuple[Any, ...]] = Counter(
        (str(s), str(sd), int(t), _rnd(x), _rnd(b), _rnd(lv), bool(fl))
        for s, sd, t, x, b, lv, fl in zip(T["sym"], T["side"], T["t_in"], T["stop_pct"], T["B"], T["L"], T["filled"], strict=True)
    )
    diff = sum(((ref - mine) + (mine - ref)).values())
    emit(
        f"\n## Відтворення паспорта S17 1d до угоди (мультимножини повних рядків)\n"
        f"рядків-кандидатів: паспорт {sum(ref.values())}, тут {sum(mine.values())}; рядків без пари {diff}; "
        f"унікальних ключів (sym, side, t_in) {len({k[:3] for k in mine})}"
    )


def link_report(T: dict[str, np.ndarray], Q: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> None:
    """How the placebo finds its parent: key alone vs (key, stop_pct) (critic B's finding 1)."""
    cnt = Counter(T["key"].tolist())
    dup = sum(1 for c in cnt.values() if c > 1)
    uid = [m17.parent_uid(k, x) for k, x in zip(T["key"], T["stop_pct"], strict=True)]
    ucnt = Counter(uid)
    q_uid = [m17.parent_uid(k, x) for k, x in zip(Q["pkey"], Q["stop_pct"], strict=True)]
    known = np.isin(Q["pkey"], list(cnt))
    hit = np.array([u in ucnt for u in q_uid])
    old = np.isin(Q["pkey"], list(set(T["key"][S["VP"]].tolist())))
    emit(
        f"\n## Батько плацебо (1d)\nключів sym|tf|side|t0 з кількома сигналами {dup}, у них {sum(c for c in cnt.values() if c > 1)} з {len(uid)} рядків; "
        f"неоднозначних (ключ, стоп%) {sum(1 for c in ucnt.values() if c > 1)}; плацебо-рядків, чий ключ є серед сигналів, {int(known.sum())}, "
        f"з них знайшли батька за (ключ, стоп%) {int((known & hit).sum())}\n"
        f"плацебо з ключем із VP (старе правило) {int(old.sum())}, зі справжнім батьком у VP {int((Q['par'] >= 0).sum())}; "
        f"заповнених у ланцюгу плацебо {int(S['Q'].sum())}"
    )


def chain_bounds(T: dict[str, np.ndarray], S: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> None:
    """Bounds of not recomputing the chain for H5 (the H5 trade's exit is not stored): placements of the same symbol
    an H5 trade could still block (from the base exit to t2 + hold) and candidates the base trade blocked although H5
    did not enter (from the fill to the base exit). Both are upper bounds on the count; R is the H5 strategy's R."""
    V, VP = S["V"], S["VP"]
    n = int(V.sum())
    period = T["t_in"] >= vsp.PASSPORT_MS
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    cand = period & demo & (np.nan_to_num(T["turn"], nan=-1) >= 5e6) & vsp.in_prev_day_top(T, top_days) & ~VP
    rmod = np.where(T["filled"] & T["ok5"], np.nan_to_num(T["R5"]), 0.0)
    hold = ss.S17_HOLD_BARS * DAY_MS
    blk: set[int] = set()
    fre: set[int] = set()
    for i in np.flatnonzero(V):
        same = T["sym"] == T["sym"][i]
        if T["ok5"][i]:
            w = same & VP & (T["t_in"] >= T["B_x"][i]) & (T["t_in"] < T["t2"][i] + hold)
            blk.update(np.flatnonzero(w).tolist())
        else:
            w = same & cand & (T["t_in"] >= T["t_fill"][i]) & (T["t_in"] < T["B_x"][i])
            fre.update(np.flatnonzero(w).tolist())
    b, fr = sorted(blk), sorted(fre)
    emit(
        f"\n## Ланцюг H5 не перераховано: межі (1d, 2021+, на n {n})\n"
        f"розміщень VP, які угода H5 могла б заблокувати, ≤ {len(b)} з {int(VP.sum())}: зсув Δ5, якби всі заблоковано, {-rmod[b].sum() / n:+.4f}R "
        f"(за E[R] бази {-len(b) * np.mean(T['B'][V]) / n:+.4f}); кандидатів, яких звільнила б відсутність входу H5, ≤ {len(fr)}: "
        f"зсув, якби всі увійшли, {rmod[fr].sum() / n:+.4f}R (за E[R] бази {len(fr) * np.mean(T['B'][V]) / n:+.4f})"
    )


def base(T: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> None:
    VP, V, P = S["VP"], S["V"], S["P"]
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    wk = T["week"]
    emit("\n## База S17 1d (R на ризик угоди; заповнені, якщо не сказано інше)")
    r21, ro = mci(T["B"][V], wk[V]), mci(T["B"][V & OOS], wk[V & OOS])
    lr = np.nan_to_num(T["L"], nan=0.0)
    late = mci(lr[VP & OOS], wk[VP & OOS])
    emit(f"V 2021+ {f(r21)}; IS {f(mci(T['B'][V & IS], wk[V & IS]))}; OOS {f(ro)} з {int((VP & OOS).sum())} розміщень")
    emit(f"V ×1.5 2021+ {f(mci(T['B15'][V], wk[V]))}; OOS {f(mci(T['B15'][V & OOS], wk[V & OOS]))}; P 2021+ {f(mci(T['B'][P], wk[P]))}")
    emit(f"пізніше на свічку, на розміщення, OOS {f(late)}")
    k = KNOWN
    ok = (
        int(r21["n"]) == k["n21"]
        and abs(r21["m"] - k["m21"]) < 0.0015
        and int(ro["n"]) == k["n_oos"]
        and int((VP & OOS).sum()) == k["place_oos"]
        and abs(ro["m"] - k["m_oos"]) < 0.0015
        and abs(late["m"] - k["late_oos"]) < 0.0015
    )
    emit(f"відоме число паспорта (n 1448 / +0.025; OOS 284 з 320 / +0.027; пізніше OOS +0.123): {'ЗБІГ' if ok else 'РОЗБІЖНІСТЬ'}")
    days = (np.minimum(T["B_x"], T["t1"]) - T["t_in"])[VP] / DAY_MS
    yrs = (vsp.END["1d"] - vsp.PASSPORT_MS) / (365.25 * DAY_MS)
    emit(
        f"слот-доби ліміту на розміщення (від розміщення до виходу / дедлайну): медіана {np.median(days):.1f}, середнє {days.mean():.1f}, сума {days.sum():.0f} ({days.sum() / yrs:.0f} на рік)"
    )


def h1(T: dict[str, np.ndarray], Q: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> dict[str, Any]:
    V, P = S["V"] & np.isfinite(T["depth"]), S["P"] & np.isfinite(T["depth"])
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    x, y, wk, sym = T["depth"], T["B"], T["week"], T["sym"]
    emit("\n## H1: depth = (close[p] − мін. лоу q … k−1) / ATR14[p], ρ = Спірмен(depth, R), напрям +")
    r = m28.spearman_ci(x[V], y[V], wk[V], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    Qm = S["Q"]
    par = Q["par"][Qm]
    qx, qw, qy = x[par], wk[par], Q["B"][Qm]
    ok = np.isfinite(qx)
    d = m28.spearman_diff_ci(x[V], y[V], wk[V], qx[ok], qy[ok], qw[ok], n_boot=vsp.N_BOOT, seed=vsp.SEED)
    emit(
        f"V 2021+: ρ {f(r)}, p₁ {p1(r):.4f}, MDE₈₀ {MDE_K * r['se']:.3f}; плацебо ρ_pl {m28.spearman(qx[ok], qy[ok]):+.3f} (n {int(ok.sum())}); ρ − ρ_pl {f(d, 'd')} p₁ {p1(d, 'd'):.4f}"
    )
    do = m28.spearman_diff_ci(x[V], y[V], wk[V], qx[ok], qy[ok], Q["week"][Qm][ok], n_boot=NB_DESC, seed=vsp.SEED)
    emit(f"чутливість: кластери плацебо за власним тижнем: ρ − ρ_pl {f(do, 'd')}")
    cells = {
        "IS": rho(x[V & IS], y[V & IS]),
        "OOS": rho(x[V & OOS], y[V & OOS]),
        "2025": rho(x[V & (T["year"] == 2025)], y[V & (T["year"] == 2025)]),
        "2026": rho(x[V & (T["year"] == 2026)], y[V & (T["year"] == 2026)]),
        "без топ-5 монет": v28.drop_top5_rho(x[V], y[V], sym[V], 1.0),
        "P 2021+": rho(x[P], y[P]),
    }
    emit("точково: " + "; ".join(f"{k} {v:+.3f}" for k, v in cells.items()))
    med = float(np.median(x[V & IS]))
    hi_oos = V & OOS & (x >= med)
    e15 = mci(T["B15"][hi_oos], wk[hi_oos])
    emit(f"медіана depth на V IS {med:.2f}; depth ≥ медіани, OOS ×1.5: {f(e15)}")
    for lab, mm in (("≥ медіани", V & (x >= med)), ("< медіани", V & (x < med))):
        pct = (y * T["stop_pct"])[mm]
        emit(
            f"  {lab}, 2021+: E[R] {f(mci(y[mm], wk[mm], NB_DESC))}; % ціни ≈ R × стоп% {np.mean(pct) * 100:+.2f} %; стоп% медіана {np.median(T['stop_pct'][mm]) * 100:.1f}"
        )
    emit(
        f"описово: ρ(depth, stop_pct) {rho(x[V], T['stop_pct'][V]):+.3f}; ρ(depth_q, R) {rho(T['depth_q'][V], y[V]):+.3f}; ρ(speed, R) {rho(T['speed'][V], y[V]):+.3f}; ×1.5 ρ {rho(x[V], T['B15'][V]):+.3f}"
    )
    qs = np.quantile(T["stop_pct"][V], [1 / 3, 2 / 3])
    terc = np.digitize(T["stop_pct"], qs)
    emit(
        "ρ усередині терцилів стопу: "
        + "; ".join(f"T{t + 1} {rho(x[V & (terc == t)], y[V & (terc == t)]):+.3f} (n {int((V & (terc == t)).sum())})" for t in range(3))
    )
    emit(
        f"лонги {rho(x[V & (T['side'] == 'long')], y[V & (T['side'] == 'long')]):+.3f}; шорти {rho(x[V & (T['side'] == 'short')], y[V & (T['side'] == 'short')]):+.3f}"
    )
    v28.per_asset_rho("H1", x[V], y[V], sym[V], 1.0, IS[V], OOS[V])
    cond = all(v > 0 for v in cells.values()) and e15["m"] > 0
    return {"p1": p1(r), "pq": p1(d, "d"), "cond": cond, "r": r, "key": "m", "txt": f"ρ {f(r)}"}


def h5(T: dict[str, np.ndarray], Q: dict[str, np.ndarray], S: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> dict[str, Any]:
    V, P = S["V"], S["P"]
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    wk, sym = T["week"], T["sym"]
    ok = T["ok5"]
    d = np.where(ok, T["R5"], 0.0) - T["B"]
    d15 = np.where(ok, T["R515"], 0.0) - T["B15"]
    emit("\n## H5: reclaim замість ліміту, Δ5 = R_H5 − R_база на всіх заповнених V (без reclaim = 0R), напрям > 0")
    r = mci(d[V], wk[V])
    Qm = S["Q"]
    dq = np.where(Q["ok5"], Q["R5"], 0.0) - Q["B"]
    qw = wk[Q["par"][Qm]]
    c = pair_vs(d[V], wk[V], dq[Qm], qw)
    emit(
        f"V 2021+: Δ5 {f(r)}, p₁ {p1(r):.4f}, MDE₈₀ {MDE_K * r['se']:.3f}; плацебо Δ5_pl {f(mci(dq[Qm], qw, NB_DESC))} (n {int(Qm.sum())}); Δ5 − Δ5_pl {f(c, 'd')} p₁ {p1(c, 'd'):.4f}"
    )
    co = pair_vs(d[V], wk[V], dq[Qm], Q["week"][Qm], NB_DESC)
    emit(f"чутливість: кластери плацебо за власним тижнем: Δ5 − Δ5_pl {f(co, 'd')}")
    cells = {
        "IS": float(np.mean(d[V & IS])),
        "OOS": float(np.mean(d[V & OOS])),
        "2025": float(np.mean(d[V & (T["year"] == 2025)])),
        "2026": float(np.mean(d[V & (T["year"] == 2026)])),
        "без топ-5 монет": v28.drop_top5_mean(d[V], sym[V]),
        "P 2021+": float(np.mean(d[P])),
    }
    emit("точково: " + "; ".join(f"{k} {v:+.3f}" for k, v in cells.items()))
    emit(f"×1.5: Δ5 2021+ {f(mci(d15[V], wk[V], NB_DESC))}; OOS {f(mci(d[V & OOS], wk[V & OOS], NB_DESC))}")
    n = int(V.sum())
    emit(
        f"частки на V 2021+ (n {n}): reclaim є {T['rc'][V].mean():.1%}; reclaim пройшов відбір / список на t₂ {ok[V].mean():.1%}; "
        f"без входу H5 {(~ok[V]).mean():.1%}"
    )
    emit(
        f"E[R] бази: на угодах з входом H5 {f(mci(T['B'][V & ok], wk[V & ok], NB_DESC))}; без входу {f(mci(T['B'][V & ~ok], wk[V & ~ok], NB_DESC))}; "
        f"E[R_H5] самих reclaim-угод {f(mci(T['R5'][V & ok], wk[V & ok], NB_DESC))}"
    )
    # Decomposition of "no H5 entry" (critic B's finding 2): no reclaim vs a reclaim that fails the checks at t2.
    has = T["rc"] & np.isfinite(T["R5"])
    emit("розклад Δ5 за групами (V 2021+): | група | n | E[R] бази | E[R_H5] | внесок у Δ5 |")
    for lab, g in (
        ("reclaim немає (стоп раніше або > 24 год)", V & ~has),
        ("reclaim є, відбір / список на t₂ не пройдено", V & has & ~ok),
        ("вхід H5 є", V & ok),
    ):
        r5 = f"{np.mean(T['R5'][g]):+.3f}" if g.any() else "—"
        emit(f"| {lab} | {int(g.sum())} | {f(mci(T['B'][g], wk[g], NB_DESC))} | {r5} | {d[g].sum() / n:+.3f} |")
    fail = V & has & ~ok
    checks = h5_checks(T, top_days)
    tot = np.zeros(T["t_in"].size, dtype=np.int64)
    for m in checks.values():
        tot += m.astype(np.int64)
    emit(
        f"причини відсіву на t₂ ({int(fail.sum())} угод; одна угода може мати кілька): "
        + "; ".join(f"{k} {int((fail & m).sum())} (лише ця {int((fail & m & (tot == 1)).sum())})" for k, m in checks.items())
    )
    dn = np.where(has, T["R5"], 0.0) - T["B"]
    emit(
        f"розвідувально (після результатів): Δ5 без відбору / списку на t₂ {f(mci(dn[V], wk[V], NB_DESC))}; "
        f"R_H5 на відкинутих на t₂ {np.mean(T['R5'][fail]) if fail.any() else math.nan:+.3f}"
    )
    e15 = mci(T["R515"][V & ok & OOS], wk[V & ok & OOS])
    emit(
        f"reclaim-угоди OOS ×1.5: {f(e15)}; стратегія з модулем на заповнених базою, OOS ×1.5 (без входу = 0): {f(mci(np.where(ok, T['R515'], 0.0)[V & OOS], wk[V & OOS], NB_DESC))}"
    )
    emit(f"лонги {np.mean(d[V & (T['side'] == 'long')]):+.3f}; шорти {np.mean(d[V & (T['side'] == 'short')]):+.3f}")
    v28.per_asset_mean("H5", d[V], sym[V], wk[V], IS[V], OOS[V])
    cond = all(v > 0 for v in cells.values()) and e15["m"] > 0
    return {"p1": p1(r), "pq": p1(c, "d"), "cond": cond, "r": r, "key": "m", "txt": f"Δ5 {f(r)}"}


def h6(T: dict[str, np.ndarray], Q: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> dict[str, Any]:
    VP, PP = S["VP"], S["PP"]
    IS, OOS = T["t_in"] < vsp.OOS_MS, T["t_in"] >= vsp.OOS_MS
    wk, sym = T["week"], T["sym"]
    d = np.nan_to_num(T["L"], nan=0.0) - np.nan_to_num(T["B"], nan=0.0)
    d15 = np.nan_to_num(T["L15"], nan=0.0) - np.nan_to_num(T["B15"], nan=0.0)
    emit(
        "\n## H6: ліміт з другої свічки, Δ6 = R_пізно − R_база на всіх розміщеннях V (0R, якщо не поставлено / не заповнено); вердикт — IS"
    )
    A = VP & IS
    r = mci(d[A], wk[A])
    Qm = S["QP"] & (Q["t_in"] < vsp.OOS_MS)
    dq = np.nan_to_num(Q["L"], nan=0.0) - np.nan_to_num(Q["B"], nan=0.0)
    qw = wk[Q["par"][Qm]]
    c = pair_vs(d[A], wk[A], dq[Qm], qw)
    emit(
        f"V IS: Δ6 {f(r)}, p₁ {p1(r):.4f}, MDE₈₀ {MDE_K * r['se']:.3f}; плацебо Δ6_pl IS {f(mci(dq[Qm], qw, NB_DESC))} (n {int(Qm.sum())}); Δ6 − Δ6_pl {f(c, 'd')} p₁ {p1(c, 'd'):.4f}"
    )
    co = pair_vs(d[A], wk[A], dq[Qm], Q["week"][Qm], NB_DESC)
    emit(f"чутливість: кластери плацебо за власним тижнем: Δ6 − Δ6_pl {f(co, 'd')}")
    h1_ = VP & (T["t_in"] < Y2023)
    h2_ = A & (T["t_in"] >= Y2023)
    cells = {
        "2021–2022": float(np.mean(d[h1_])),
        "2023 … 2025-02": float(np.mean(d[h2_])),
        "без топ-5 монет (IS)": v28.drop_top5_mean(d[A], sym[A]),
        "P IS": float(np.mean(d[PP & IS])),
    }
    emit("точково: " + "; ".join(f"{k} {v:+.3f}" for k, v in cells.items()))
    emit(f"OOS (повтор знака, не вердикт): Δ6 {f(mci(d[VP & OOS], wk[VP & OOS], NB_DESC))}; 2021+ {f(mci(d[VP], wk[VP], NB_DESC))}")
    emit(f"×1.5 IS: Δ6 {f(mci(d15[A], wk[A], NB_DESC))}")
    lf = A & np.isfinite(T["L15"])
    e15 = mci(T["L15"][lf], wk[lf])
    emit(
        f"пізні заповнені угоди IS ×1.5: {f(e15)}; на розміщення IS ×1.5 {f(mci(np.nan_to_num(T['L15'], nan=0.0)[A], wk[A], NB_DESC))}; "
        f"поставлено {T['late_placed'][A].mean():.1%}, заповнено {np.isfinite(T['L'][A]).mean():.1%} (база {T['filled'][A].mean():.1%})"
    )
    lag = np.where(T["filled"], (T["t_fill"] - T["t_in"]) / DAY_MS, math.nan)
    groups = (
        ("заповнена базою в першу добу", T["filled"] & (lag < 1)),
        ("2–5 доба", T["filled"] & (lag >= 1) & (lag < 5)),
        ("пізніше", T["filled"] & (lag >= 5)),
        ("база не заповнена", ~T["filled"]),
    )
    emit(
        "за часом заповнення бази (IS, описово): "
        + "; ".join(f"{lab} n {int((A & g).sum())}, Δ6 {np.mean(d[A & g]) if (A & g).any() else math.nan:+.3f}" for lab, g in groups)
    )
    emit(f"лонги {np.mean(d[A & (T['side'] == 'long')]):+.3f}; шорти {np.mean(d[A & (T['side'] == 'short')]):+.3f}")
    v28.per_asset_mean("H6 (на розміщеннях 2021+)", d[VP], sym[VP], wk[VP], IS[VP], OOS[VP])
    cond = all(v > 0 for v in cells.values()) and e15["m"] > 0
    return {"p1": p1(r), "pq": p1(c, "d"), "cond": cond, "r": r, "key": "m", "txt": f"Δ6 IS {f(r)}"}


def sign_rows(x: tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]) -> None:
    T, _, S = x
    V, VP = S["V"], S["VP"]
    IS = T["t_in"] < vsp.OOS_MS
    wk = T["week"]
    emit("\n## 4h — рядок знака (не вердикт)")
    m = V & np.isfinite(T["depth"])
    r = m28.spearman_ci(T["depth"][m], T["B"][m], wk[m], n_boot=NB_DESC, seed=vsp.SEED)
    d5 = np.where(T["ok5"], T["R5"], 0.0) - T["B"]
    d6 = np.nan_to_num(T["L"], nan=0.0) - np.nan_to_num(T["B"], nan=0.0)
    emit(f"база V 2021+ {f(mci(T['B'][V], wk[V], NB_DESC))}")
    emit(f"H1 ρ 2021+ {f(r)}; H5 Δ5 2021+ {f(mci(d5[V], wk[V], NB_DESC))}; H6 Δ6 IS {f(mci(d6[VP & IS], wk[VP & IS], NB_DESC))}")


def diagnostics(T: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> None:
    VP, V = S["VP"], S["V"]
    wk = T["week"]
    emit("\n## Діагностики поза Холмом (1d, 2021+)")
    reach = T["t_reach"] > 0
    m = V & reach
    emit(
        f"H8: заповнень після досягнення цілі 2R до заповнення {int(m.sum())} з {int(V.sum())} ({m.sum() / max(V.sum(), 1):.1%}); їх E[R] {f(mci(T['B'][m], wk[m], NB_DESC))}"
    )
    lag = (T["t_reach"] - T["t_in"])[m] / HOUR_MS
    fl = (T["t_fill"] - T["t_in"]) / DAY_MS
    emit(
        f"H8: ціль досягнуто в першу годину після розміщення у {np.mean(lag < 1):.1%}, за першу добу у {np.mean(lag < 24):.1%}; "
        f"медіана часу до заповнення {np.median(fl[m]):.2f} доби (усі V {np.median(fl[V]):.2f})"
    )
    end = np.minimum(T["B_x"], T["t1"])
    end_c = np.where(reach, np.minimum(end, T["t_reach"] + HOUR_MS), end)
    s0, s1_ = ((end - T["t_in"])[VP] / DAY_MS).sum(), ((end_c - T["t_in"])[VP] / DAY_MS).sum()
    emit(
        f"H8: слот-доби ліміту без правила {s0:.0f}, зі скасуванням за ціллю {s1_:.0f} ({s1_ / s0 - 1:+.1%}); незаповнених, що дійшли до цілі, {int((VP & ~T['filled'] & reach).sum())}"
    )
    r = np.where(T["filled"], np.nan_to_num(T["B"]), 0.0)
    day = T["t_in"] // DAY_MS
    w = m17.cluster_weights(T["side"][VP].tolist(), day[VP].tolist())
    order = np.argsort(T["t_in"][VP], kind="stable")
    wn = w * w.size / w.sum()  # the same total risk as equal weights (critic B's finding 4)
    for lab, rr in (("рівний ризик", r[VP]), ("ризик / √k", r[VP] * w), ("ризик / √k, нормовано на рівний сумарний ризик", r[VP] * wn)):
        ws = np.bincount(np.unique(wk[VP], return_inverse=True)[1], weights=rr)
        emit(f"H10 {lab}: сума R {rr.sum():+.1f}, SD тижневої суми {ws.std(ddof=1):.2f}, макс. просадка {m17.max_drawdown(rr[order]):.1f}R")
    fm = T["filled"][VP]
    emit(
        f"H10 E[R] на одиницю ризику: рівний {r[VP][fm].mean():+.3f}, ризик / √k {(r[VP] * w)[fm].sum() / w[fm].sum():+.3f}; k > 1 у {np.mean(w < 1):.1%} розміщень"
    )
    emit("H11: не рахувалось (логіка слотів demo_trader не моделюється) — ◯")


def overlap(T: dict[str, np.ndarray], S: dict[str, np.ndarray]) -> None:
    V, VP = S["V"], S["VP"]
    d5 = np.where(T["ok5"], T["R5"], 0.0) - T["B"]
    d6 = np.nan_to_num(T["L"], nan=0.0) - np.nan_to_num(T["B"], nan=0.0)
    c5, c6 = np.abs(d5) > 1e-12, np.abs(d6) > 1e-12
    emit(
        f"\n## Перекриття: H5 змінює R у {c5[V].mean():.1%} заповнених V, H6 — у {c6[VP].mean():.1%} розміщень; обидва — {(c5 & c6)[V].mean():.1%} заповнених; ρ(Δ5, Δ6) на заповнених {rho(d5[V], d6[V]):+.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s17_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir / "S17"
    (out / "sanity").mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s17_modules --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
