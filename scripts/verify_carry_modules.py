"""Carry modules for strategy A (card IDEA-20261031-structural-edges-depth, item 3): H1 rank, H7 exit / rotation,
H4 spot-as-collateral capital. Cached data only (paper_carry_bench), no network.

    uv run python scripts/verify_carry_modules.py time
    uv run python scripts/verify_carry_modules.py run [--placebo 200]

Full output: ~/.cache/tradedesk-agents/carry_modules/results.json; compact tables on stdout.
"""

from __future__ import annotations

import argparse
import bisect
import json
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tradedesk.analytics.carry import YEAR_MS  # noqa: E402
from tradedesk.analytics.carry_costs import CostModel, max_drawdown  # noqa: E402
from tradedesk.analytics.carry_modules import (  # noqa: E402
    block_bootstrap,
    capital_factor,
    cluster_bootstrap_total,
    diff_curve,
    holm,
    paired_symbol_diff,
    pnl_by_symbol,
    simulate_carry_modules,
    tiered_haircut,
    window_weekly,
)

CACHE = Path.home() / ".cache" / "tradedesk-agents"
BENCH = CACHE / "paper_carry_bench"
OUT = CACHE / "carry_modules"
CAL = CACHE / "carry_cost_calibration" / "calibration.json"
LADDER = Path(__file__).resolve().parent.parent / "docs" / "paper" / "ladder" / "2026-10-17-bybit.json"
LEGS_A = ["spot_binance", "perp_binance"]
CAP = 1.5
W = 1 / 5
OOS = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp() * 1000)
Y2026 = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
N_BOOT = 10_000
T_START = time.time()


def rss() -> str:
    return f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def log(msg: str) -> None:
    print(f"[{time.time() - T_START:6.0f}s rss {rss()}] {msg}", flush=True)


def pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def load() -> tuple[dict[str, Any], dict[str, Any]]:
    import verify_carry_alts as alts

    uni = {k: tuple(v) for k, v in json.loads((BENCH / "universe_a.json").read_text()).items()}
    mk = alts.load_markets(BENCH, 730, uni)  # type: ignore[arg-type]
    log(f"universe {len(uni)}, with prices {len(mk)}")
    return mk, uni


def models() -> dict[str, CostModel]:
    cal = json.loads(CAL.read_text())["models"]
    m = {k: CostModel(v["fees"], v["slippage"], v["timing"]) for k, v in cal.items() if k in ("old", "main", "stress")}
    # Sensitivity registered before the run (critic B notes 4-5): A-specific perp slippage 0.153 %, no timing term.
    m["a_perp"] = CostModel(m["main"].fees, {**m["main"].slippage, "perp_binance": 0.00153}, 0.0)
    return m


def sim(mk: dict[str, Any], cm: CostModel, **mod: Any) -> dict[str, Any]:
    return simulate_carry_modules(
        mk,
        slots=5,
        entry_apr=0.20,
        spot_fee=cm.fees["spot_binance"],
        perp_fee=cm.fees["perp_binance"],
        slippage=cm.sim_slippage(LEGS_A),
        **mod,
    )


def summary(res: dict[str, Any], cap: float = CAP) -> dict[str, Any]:
    st, eq = res["stats"], res["equity"]
    wk = window_weekly(eq, 0)
    worst = min(wk, key=lambda x: x[1])
    oos_years = (st["end_ms"] - OOS) / YEAR_MS
    oos_total = sum(v for _, v in window_weekly(eq, OOS))
    return {
        "n_trades": st["n_trades"],
        "n_rotations": st["n_rotations"],
        "apr_notional": st["apr_on_notional"],
        "apr_capital": st["apr_on_notional"] / cap,
        "oos_apr_capital": oos_total / oos_years / cap,
        "max_dd_capital": max_drawdown(eq) / cap,
        "worst_week_capital": worst[1] / cap,
        "worst_week": datetime.fromtimestamp(worst[0] / 1000, UTC).strftime("%Y-%m-%d"),
        "costs_per_year_notional": st["costs"] / st["years"],
        "utilisation": st["slot_utilisation"],
    }


