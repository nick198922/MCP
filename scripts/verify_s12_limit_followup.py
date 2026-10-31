#!/usr/bin/env python3
"""Item 10d (IDEA-20261030-demo-setups-depth, registration «Попередня реєстрація 10d»): follow-up of a filled S12 v7
limit - exit at the close of the 6th 1h bar after the fill bar unless a close above the level and the fill bar's high
confirmed (S28 H6 definition, no new choice). Paired against the S12 v7 passport base (verdict sample V: top-100 and
>= $5M, demo selection, demo chain), with a same-frequency placebo, IS / OOS, years, without the top-5 coins, costs
x1.5, trades and R per year. S28 is descriptive only (the data of the finding).

    uv run python scripts/verify_s12_limit_followup.py --setup S12 --stage sanity
    uv run python scripts/verify_s12_limit_followup.py --setup S12 --stage run     # per-symbol rows (local caches)
    uv run python scripts/verify_s12_limit_followup.py --setup S12 --stage power   # n, SE, MDE80 only (no means)
    uv run python scripts/verify_s12_limit_followup.py --setup S12 --stage report

No network: 1h bars from the S14 cache (``course_s14_ema``), passport rows from ``setup_passport/<setup>/cands``.
Outputs (rows, <stage>.txt) under ``~/.cache/tradedesk-agents/s12_limit_followup/<setup>/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s28_modules as v28  # noqa: E402
import verify_s38_modules as v38  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import limit_followup as lf  # noqa: E402
from tradedesk.analytics import s28_modules as m28  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.course_s14_ema import resample  # noqa: E402
from tradedesk.analytics.stop_management import risk_unit  # noqa: E402

HOLD = vsp.HOLD["4h"]
END = vsp.END["4h"]
INTEREST = 0.05
MDE_K = 2.8  # z(0.975) + z(0.80)
N_PERM = 2000
emit = vsp.emit
YEAR_MS = 365.25 * vsp.DAY_MS


# ---------------------------------------------------------------- per-symbol rows


def run_symbol(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    setup, s, s1_cache, data_dir, out_dir = args
    t0w = time.time()
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    tb = resample(h1, 4)
    det = vsp.SETUPS[setup][0]
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for sig in det(tb, "4h"):
        t0 = sig.signal_ts + tb.bar_ms
        if not (vsp.START_MS <= t0 < END):
            continue
        i = int(np.searchsorted(tb.t, sig.signal_ts))
        t1 = int(sig.indicators["rest_until_ms"])
        sg = 1.0 if sig.side == "long" else -1.0
        e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
        bb = tb if sig.side == "long" else tb.mirrored()
        path = h1 if sig.side == "long" else h1.mirrored()
        if not (st < e < tg) or not float(bb.c[i]) > e:
            continue
        fu = lf.followup_trade(path, t0, t1, e, st, tg, HOLD, vsp.RULE_B, vsp.F1)
        if fu is None:
            continue
        fu15 = lf.followup_trade(path, t0, t1, e, st, tg, HOLD, vsp.RULE_B, vsp.F15)
        assert fu15 is not None and fu15.early == fu.early
        why[f"{sig.side}:filled"] += 1
        why[f"{sig.side}:open6"] += int(fu.open_at_end)
        why[f"{sig.side}:early"] += int(fu.early)
        rows.append(
            {
                "sym": s,
                "side": sig.side,
                "t_in": t0,
                "B": fu.base.r_net,
                "B15": fu15.base.r_net,
                "R6": fu.trunc.r_net,
                "R6_15": fu15.trunc.r_net,
                "open": int(fu.open_at_end),
                "early": int(fu.early),
                "dm": (fu.m - fu.k) if fu.m >= 0 else -1,
                "kind": fu.base.exit_kind,
                "rp": risk_unit(e, st) / abs(e),  # R -> fraction of the entry price
            }
        )
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0w, "rss": vsp.rss_mb()}


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((a.setup, s, str(a.s1_cache), str(a.data_dir), str(out / "sanity")))
        emit(f"timing {s}: {r['sec']:.1f}s; {r['why']}; rss {r['rss']:.0f} MB")
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.1f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [(a.setup, s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "rows" / f"{s}.json").exists()]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


# ---------------------------------------------------------------- the verdict sample, joined to the module rows


def sample(a: argparse.Namespace, out: Path) -> dict[str, np.ndarray]:
    """Passport rows of the verdict sample V (filled), joined with the module rows on (symbol, side, placement, base R)."""
    T = vsp.load_rows(a.passport_cache / a.setup)
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    top100 = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    guard = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    sel = top100 & turn5 & guard & (T["t_in"] >= vsp.PASSPORT_MS)
    V = vsp.chain(T, sel) & T["filled"]
    causal = causal_chain(T, sel)
    mine: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for p in sorted((out / "rows").glob("*.json")):
        for r in json.loads(p.read_text())["rows"]:
            mine[(r["sym"], r["side"], int(r["t_in"]))].append(r)
    idx = np.flatnonzero(V | causal)
    cols: dict[str, list[Any]] = defaultdict(list)
    miss = amb = 0
    for i in idx:
        cand = [r for r in mine.get((str(T["sym"][i]), str(T["side"][i]), int(T["t_in"][i])), []) if abs(r["B"] - T["B"][i]) < 1e-9]
        if not cand:
            miss += 1
            continue
        amb += int(len(cand) > 1)
        r = cand[0]
        for k in ("sym", "side", "t_in", "B", "B15", "R6", "R6_15", "open", "early", "dm", "kind", "rp"):
            cols[k].append(r[k])
        cols["V"].append(bool(V[i]))
        cols["C"].append(bool(causal[i]))
        cols["B15_pp"].append(float(T["B15"][i]))
    emit(
        f"join: V {int(V.sum())} filled trades (2021+), causal-analogue chain {int(causal.sum())}; matched {len(cols['B'])} of {idx.size}, "
        f"missing {miss}, ambiguous {amb}"
    )
    S = {k: np.array(v) for k, v in cols.items()}
    S["t_in"] = S["t_in"].astype(np.int64)
    for k in ("open", "early"):
        S[k] = S[k].astype(bool)
    S["week"] = S["t_in"] // sp.WEEK_MS
    S["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in S["t_in"]])
    S["mod"] = np.where(S["early"], S["R6"], S["B"])
    S["mod15"] = np.where(S["early"], S["R6_15"], S["B15"])
    S["d"] = S["R6"] - S["B"]
    S["delta"] = S["mod"] - S["B"]
    S["delta15"] = S["mod15"] - S["B15"]
    bad = int(np.sum(~S["open"] & (np.abs(S["d"]) > 1e-12)))
    b15 = float(np.nanmax(np.abs(S["B15"] - S["B15_pp"]))) if S["B15"].size else 0.0
    emit(f"checks: trades closed by k+6 with R6 != B: {bad} (must be 0); max |B15 - passport B15| {b15:.1e}")
    return S


def causal_chain(T: dict[str, np.ndarray], m: np.ndarray) -> np.ndarray:
    """``check_prelimit_chain.chain_causal``: a kept trade blocks its symbol and side between its fill and its exit
    only (not a one-position rule). Descriptive row."""
    t_fill = np.array([int(x) for x in T["t_fill"]])
    idx = np.flatnonzero(m)
    idx = idx[np.lexsort((T["order"][idx], T["t_in"][idx]))]
    open_: dict[tuple[str, str], list[tuple[int, int]]] = {}
    keep = np.zeros(m.size, bool)
    for k in idx:
        key = (str(T["sym"][k]), str(T["side"][k]))
        t0 = int(T["t_in"][k])
        if any(f <= t0 < x for f, x in open_.get(key, [])):
            continue
        if np.isfinite(T["B"][k]):
            keep[k] = True
            open_.setdefault(key, []).append((int(t_fill[k]), int(T["B_x"][k])))
    return keep


# ---------------------------------------------------------------- stats


def mci(v: np.ndarray, wk: np.ndarray) -> dict[str, float]:
    return sp.mean_ci(v, wk, n_boot=vsp.N_BOOT, seed=vsp.SEED)


def stage_power(a: argparse.Namespace, out: Path) -> None:
    S = sample(a, out)
    oos = S["t_in"] >= vsp.OOS_MS
    for lab, m in (
        ("longs 2021+ (primary)", S["V"] & (S["side"] == "long")),
        ("longs OOS", S["V"] & (S["side"] == "long") & oos),
        ("shorts 2021+", S["V"] & (S["side"] == "short")),
        ("shorts OOS", S["V"] & (S["side"] == "short") & oos),
    ):
        dl = S["delta"][m]
        r = mci(dl - dl.mean(), S["week"][m])  # centred: the mean is not shown
        emit(
            f"{lab:22s} n {m.sum():6d}, weeks {int(r['k'])}, open after k+6 {S['open'][m].mean():.1%}, early exits {S['early'][m].mean():.1%}; "
            f"SE {r['se']:.4f}, MDE80 {MDE_K * r['se']:.4f} (interest {INTEREST})"
        )


def span(lab: str) -> tuple[int, int]:
    """The calendar span of a row's placements: 2021+, IS, OOS or one year (clipped to the passport period)."""
    if "2021+" in lab:
        return vsp.PASSPORT_MS, END
    if "IS" in lab:
        return vsp.PASSPORT_MS, vsp.OOS_MS
    if "OOS" in lab:
        return vsp.OOS_MS, END
    for y in range(2021, 2027):
        if str(y) in lab:
            return max(vsp.PASSPORT_MS, vsp.s1.ms(f"{y}-01-01T00:00")), min(END, vsp.s1.ms(f"{y + 1}-01-01T00:00"))
    return vsp.PASSPORT_MS, END


