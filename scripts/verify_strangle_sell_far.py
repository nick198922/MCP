"""Verify IDEA-20261028-long-strangle-sell-far-strike on cached Deribit BTC surfaces (no network).

Stage 0: coverage of fresh marks on the far strike at trigger days, episode counts, SD, MDE.
Stage 1: H2 ratio of the sold-strike mark at D to the bought-leg mark at T (per side) vs the same-date
model with unchanged IV (sticky smile). Stage 2: H1 expiry P&L of the management variants (descriptive)
and the non-trigger short-leg control. Pre-registration: the card's "Попередня реєстрація" section.

    uv run python scripts/verify_strangle_sell_far.py --stage 0
    uv run python scripts/verify_strangle_sell_far.py --stage all
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import statistics
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.strangle_manage import (  # noqa: E402
    Params,
    block_draws,
    build_episode,
    choose_series,
    control_short,
    cvar,
    dedup,
    h1_pnl,
    h2_verdict,
    mde,
    nontrigger,
    pct_ci,
    pick_strike,
)

CARD = ROOT / "docs" / "ideas" / "IDEA-20261028-long-strangle-sell-far-strike.md"
EVENTS = ROOT / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
SRC = Path.home() / ".cache" / "tradedesk-agents" / "put_skew"
H2_FIELDS = ("R", "R_mod", "R_stk", "R_net", "R_pct", "dR", "dR_stk", "iv_b", "iv_s", "iv_mod", "m_s")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def prereg_hash() -> str:
    txt = CARD.read_text(encoding="utf-8")
    a = txt.index("## Попередня реєстрація")
    b = txt.index("## Вердикт")
    return hashlib.sha256(txt[a:b].strip().encode()).hexdigest()[:16]


def load_days(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    days = []
    for k in sorted(raw["days"], key=int):
        d = raw["days"][k]
        exps = {}
        for exp_ms, e in d["expiries"]:
            exps[int(exp_ms)] = {"F": e["F"], "strikes": {float(K): {"mark_iv": s["mark_iv"]} for K, s in e["strikes"]}}
        days.append({"day": int(d["day"]), "t_ms": int(d["t_ms"]), "spot": d["spot"], "expiries": exps})
    del raw
    for a, b in zip(days, days[1:], strict=False):
        if b["day"] - a["day"] != 86_400_000:
            raise SystemExit(f"gap in days at {a['day']}")
    return days


def load_delivery(path: Path) -> dict[str, float]:
    return {r["date"]: float(r["delivery_price"]) for r in json.loads(path.read_text())}


def ymd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def fomc_ms() -> list[int]:
    with EVENTS.open(encoding="utf-8") as f:
        return [
            int(datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
            for r in csv.DictReader(f)
            if r["kind"] == "fomc"
        ]


def episodes(days: list[dict[str, Any]], p: Params, step: int = 3) -> list[dict[str, Any]]:
    return [build_episode(days, i, p) for i in range(0, len(days) - p.max_wait, step)]


def summ(vals: list[float], block: int, ndig: int = 3) -> dict[str, Any]:
    if not vals:
        return {"n": 0}
    ci = pct_ci(block_draws(vals, statistics.fmean, block=block))
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), ndig + 1),
        "sd": round(statistics.stdev(vals), ndig + 1) if len(vals) > 1 else None,
        "ci": [round(ci[0], ndig + 1), round(ci[1], ndig + 1)] if ci else None,
    }


def stage0(days: list[dict[str, Any]], eps: list[dict[str, Any]], p: Params) -> dict[str, Any]:
    from collections import Counter

    st = Counter(e["status"] for e in eps)
    reach = [e for e in eps if e["status"] in ("ok", "no_sell_mark")]
    out: dict[str, Any] = {"entries": len(eps), "status": dict(st)}
    for side in ("up", "down"):
        r = [e for e in reach if e["side"] == side]
        keys: dict[int, bool] = {}
        for e in r:
            keys[e["D"]] = keys.get(e["D"], False) or e["status"] == "ok"
        ok = [e for e in r if e["status"] == "ok"]
        rows = dedup(ok, lambda e: (e["D"], e["side"]), H2_FIELDS)
        sd = statistics.stdev([x["R"] for x in rows]) if len(rows) > 1 else None
        out[side] = {
            "episodes_reaching_D": len(r),
            "episode_coverage": round(len(ok) / len(r), 3) if r else None,
            "unique_D": len(keys),
            "unique_D_covered": sum(keys.values()),
            "coverage_D": round(sum(keys.values()) / len(keys), 3) if keys else None,
            "n_dedup_ok": len(rows),
            "sd_R": round(sd, 3) if sd else None,
            "mde_R": round(mde(sd, len(rows)), 3) if sd else None,
        }
    # descriptive: unconditional far-strike coverage on every day, series DTE 14-21
    n_days = cov_up = cov_dn = 0
    for d in days:
        exp = choose_series(d["expiries"], d["t_ms"], p.dte, p.dte_mid)
        if exp is None:
            continue
        n_days += 1
        ks = list(d["expiries"][exp]["strikes"])
        cov_up += pick_strike(ks, d["spot"], 1 + p.sell_off, p.sell_band) is not None
        cov_dn += pick_strike(ks, d["spot"], 1 - p.sell_off, p.sell_band) is not None
    out["all_days"] = {"days_with_series": n_days, "cov_call_+8": round(cov_up / n_days, 3), "cov_put_-8": round(cov_dn / n_days, 3)}
    ent = len(eps) - st.get("no_series", 0)
    out["entry_both_legs"] = (
        round((len(eps) - st.get("no_series", 0) - st.get("no_put", 0) - st.get("no_call", 0)) / ent, 3) if ent else None
    )
    by_year: dict[str, list[int]] = {}
    for e in reach:
        y = ymd(e["D"])[:4]
        c = by_year.setdefault(y, [0, 0])
        c[0] += 1
        c[1] += e["status"] == "ok"
    out["episode_coverage_by_year"] = {y: f"{c[1]}/{c[0]}" for y, c in sorted(by_year.items())}
    return out


def h2_side(rows: list[dict[str, Any]], block: int) -> dict[str, Any]:
    res = {
        f: summ([r[f] for r in rows if r[f] is not None], block)
        for f in ("R", "R_mod", "R_stk", "R_net", "R_pct", "dR", "dR_stk", "iv_b", "iv_s", "iv_mod", "m_s")
    }
    res["iv_s_minus_iv_mod"] = summ([r["iv_s"] - r["iv_mod"] for r in rows if r["iv_mod"] is not None], block)
    res["share_R_ge_0.9"] = round(sum(r["R"] >= 0.9 for r in rows) / len(rows), 3) if rows else None
    res["share_R_ge_1.0"] = round(sum(r["R"] >= 1.0 for r in rows) / len(rows), 3) if rows else None
    return res


def h2_block(eps: list[dict[str, Any]], cov: dict[str, Any], block: int = 5) -> dict[str, Any]:
    ok = [e for e in eps if e["status"] == "ok"]
    out = {}
    for side in ("up", "down"):
        rows = dedup([e for e in ok if e["side"] == side], lambda e: (e["D"], e["side"]), H2_FIELDS)
        rows.sort(key=lambda r: r["key"][0])
        h = h2_side(rows, block)
        r = h["R"]
        h["verdict"] = (
            h2_verdict(r["mean"], tuple(r["ci"]) if r.get("ci") else None, coverage=cov[side]["coverage_D"] or 0.0, n=r["n"])
            if r["n"]
            else "◯"
        )
        out[side] = h
    return out


def compact(h: dict[str, Any]) -> str:
    def f(k: str) -> str:
        s = h[k]
        if not s.get("n"):
            return f"{k}=n/a"
        ci = s.get("ci")
        return f"{k}={s['mean']:.3f}" + (f"[{ci[0]:.3f},{ci[1]:.3f}]" if ci else "")

    return (
        f"n={h['R']['n']} " + " ".join(f(k) for k in ("R", "R_mod", "dR", "R_stk", "R_net", "R_pct")) + f" verdict={h.get('verdict', '')}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="0", choices=["0", "all"])
    ap.add_argument("--snap", default=str(SRC / "snapshots_v2.json"))
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "tradedesk-agents" / "long_strangle_sell_far"))
    a = ap.parse_args()
    out_dir = Path(a.cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"prereg sha256[:16] = {prereg_hash()}", flush=True)
    days = load_days(Path(a.snap))
    print(f"days {len(days)} {ymd(days[0]['day'])}..{ymd(days[-1]['day'])} rss {rss_mb():.0f} MB", flush=True)
    p = Params()
    eps = episodes(days, p)
    cov = stage0(days, eps, p)
    print("STAGE0 " + json.dumps(cov, ensure_ascii=False), flush=True)
    result: dict[str, Any] = {"prereg": prereg_hash(), "stage0": cov}
    if a.stage == "0":
        (out_dir / "stage0.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        return
    if all((cov[s]["coverage_D"] or 0) < 0.60 for s in ("up", "down")):
        print("coverage < 60 % on both sides: inconclusive, stop", flush=True)
        (out_dir / "stage0.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        return

    # ---- H2 primary
    h2 = h2_block(eps, cov)
    result["h2"] = h2
    for side in ("up", "down"):
        print(f"H2 {side:4s} " + compact(h2[side]), flush=True)
        print(
            f"   iv_s-iv_mod={h2[side]['iv_s_minus_iv_mod']}  share R>=0.9 {h2[side]['share_R_ge_0.9']}  R>=1 {h2[side]['share_R_ge_1.0']}",
            flush=True,
        )

    # ---- robustness (descriptive)
    sens: dict[str, Any] = {}
    for name, q in (
        ("trig_4pct", replace(p, trig=0.04)),
        ("sell_6pct", replace(p, sell_off=0.06)),
        ("sell_10pct", replace(p, sell_off=0.10)),
        ("cost_1vp", replace(p, cost_vp=0.01)),
        ("cost_2vp", replace(p, cost_vp=0.02)),
        ("cost_3vp", replace(p, cost_vp=0.03)),
    ):
        e2 = episodes(days, q)
        c2 = stage0(days, e2, q)
        sens[name] = {"coverage": {s: c2[s]["coverage_D"] for s in ("up", "down")}, **h2_block(e2, c2)}
        for side in ("up", "down"):
            print(f"SENS {name:10s} {side:4s} cov={c2[side]['coverage_D']} " + compact(sens[name][side]), flush=True)
    for side in ("up", "down"):
        ok = [e for e in eps if e["status"] == "ok" and e["side"] == side]
        rows = sorted(dedup(ok, lambda e: (e["D"], e["side"]), H2_FIELDS), key=lambda r: r["key"][0])
        b10 = summ([r["R"] for r in rows], 10)
        top = sorted(rows, key=lambda r: r["R"], reverse=True)[5:]
        notop = summ([r["R"] for r in sorted(top, key=lambda r: r["key"][0])], 5)
        print(f"SENS block10 {side} R={b10}  | without top5 R={notop}", flush=True)
        years: dict[str, list[float]] = {}
        dyears: dict[str, list[float]] = {}
        for r in rows:
            years.setdefault(ymd(r["key"][0])[:4], []).append(r["R"])
            if r["dR"] is not None:
                dyears.setdefault(ymd(r["key"][0])[:4], []).append(r["dR"])
        yr = {
            y: (len(v), round(statistics.fmean(v), 3), round(statistics.fmean(dyears.get(y, [0.0])), 3)) for y, v in sorted(years.items())
        }
        print(f"YEARS {side} (n, mean R, mean dR): {yr}", flush=True)
        fomc = fomc_ms()
        fr = [r for r in rows if any(r["first"]["T"] < f <= r["first"]["exp"] for f in fomc)]
        print(f"FOMC {side}: R={summ([r['R'] for r in fr], 5)} dR={summ([r['dR'] for r in fr if r['dR'] is not None], 5)}", flush=True)
        sens[f"block10_{side}"], sens[f"notop5_{side}"], sens[f"years_{side}"] = b10, notop, yr
    result["sens"] = sens

    # ---- EXPLORATORY (added after seeing H2 means): medians and R by realised move at D
    expl: dict[str, Any] = {}
    for side in ("up", "down"):
        ok = [e for e in eps if e["status"] == "ok" and e["side"] == side]
        for e in ok:
            e["move"] = abs(e["S_D"] / e["S_T"] - 1.0)
            e["ks_off"] = abs(e["K_s"] / e["S_T"] - 1.0)
            e["kb_off"] = abs(e["K_b"] / e["F_T"] - 1.0)
            e["dte_d"] = e["tau_D"] * 365.0
        rows = sorted(
            dedup(ok, lambda e: (e["D"], e["side"]), (*H2_FIELDS, "move", "ks_off", "kb_off", "dte_d")), key=lambda r: r["key"][0]
        )
        med = {
            f: round(statistics.median([r[f] for r in rows if r[f] is not None]), 3)
            for f in ("R", "R_mod", "dR", "R_net", "move", "ks_off", "kb_off", "dte_d")
        }
        buckets = {}
        for lo, hi in ((0.03, 0.04), (0.04, 0.05), (0.05, 0.07), (0.07, 1.0)):
            b = [r for r in rows if lo <= r["move"] < hi]
            if b:
                buckets[f"{lo:.2f}-{hi:.2f}"] = {
                    "n": len(b),
                    "R_mean": round(statistics.fmean(r["R"] for r in b), 3),
                    "R_med": round(statistics.median(r["R"] for r in b), 3),
                    "R_mod_mean": round(statistics.fmean(r["R_mod"] for r in b if r["R_mod"] is not None), 3),
                    "dR_mean": round(statistics.fmean(r["dR"] for r in b if r["dR"] is not None), 3),
                    "iv_s-iv_mod": round(statistics.fmean(r["iv_s"] - r["iv_mod"] for r in b if r["iv_mod"] is not None), 4),
                }
        # median CI by block bootstrap (exploratory)
        mci = pct_ci(block_draws([r["R"] for r in rows], statistics.median, block=5))
        expl[side] = {"median": med, "median_R_ci": mci, "by_move": buckets}
        print(f"EXPL {side} medians {med} median_R_ci {mci}", flush=True)
        for k, v in buckets.items():
            print(f"   move {k}: {v}", flush=True)
    result["exploratory"] = expl

    # ---- H1 descriptive + control
    deliv = load_delivery(SRC / "delivery_btc_usd.json")
    by_day = {d["day"]: d for d in days}

    def s_exp_of(exp: int) -> float | None:
        return deliv.get(ymd(exp))

    h1: dict[str, Any] = {}
    spots = [d["spot"] for d in days]
    for side in ("up", "down"):
        rows_e = []
        for e in eps:
            if e["status"] != "ok" or e["side"] != side or s_exp_of(e["exp"]) is None:
                continue
            pnl = h1_pnl(e, by_day[e["D"]], s_exp_of(e["exp"]), p)
            rows_e.append({**e, **pnl, "g_a": pnl["g"] - pnl["a"], "b_a": pnl["b"] - pnl["a"], "c_a": pnl["c"] - pnl["a"]})
        rows = sorted(
            dedup(rows_e, lambda e: (e["D"], e["side"]), ("a", "g", "b", "c", "short", "g_a", "b_a", "c_a", "m_s")),
            key=lambda r: r["key"][0],
        )
        res: dict[str, Any] = {"n": len(rows)}
        for v in ("a", "g", "b", "c"):
            vals = [r[v] for r in rows]
            res[v] = {**summ(vals, 5), "cvar5": round(cvar(vals), 4), "share_ge0": round(sum(x >= 0 for x in vals) / len(vals), 3)}
        for v in ("g_a", "b_a", "c_a"):
            res[v] = summ([r[v] for r in rows], 5)
        res["cvar_gain_g_vs_a"] = round(res["g"]["cvar5"] - res["a"]["cvar5"], 4)
        m = statistics.fmean([r["m_s"] for r in rows])
        ctrl = []
        for j, d in enumerate(days):
            if nontrigger(spots, j, p.max_wait, p.trig):
                v = control_short(d, side, m, s_exp_of, p)
                if v is not None:
                    ctrl.append(v)
        short = [r["short"] for r in rows]
        dr = block_draws(short, statistics.fmean, block=5)
        dc = block_draws(ctrl, statistics.fmean, block=5, seed=11)
        dc15 = block_draws(ctrl, statistics.fmean, block=15, seed=11)
        res["short_episode"] = summ(short, 5)
        res["control"] = {"m": round(m, 4), **summ(ctrl, 5)}
        res["control_block15"] = summ(ctrl, 15)
        res["short_minus_control"] = {
            "mean": round(statistics.fmean(short) - statistics.fmean(ctrl), 4),
            "ci": pct_ci([x - y for x, y in zip(dr, dc, strict=True)]),
            "ci_ctrl_block15": pct_ci([x - y for x, y in zip(dr, dc15, strict=True)]),
        }
        ni = pct_ci(block_draws([r["g_a"] for r in rows], statistics.fmean, block=5))  # unrounded, same seed
        res["g_a_ci_raw"] = ni
        res["noninferior_g"] = bool(ni and ni[0] >= -0.003)
        print(f"   g_a CI unrounded {ni}", flush=True)
        res["cvar_better_1pct"] = res["cvar_gain_g_vs_a"] >= 0.01
        h1[side] = res
        print(f"H1 {side} n={len(rows)}", flush=True)
        for v in ("a", "g", "b", "c"):
            print(f"   {v}: {res[v]}", flush=True)
        for v in ("g_a", "b_a", "c_a"):
            print(f"   {v}: {res[v]}", flush=True)
        print(
            f"   cvar gain g-a {res['cvar_gain_g_vs_a']} noninferior {res['noninferior_g']} cvar>=1% {res['cvar_better_1pct']}", flush=True
        )
        print(f"   short(ep, % S_D) {res['short_episode']} control {res['control']} ctrl_b15 {res['control_block15']}", flush=True)
        print(f"   short-control {res['short_minus_control']}", flush=True)
    result["h1"] = h1
    (out_dir / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    print(f"done rss {rss_mb():.0f} MB -> {out_dir / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()