def paired(var: dict[str, Any], base: dict[str, Any], cap: float = CAP) -> dict[str, Any]:
    """Paired gain of ``var`` over ``base`` on capital (both / cap), OOS weekly block CI + symbol-cluster CI."""
    d = diff_curve(var["equity"], base["equity"])
    end = base["stats"]["end_ms"]
    oos_years = (end - OOS) / YEAR_MS
    wk = [v / cap for _, v in window_weekly(d, OOS)]
    bb = block_bootstrap(wk, n_boot=N_BOOT)
    sym = paired_symbol_diff(pnl_by_symbol(var["trades"], W / cap, OOS), pnl_by_symbol(base["trades"], W / cap, OOS))
    cb = cluster_bootstrap_total(sym, oos_years, n_boot=N_BOOT)
    seg = {}
    for name, a, b in (("2025", OOS, Y2026), ("2026", Y2026, None)):
        w = window_weekly(d, a, b)
        yrs = ((b or end) - a) / YEAR_MS
        seg[name] = sum(v for _, v in w) / yrs / cap
    top5 = sorted(sym, key=lambda k: sym[k], reverse=True)[:5]
    wo_top5 = (sum(sym.values()) - sum(sym[k] for k in top5)) / oos_years
    is_w = window_weekly(d, 0, OOS)
    is_years = (OOS - base["stats"]["start_ms"]) / YEAR_MS
    sym_full = paired_symbol_diff(pnl_by_symbol(var["trades"], W / cap), pnl_by_symbol(base["trades"], W / cap))
    return {
        "oos_gain": bb["apr"], "oos_lo": bb["lo"], "oos_hi": bb["hi"], "p": bb["p"], "weeks": bb["weeks"],
        "sym_gain": cb["apr"], "sym_lo": cb["lo"], "sym_hi": cb["hi"], "sym_p": cb["p"], "clusters": cb["clusters"],
        "seg2025": seg["2025"], "seg2026": seg["2026"], "oos_wo_top5": wo_top5, "top5": top5,
        "is_gain": sum(v for _, v in is_w) / is_years / cap,
        "full_gain": (var["stats"]["total_return"] - base["stats"]["total_return"]) / base["stats"]["years"] / cap,
        "sym_full": sym_full,
    }  # fmt: skip