def per_year(v: np.ndarray, lo_hi: tuple[int, int]) -> tuple[float, float]:
    """Trades per year and the sum of R per year over the row's calendar span."""
    yrs = (lo_hi[1] - lo_hi[0]) / YEAR_MS
    return v.size / yrs, float(v.sum()) / yrs


def fmt(r: dict[str, float]) -> str:
    return f"{r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]"


def row(lab: str, S: dict[str, np.ndarray], m: np.ndarray) -> dict[str, float]:
    wk = S["week"][m]
    b, md, dl = mci(S["B"][m], wk), mci(S["mod"][m], wk), mci(S["delta"][m], wk)
    b15, md15 = float(S["B15"][m].mean()), float(S["mod15"][m].mean())
    d15 = mci(S["delta15"][m], wk)
    sp_ = span(lab)
    npy, sb = per_year(S["B"][m], sp_)
    _, sm = per_year(S["mod"][m], sp_)
    _, sm15 = per_year(S["mod15"][m], sp_)
    pct = float(np.mean(S["delta"][m] * S["rp"][m]))
    emit(
        f"{lab:24s} n {m.sum():6d} early {S['early'][m].mean():5.1%} | base {fmt(b)} mod {fmt(md)} | Δ {fmt(dl)} p1 {m28.one_sided(dl['m'], dl['p']):.3f} "
        f"| x1.5 base {b15:+.3f} mod {md15:+.3f} Δ {fmt(d15)} | {npy:5.0f}/yr, ΣR/yr base {sb:+.1f} mod {sm:+.1f} (x1.5 {sm15:+.1f}) | Δ % price {pct:+.4%}"
    )
    return {"d": dl["m"], "lo": dl["lo"], "hi": dl["hi"], "p": dl["p"], "se": dl["se"], "mod": md["m"], "mod15": md15, "d15": d15["m"]}


def placebo(S: dict[str, np.ndarray], m: np.ndarray, lab: str) -> dict[str, float]:
    d, e, o = S["d"][m], S["early"][m], S["open"][m]
    x = lf.placebo_contrast(d, e, o)
    c = mci(x, S["week"][m])
    dm = float(np.mean(d * e))
    rng = np.random.default_rng(vsp.SEED)
    oi = np.flatnonzero(o)
    ne = int(e.sum())
    perm = np.array([d[rng.choice(oi, ne, replace=False)].sum() / d.size for _ in range(N_PERM)])
    pp = (np.sum(perm >= dm) + 1) / (N_PERM + 1)
    emit(
        f"placebo {lab:16s}: early {ne} of {oi.size} open after k+6 (q {ne / max(1, oi.size):.1%}); E[Δ placebo] {np.mean(perm):+.3f}; "
        f"C = Δ - E[Δ placebo] {fmt(c)} p1 {m28.one_sided(c['m'], c['p']):.3f}; perm p {pp:.4f}"
    )
    return {"m": c["m"], "p1": m28.one_sided(c["m"], c["p"])}


def stage_report(a: argparse.Namespace, out: Path) -> None:
    S = sample(a, out)
    V, oos = S["V"], S["t_in"] >= vsp.OOS_MS
    L, Sh = V & (S["side"] == "long"), V & (S["side"] == "short")
    if a.setup == "S12":
        dl = S["delta"][L]
        se = mci(dl - dl.mean(), S["week"][L])["se"]
        emit(f"power gate (longs 2021+): MDE80 {MDE_K * se:.4f} vs {INTEREST}")
        if MDE_K * se > INTEREST:
            emit("verdict: ◯ only forward (MDE80 > interest); no means computed")
            return
    else:
        L = V & (S["side"] == "long")
        emit("S28: data of the finding, descriptive only")
    emit("## rows (filled trades of V; R per trade risk, not capital)")
    res = {
        "L": row("longs 2021+", S, L),
        "LIS": row("longs IS", S, L & ~oos),
        "LOOS": row("longs OOS", S, L & oos),
    }
    for y in sorted(set(S["year"][L].tolist())):
        row(f"longs {y}", S, L & (S["year"] == y))
    if Sh.any():
        res["S"] = row("shorts 2021+ (mirror)", S, Sh)
        row("shorts IS", S, Sh & ~oos)
        row("shorts OOS", S, Sh & oos)
        row("both 2021+", S, V)
        row("both OOS", S, V & oos)
    C = S["C"] & (S["side"] == "long")
    row("longs causal-analogue", S, C)
    emit("## placebo (same number of 6th-hour exits among trades open after k+6)")
    pl = placebo(S, L, "longs 2021+")
    placebo(S, L & oos, "longs OOS")
    if Sh.any():
        placebo(S, Sh, "shorts 2021+")
    emit("## without the 5 coins with the largest contribution to Δ")
    nt5 = v28.drop_top5_mean(S["delta"][L], S["sym"][L])
    nt5o = v28.drop_top5_mean(S["delta"][L & oos], S["sym"][L & oos])
    emit(f"longs 2021+ Δ without top-5 {nt5:+.3f}; OOS {nt5o:+.3f}")
    emit("## anatomy (descriptive; conditioned on the trade's own path, as S28 H6)")
    for lab, m in (("longs 2021+", L), ("longs OOS", L & oos)):
        o, e = S["open"][m], S["early"][m]
        conf = o & ~e
        emit(
            f"{lab}: open after k+6 {o.mean():.1%}; of them confirmed {conf.sum() / max(1, o.sum()):.1%}; "
            f"base R | early-exit trades {S['B'][m][e].mean():+.3f}, their R at k+6 {S['R6'][m][e].mean():+.3f}; "
            f"base R | confirmed {S['B'][m][conf].mean():+.3f} (R at k+6 {S['R6'][m][conf].mean():+.3f}); base R | closed by k+6 {S['B'][m][~o].mean():+.3f}"
        )
        kinds = Counter(S["kind"][m][e].tolist())
        emit(f"  base exit kinds of early-exit trades: {dict(kinds.most_common())}")
        dm = S["dm"][m][conf]
        if dm.size:
            emit(f"  confirmation delay (bars after k) of confirmed open trades: {dict(sorted(Counter(dm.tolist()).items()))}")
    emit("## 5a (Δ per coin, longs 2021+)")
    v28.per_asset_mean("Δ longs", S["delta"][L], S["sym"][L], S["week"][L], L[L] & ~oos[L], oos[L])
    if a.setup != "S12":
        return
    r = res["L"]
    ci_pos = r["lo"] > 0
    pts = res["LIS"]["d"] > 0 and res["LOOS"]["d"] > 0 and nt5 > 0 and r["d15"] > 0
    if ci_pos and pl["p1"] < 0.05 and pts:
        v = "✓"
    elif r["d"] > 0 and pl["m"] > 0:
        v = "≈"
    elif r["hi"] < INTEREST:
        v = "✗ (useful effect excluded)"
    else:
        v = "◯"
    lo = res["LOOS"]
    c7 = lo["lo"] > 0 and lo["mod15"] > 0
    emit(
        f"verdict (registered rule, longs 2021+): {v}; 7c: {'yes' if c7 else 'no'} (Δ OOS lo {lo['lo']:+.3f}, E[R] mod x1.5 OOS {lo['mod15']:+.3f})"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", choices=("S12", "S28"), default="S12")
    ap.add_argument("--stage", choices=("sanity", "run", "power", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_limit_followup")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir / a.setup
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        emit(f"verify_s12_limit_followup --setup {a.setup} --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"sanity": stage_sanity, "run": stage_run, "power": stage_power, "report": stage_report}[a.stage](a, out)
        emit(f"peak rss {vsp.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