def five_a(var: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Per-symbol contributions to the paired difference (on capital), IS vs OOS persistence of the top quarter."""
    is_ = paired_symbol_diff(pnl_by_symbol(var["trades"], W / CAP, 0, OOS), pnl_by_symbol(base["trades"], W / CAP, 0, OOS))
    oos = paired_symbol_diff(pnl_by_symbol(var["trades"], W / CAP, OOS), pnl_by_symbol(base["trades"], W / CAP, OOS))
    nz = sorted((s for s in is_ if abs(is_[s]) > 1e-12), key=lambda s: is_[s], reverse=True)
    q = max(1, len(nz) // 4)
    top = nz[:q]
    rest = [s for s in set(is_) | set(oos) if s not in top]
    tot = sum(oos.values())
    top_oos = sorted(oos, key=lambda s: abs(oos[s]), reverse=True)[:8]
    return {
        "n_is_nonzero": len(nz), "top_is": top, "top_is_oos": sum(oos.get(s, 0.0) for s in top),
        "rest_oos": sum(oos.get(s, 0.0) for s in rest),
        "top5_share_oos": (sum(sorted(oos.values(), reverse=True)[:5]) / tot) if tot else None,
        "largest_oos": [(s, oos[s]) for s in top_oos],
    }  # fmt: skip


def price_at(bars: list[dict[str, Any]], t: int) -> float:
    ts = [b["ts"] for b in bars]
    i = max(0, bisect.bisect_right(ts, t - 4 * 3_600_000) - 1)
    return float(bars[i]["close"])


def h4(base: dict[str, Any], mk: dict[str, Any], uni: dict[str, Any]) -> dict[str, Any]:
    tbl = json.loads((OUT / "collateral_bybit.json").read_text())["result"]["list"]
    tiers = {r["currency"]: r["collateralRatioList"] for r in tbl}
    ladder = json.loads(LADDER.read_text())["symbols"]
    out: dict[str, Any] = {"table_coins": len(tiers), "per_trade": []}
    trades = base["trades"]
    for tr in trades:
        sym = tr["symbol"]
        spot_sym, mult = uni[sym]
        coin = spot_sym.split("/")[0]
        px = price_at(mk[sym]["spot"], tr["open_ts"]) / mult
        row = {"symbol": sym, "coin": coin, "open_ts": tr["open_ts"], "close_ts": tr["close_ts"], "runup": tr["max_runup"],
               "prem": max(0.0, tr["max_premium_rise"]), "mmr": ladder.get(sym.split("/")[0] + "USDT", {}).get("mmr")}  # fmt: skip
        for usd in (2_000, 20_000):
            h = tiered_haircut(tiers.get(coin, []), usd / px) if px > 0 else None
            row[f"h{usd}"] = None if h is None or h >= 1 - 1e-12 else h
        out["per_trade"].append(row)
    eligible = {r["coin"] for r in out["per_trade"] if r["h2000"] is not None}
    out["coins_traded"] = len({r["coin"] for r in out["per_trade"]})
    out["coins_eligible_2000"] = len(eligible)
    out["trades_eligible_2000"] = sum(1 for r in out["per_trade"] if r["h2000"] is not None)

    def k_peak(key: str | None, h_uniform: float | None, **kw: float) -> float:
        ev = []
        for r in out["per_trade"]:
            h = h_uniform if key is None else r[key]
            extra = W * (capital_factor(h, **kw) - 1.0)
            ev += [(r["open_ts"], extra), (r["close_ts"], -extra)]
        cur = peak = 0.0
        for _, x in sorted(ev, key=lambda e: (e[0], e[1])):
            cur += x
            peak = max(peak, cur)
        return 1.0 + peak

    grid = {}
    for m in (0.25, 0.5, 1.0):
        for x in (0.10, 0.25):
            kw = {"move": m, "premium": x}
            grid[f"m{m}_x{x}"] = {
                **{f"h{h}": capital_factor(h, **kw) for h in (0.0, 0.10, 0.25)},
                "official2000": k_peak("h2000", None, **kw),
                "official20000": k_peak("h20000", None, **kw),
            }
    out["k_grid"] = grid
    liq = {}
    for name, key, hu in (("h0", None, 0.0), ("h0.1", None, 0.10), ("h0.25", None, 0.25), ("official2000", "h2000", None)):
        n = n_el = 0
        for r in out["per_trade"]:
            h = hu if key is None else r[key]
            if h is None:
                continue
            n_el += 1
            mmr = r["mmr"] or 0.05
            eq = (1 - h) * (1 + r["runup"] - r["prem"]) - r["runup"]
            n += eq < mmr * (1 + r["runup"])
        liq[name] = {"eligible_trades": n_el, "liquidated_without_topup": n}
    out["liquidation_check"] = liq
    out["max_runup_trades"] = sorted(((r["symbol"], r["runup"], r["prem"]) for r in out["per_trade"]), key=lambda z: -z[1])[:5]
    return out


def run(a: argparse.Namespace) -> None:
    mk, uni = load()
    cm = models()
    res: dict[str, Any] = {"registered_card_sha256": "68ea4a515fabd6d5086faf7f25e27f81617944155fc41dda0819be4b7bf262bc"}
    # 0. reproduction (old model) and base
    old = sim(mk, cm["old"])
    log(f"old model base: {old['stats']['n_trades']} trades, APR notional {pct(old['stats']['apr_on_notional'])} (bench: 42, +6.5%)")
    res["reproduction_old"] = summary(old)
    del old
    sims: dict[str, dict[str, Any]] = {}
    base = sim(mk, cm["main"])
    sims["base"] = base
    log(f"main base: {summary(base)}")
    # 1. grid exit x rotation x rank (main costs)
    grid_rows = {}
    for rank in ("apr7", "min7_30"):
        for ex in (0.0, -0.05, -0.10):
            for gap in (None, 0.25, 0.50, 1.00):
                key = f"{rank}|exit{ex}|gap{gap}"
                r = base if key == "apr7|exit0.0|gapNone" else sim(mk, cm["main"], rank=rank, exit_apr=ex, rotation_gap=gap)
                p = paired(r, base)
                grid_rows[key] = {**summary(r), **{k: v for k, v in p.items() if k != "sym_full"}}
                if key in ("min7_30|exit0.0|gapNone", "apr7|exit-0.05|gapNone", "apr7|exit0.0|gap0.5"):
                    sims[key] = r
                    grid_rows[key]["five_a"] = five_a(r, base)
                log(
                    f"{key}: trades {r['stats']['n_trades']} rot {r['stats']['n_rotations']} OOS gain {pct(p['oos_gain'], 2)} [{pct(p['oos_lo'], 2)}; {pct(p['oos_hi'], 2)}]"
                )
    r = sim(mk, cm["main"], rank="median30")
    grid_rows["median30|exit0.0|gapNone"] = {**summary(r), **{k: v for k, v in paired(r, base).items() if k != "sym_full"}}
    res["grid"] = grid_rows
    # 2. cost sensitivity for primaries
    prim = {"H1": dict(rank="min7_30"), "H7a": dict(exit_apr=-0.05), "H7b": dict(rotation_gap=0.50)}
    cost_rows = {}
    for cname in ("stress", "a_perp"):
        b = sim(mk, cm[cname])
        for h, mod in prim.items():
            p = paired(sim(mk, cm[cname], **mod), b)
            cost_rows[f"{h}|{cname}"] = {k: v for k, v in p.items() if k not in ("sym_full",)}
        cost_rows[f"base|{cname}"] = summary(b)
        log(f"costs {cname} done")
    res["costs"] = cost_rows
    # 3. H4 capital
    h4r = h4(base, mk, uni)
    k_off = h4r["k_grid"]["m0.25_x0.1"]["official2000"]
    st = base["stats"]
    h4_rows = {}
    for name, k in (("h0", capital_factor(0.0)), ("h0.1", capital_factor(0.10)), ("h0.25", capital_factor(0.25)), ("official2000", k_off),
                    ("official20000", h4r["k_grid"]["m0.25_x0.1"]["official20000"])):  # fmt: skip
        gain_curve = [(t, v / k - v / CAP) for t, v in base["equity"]]
        wk = [v for _, v in window_weekly(gain_curve, OOS)]
        bb = block_bootstrap(wk, n_boot=N_BOOT)
        eq_k = base["equity"]
        wk_all = window_weekly(eq_k, 0)
        h4_rows[name] = {"K": k, "apr_capital": st["apr_on_notional"] / k, "oos_gain": bb["apr"], "oos_lo": bb["lo"], "oos_hi": bb["hi"], "p": bb["p"],
                         "max_dd_capital": max_drawdown(eq_k) / k, "worst_week_capital": min(v for _, v in wk_all) / k}  # fmt: skip
    h4r["rows"] = h4_rows
    res["h4"] = h4r
    log(f"H4: coins traded {h4r['coins_traded']}, eligible {h4r['coins_eligible_2000']}, K official {k_off:.3f}")
    # 4. placebo for H1
    plac = []
    for s in range(1, a.placebo + 1):
        r = sim(mk, cm["main"], rank="random", seed=s)
        d = diff_curve(r["equity"], base["equity"])
        plac.append(sum(v for _, v in window_weekly(d, OOS)) / ((st["end_ms"] - OOS) / YEAR_MS) / CAP)
        if s % 20 == 0:
            log(f"placebo {s}/{a.placebo}")
    plac.sort()
    res["placebo"] = {
        "n": len(plac),
        "p95": plac[int(0.95 * (len(plac) - 1))] if plac else None,
        "median": plac[len(plac) // 2] if plac else None,
        "values": plac,
    }
    # 5. Holm
    pv = {"H1": grid_rows["min7_30|exit0.0|gapNone"]["p"], "H7a": grid_rows["apr7|exit-0.05|gapNone"]["p"],
          "H7b": grid_rows["apr7|exit0.0|gap0.5"]["p"], "H4": h4_rows["official2000"]["p"]}  # fmt: skip
    res["holm"] = holm(pv)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str))
    report(res)
    log("done")


def report(res: dict[str, Any]) -> None:
    g = res["grid"]
    print("\n== grid (main costs), on capital (1.5 x notional); gain vs base on OOS >= 2025-03-01 ==")
    print("| variant | trades | rot | APR cap | max DD | worst wk | OOS gain | CI weeks | CI symbols | 2025 | 2026 | w/o top5 | IS gain |")
    for k, r in g.items():
        print(f"| {k} | {r['n_trades']} | {r['n_rotations']} | {pct(r['apr_capital'])} | {pct(-r['max_dd_capital'])} | {pct(r['worst_week_capital'])} | "
              f"{pct(r['oos_gain'], 2)} | [{pct(r['oos_lo'], 2)}; {pct(r['oos_hi'], 2)}] | [{pct(r['sym_lo'], 2)}; {pct(r['sym_hi'], 2)}] | "
              f"{pct(r['seg2025'], 2)} | {pct(r['seg2026'], 2)} | {pct(r['oos_wo_top5'], 2)} | {pct(r['is_gain'], 2)} |")  # fmt: skip
    print("\n== cost sensitivity (primaries) ==")
    for k, r in res["costs"].items():
        if k.startswith("base"):
            print(f"{k}: trades {r['n_trades']} APR cap {pct(r['apr_capital'])} OOS {pct(r['oos_apr_capital'])}")
        else:
            print(
                f"{k}: OOS gain {pct(r['oos_gain'], 2)} [{pct(r['oos_lo'], 2)}; {pct(r['oos_hi'], 2)}] sym [{pct(r['sym_lo'], 2)}; {pct(r['sym_hi'], 2)}]"
            )
    h = res["h4"]
    print(
        f"\n== H4: coins traded {h['coins_traded']}, in Bybit table at $2k {h['coins_eligible_2000']}, trades eligible {h['trades_eligible_2000']} =="
    )
    for k, r in h["rows"].items():
        print(
            f"{k}: K {r['K']:.3f} APR cap {pct(r['apr_capital'])} OOS gain {pct(r['oos_gain'], 2)} [{pct(r['oos_lo'], 2)}; {pct(r['oos_hi'], 2)}] maxDD {pct(-r['max_dd_capital'])} worst wk {pct(r['worst_week_capital'])}"
        )
    print("K grid:", json.dumps({k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in h["k_grid"].items()}))
    print("liquidation check:", h["liquidation_check"], "top runups:", [(s, round(u, 2), round(p, 2)) for s, u, p in h["max_runup_trades"]])
    pl = res["placebo"]
    print(f"\n== placebo H1: n {pl['n']}, median {pct(pl['median'], 2)}, p95 {pct(pl['p95'], 2)} ==")
    print("holm:", json.dumps(res["holm"]))
    for k in ("min7_30|exit0.0|gapNone", "apr7|exit-0.05|gapNone", "apr7|exit0.0|gap0.5"):
        f = g[k]["five_a"]
        print(f"5a {k}: IS nonzero {f['n_is_nonzero']}, top-quarter IS {f['top_is']} -> OOS {pct(f['top_is_oos'], 2)} vs rest {pct(f['rest_oos'], 2)}; "
              f"top5 share OOS {f['top5_share_oos']}; largest OOS {[(s, round(v * 100, 2)) for s, v in f['largest_oos']]}")  # fmt: skip


def timing() -> None:
    mk, uni = load()
    print("sample universe:", list(uni.items())[:3])
    cm = models()
    t0 = time.time()
    r = sim(mk, cm["main"])
    log(f"one main sim {time.time() - t0:.1f}s, trades {r['stats']['n_trades']}")
    t0 = time.time()
    sim(mk, cm["main"], rank="min7_30", rotation_gap=0.5)
    log(f"one H1+rotation sim {time.time() - t0:.1f}s")


def post() -> None:
    """Descriptive H4 tables from results.json (no simulation): per-coin collateral eligibility and the time share
    of the walk with k slots in coins that are not collateral (Bybit table, $2 000 slot)."""
    res = json.loads((OUT / "results.json").read_text())
    h = res["h4"]
    coins: dict[str, dict[str, Any]] = {}
    for r in h["per_trade"]:
        c = coins.setdefault(r["coin"], {"trades": 0, "h2000": r["h2000"], "h20000": r["h20000"]})
        c["trades"] += 1
    print("| coin | trades | haircut $2k | haircut $20k |")
    for c, v in sorted(coins.items(), key=lambda kv: -kv[1]["trades"]):
        f = lambda x: "not collateral" if x is None else f"{x:.0%}"  # noqa: E731
        print(f"| {c} | {v['trades']} | {f(v['h2000'])} | {f(v['h20000'])} |")
    ev = []
    for r in h["per_trade"]:
        if r["h2000"] is None:
            ev += [(r["open_ts"], 1), (r["close_ts"], -1)]
    ev.sort()
    t0 = min(r["open_ts"] for r in h["per_trade"])
    t1 = max(r["close_ts"] for r in h["per_trade"])
    share: dict[int, float] = {}
    cur, last = 0, t0
    for ts, d in ev:
        share[cur] = share.get(cur, 0.0) + (ts - last)
        cur, last = cur + d, ts
    share[cur] = share.get(cur, 0.0) + (t1 - last)
    tot = sum(share.values())
    print("time share by number of slots in non-collateral coins:", {k: round(v / tot, 3) for k, v in sorted(share.items())})
    mean_k = 1 + 0.5 * sum(k * v for k, v in share.items()) / tot / 5
    print(f"time-mean capital factor (descriptive, not a provision): {mean_k:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["time", "run", "post"])
    ap.add_argument("--placebo", type=int, default=200)
    a = ap.parse_args()
    if a.part == "time":
        timing()
    elif a.part == "post":
        post()
    else:
        run(a)


if __name__ == "__main__":
    main()
