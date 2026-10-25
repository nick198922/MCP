"""IDEA-20261025-ethena-usde-supply-funding-regime: does USDe supply growth predict lower BTC/ETH funding next week?

Stages (network only through ``core.http.Http`` / runtime providers; per-file JSON caches under --cache-dir):
  fetch   USDe history (DefiLlama stablecoins id 146, untrusted), Binance ``metrics`` OI for each Sunday,
          Binance funding / BTC daily bars for October 2026 and Bybit funding (ccxt via ``rt.exchanges``).
          Binance funding and daily bars to 2026-09 are read from the funding-crowding cache (--fc-cache).
  stage0  data checks, n, ADF / lag-1 autocorrelation, MDE of the partial correlation. Prints NO r and NO means.
  stage1  H1 (registered), robustness, H2 / H3 descriptive. Refuses to run unless stage0 passed the MDE gate.

Registration: the card's «Попередня реєстрація». H1 is a funding forecast, not a strategy (no costs); H2 net carry is
on notional, funding minus 0.3 % APR.

    uv run python scripts/verify_usde_funding.py --stage fetch --cache-dir ~/.cache/tradedesk-agents/usde_funding
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import resource
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradedesk.analytics.usde_funding import (  # noqa: E402
    DAY_MS,
    adf_tstat,
    block_bootstrap_diff,
    expanding_tercile_top,
    h1_rows,
    h1_rows_4w,
    lag1_autocorr,
    mde_partial_corr,
    monday_anchors,
    ols_residuals,
    partial_corr_nw,
    tercile_top,
    weekly_panel,
)
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402

SYMS = ("BTCUSDT", "ETHUSDT")
CCXT = {"BTCUSDT": "BTC/USDT:USDT", "ETHUSDT": "ETH/USDT:USDT"}
START = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
OCT = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
USDE_LAG_MS = 2 * DAY_MS  # registered: DefiLlama stamp <= T - 2 d
MDE_GATE = 0.25
COST_APR = 0.003


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, UTC).strftime("%Y-%m-%d")


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


# ---------------------------------------------------------------- fetch


async def fetch(cache: Path, now_ms: int) -> None:
    from tradedesk.server import build_server

    raw = cache / "raw" / "usde_146.json"
    if not raw.exists():
        ll = Http("defillama_stablecoins", "https://stablecoins.llama.fi", rate=0.5, burst=1, timeout=180, retries=2)
        try:
            f = await ll.get_json("/stablecoin/146")
            wr(raw, f.data)
        finally:
            await ll.aclose()
    print(f"usde raw {raw.stat().st_size / 1e6:.1f} MB; rss {rss_mb():.0f} MB", flush=True)
    extract_usde(cache)

    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        anchors = monday_anchors(START, now_ms - DAY_MS)
        miss = 0
        for i, t in enumerate(anchors):
            d = day(t - DAY_MS)
            for s in SYMS:
                p = cache / "metrics" / s / f"{d}.json"
                if p.exists():
                    continue
                try:
                    f = await rt.binance_vision.um_metrics_daily(s, d)
                    wr(p, f.data[-12:])
                except ProviderError as e:
                    miss += 1
                    wr(p, {"missing": e.reason})
            if i % 25 == 0:
                print(f"  metrics {i + 1}/{len(anchors)} {d} missing={miss} rss {rss_mb():.0f} MB", flush=True)
        for s in SYMS:
            p = cache / "binance_funding_oct" / f"{s}.json"
            if not p.exists():
                f = await rt.exchanges.funding_rate_history_range("binanceusdm", CCXT[s], OCT, now_ms, trust_short_page=True)
                wr(p, [[int(x["timestamp"]), float(x["fundingRate"])] for x in f.data["rows"]])
            p = cache / "bybit_funding" / f"{s}.json"
            if not p.exists():
                f = await rt.exchanges.funding_rate_history_range("bybit", CCXT[s], START, now_ms, max_pages=80)
                rows = [[int(x["timestamp"]), float(x["fundingRate"])] for x in f.data["rows"]]
                wr(p, {"rows": rows, "complete": f.data["complete"]})
            print(f"  funding {s} done rss {rss_mb():.0f} MB", flush=True)
        p = cache / "btc_k1d_oct.json"
        if not p.exists():
            f = await rt.exchanges.ohlcv("binanceusdm", "BTC/USDT:USDT", "1d", 40, since_ms=OCT)
            wr(p, [[float(v) for v in r[:6]] for r in f.data])
        print(f"FETCH COMPLETE rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.aclose()


def extract_usde(cache: Path) -> None:
    """Compact daily series from the untrusted DefiLlama payload: total circulating and the sum over chains."""
    out = cache / "usde_daily.json"
    if out.exists():
        return
    d = rd(cache / "raw" / "usde_146.json")
    print(f"  usde keys: {sorted(d)[:20]}; name={str(d.get('name'))[:40]!r}", flush=True)

    def usd(x: dict[str, Any]) -> float | None:
        c = x.get("circulating") or {}
        v = c.get("peggedUSD")
        return float(v) if isinstance(v, (int, float)) else None

    total = {int(x["date"]): usd(x) for x in d.get("tokens") or [] if usd(x) is not None}
    chains: dict[int, float] = {}
    for _, cb in (d.get("chainBalances") or {}).items():
        for x in cb.get("tokens") or []:
            v = usd(x)
            if v is not None:
                chains[int(x["date"])] = chains.get(int(x["date"]), 0.0) + v
    wr(out, {"total": sorted(total.items()), "chain_sum": sorted(chains.items()), "n_chains": len(d.get("chainBalances") or {})})
    del d


# ---------------------------------------------------------------- panel


def load_binance_funding(fc: Path, cache: Path, s: str) -> tuple[list[float], list[float], list[float]]:
    rows: list[list[float]] = []
    for p in sorted((fc / "funding" / s).glob("*.json")):
        if p.stem >= "2023-12":
            rows += rd(p)
    for t, r in rd(cache / "binance_funding_oct" / f"{s}.json"):
        if t >= OCT:
            rows.append([float(t), 8.0, r])
    rows.sort(key=lambda r: r[0])
    return [r[0] for r in rows], [r[2] for r in rows], [r[1] for r in rows]


def load_ccxt_funding(rows: list[list[float]]) -> tuple[list[float], list[float], list[float]]:
    rows = sorted(rows)
    t = [float(r[0]) for r in rows]
    hrs = [8.0] + [min((t[i] - t[i - 1]) / 3_600_000, 8.0) for i in range(1, len(t))]
    return t, [r[1] for r in rows], hrs


def load_btc_daily(fc: Path, cache: Path) -> list[list[float]]:
    rows = []
    for p in sorted((fc / "k1d" / "BTCUSDT").glob("*.json")):
        if p.stem >= "2023-12":
            rows += rd(p)
    rows += [r for r in rd(cache / "btc_k1d_oct.json") if r[0] >= OCT]
    rows.sort(key=lambda r: r[0])
    return rows


def build_panel(cache: Path, fc: Path, now_ms: int) -> dict[str, Any]:
    u = rd(cache / "usde_daily.json")
    ut = [float(t) * 1000 for t, _ in u["total"]]
    uv = [float(v) for _, v in u["total"]]
    fund = {s: load_binance_funding(fc, cache, s) for s in SYMS}
    byb = {s: load_ccxt_funding(rd(cache / "bybit_funding" / f"{s}.json")["rows"]) for s in SYMS}
    k = load_btc_daily(fc, cache)
    anchors = monday_anchors(START, now_ms - DAY_MS)
    oi: list[float | None] = []
    for t in anchors:
        tot, ok = 0.0, True
        for s in SYMS:
            m = rd(cache / "metrics" / s / f"{day(t - DAY_MS)}.json")  # last 5-min snapshot of the Sunday before T
            if isinstance(m, dict) or not m:
                ok = False
            else:
                tot += float(m[-1][2])
        oi.append(tot if ok else None)
    weeks = weekly_panel(
        anchors,
        now_ms,
        ut,
        uv,
        oi,
        (fund["BTCUSDT"], fund["ETHUSDT"]),
        (byb["BTCUSDT"], byb["ETHUSDT"]),
        [r[0] for r in k],
        [r[4] for r in k],
        [r[3] for r in k],
        usde_lag_ms=USDE_LAG_MS,
    )
    return {"weeks": weeks, "usde_t": ut, "usde_v": uv, "chain": u["chain_sum"], "total": u["total"], "fund": fund, "byb": byb}


def ctrls(r: dict[str, list[float]]) -> list[list[float]]:
    return [r[k] for k in ("c1", "c2", "c3") if k in r]


# ---------------------------------------------------------------- stage0


def stage0(cache: Path, fc: Path, now_ms: int) -> dict[str, Any]:
    p = build_panel(cache, fc, now_ms)
    w = p["weeks"]
    out: dict[str, Any] = {}
    tot = dict(p["total"])
    chs = dict(p["chain"])
    common = sorted(set(tot) & set(chs))
    rel = [abs(chs[d] - tot[d]) / tot[d] for d in common if tot[d] > 1e6]
    days = sorted(tot)
    gaps = [(day(days[i - 1] * 1000), day(days[i] * 1000)) for i in range(1, len(days)) if days[i] - days[i - 1] > 86400]
    mx = max(tot.items(), key=lambda kv: kv[1])
    out["usde"] = {
        "first": day(days[0] * 1000),
        "last": day(days[-1] * 1000),
        "n_days": len(days),
        "gaps": gaps[:10],
        "n_gaps": len(gaps),
        "chain_vs_total_max_rel": max(rel) if rel else None,
        "chain_vs_total_days_gt_1pct": sum(1 for v in rel if v > 0.01),
        "peak": [day(mx[0] * 1000), mx[1]],
        "last_value": tot[days[-1]],
    }
    print(f"USDe: {out['usde']}", flush=True)
    for s in SYMS:
        t, _, hrs = p["fund"][s]
        tb, _, hb = p["byb"][s]
        print(
            f"funding {s}: binance n={len(t)} {day(int(t[0]))}..{day(int(t[-1]))} intervals={sorted(set(hrs))}; "
            f"bybit n={len(tb)} {day(int(tb[0]))}..{day(int(tb[-1]))} complete={rd(cache / 'bybit_funding' / f'{s}.json')['complete']} "
            f"min_gap_h={min(hb[1:]):.1f}",
            flush=True,
        )
    # OI units: value / coins vs BTC close of that day (from the metrics row itself)
    chk = []
    for x in w[::13]:
        m = rd(cache / "metrics" / "BTCUSDT" / f"{day(x['t'] - DAY_MS)}.json")
        if isinstance(m, list) and m:
            chk.append((day(x["t"] - DAY_MS), round(m[-1][2] / m[-1][1]), round(m[-1][2] / 1e9, 2)))
    print(f"BTC OI price implied (value/coins) and OI $bn, every 13th Sunday: {chk}", flush=True)
    n_oi_missing = sum(1 for x in w if x["oi"] is None)
    print(f"weeks {len(w)}; OI missing {n_oi_missing}; usde None {sum(1 for x in w if x['usde'] is None)}", flush=True)

    r = h1_rows(w)
    n = len(r["y"])
    ex = ols_residuals(r["x"], ctrls(r))
    ey = ols_residuals(r["y"], ctrls(r))
    rx, ry = lag1_autocorr(ex), lag1_autocorr(ey)
    mde, n_eff = mde_partial_corr(n, 3, rx, ry)
    lvl_share = [x["usde"] / x["oi"] for x in w if x["usde"] and x["oi"]]
    lvl_f = [x["f"] for x in w if x["f"] is not None and x["usde"]]
    out["h1"] = {
        "n": n,
        "first": day(int(r["t"][0])),
        "last": day(int(r["t"][-1])),
        "adf": {"x": adf_tstat(r["x"]), "y": adf_tstat(r["y"]), "share_level": adf_tstat(lvl_share), "F_level": adf_tstat(lvl_f)},
        "ac1": {
            "x": lag1_autocorr(r["x"]),
            "y": lag1_autocorr(r["y"]),
            "share_level": lag1_autocorr(lvl_share),
            "F_level": lag1_autocorr(lvl_f),
        },
        "ac1_resid": {"x": rx, "y": ry},
        "n_eff": n_eff,
        "mde": mde,
        "gate_pass": mde <= MDE_GATE,
    }
    a = out["h1"]
    print(f"H1 rows n={n} {a['first']}..{a['last']}", flush=True)
    print("ADF t (crit -2.88): " + ", ".join(f"{k}={v:.2f}" for k, v in a["adf"].items()), flush=True)
    print("AC(1): " + ", ".join(f"{k}={v:.2f}" for k, v in a["ac1"].items()) + f"; resid x={rx:.2f} y={ry:.2f}", flush=True)
    print(f"n_eff={n_eff:.1f} MDE={mde:.3f} gate(<= {MDE_GATE}) {'PASS' if a['gate_pass'] else 'FAIL'}", flush=True)
    wr(cache / "stage0.json", out)
    return out


# ---------------------------------------------------------------- stage1


def fmt(name: str, pc: Any) -> str:
    return f"{name:<38} n={pc.n:>3} r={pc.r:+.3f} SE={pc.se:.3f} CI=[{pc.lo:+.3f},{pc.hi:+.3f}]"


def stage1(cache: Path, fc: Path, now_ms: int) -> None:
    s0 = rd(cache / "stage0.json")
    if not s0["h1"]["gate_pass"]:
        print("stage0 gate failed: refusing to compute r (inconclusive)")
        return
    p = build_panel(cache, fc, now_ms)
    w = p["weeks"]
    res: dict[str, Any] = {}
    r = h1_rows(w)
    pc = partial_corr_nw(r["y"], r["x"], ctrls(r), lag=4)
    print("== H1 (registered) ==")
    print(fmt("weekly, Binance, lag 2d", pc))
    res["h1"] = pc.__dict__ | {"contrib": None}
    half = len(r["y"]) // 2
    parts = {}
    for nm, sl in (("IS (first half)", slice(0, half)), ("OOS (second half)", slice(half, None))):
        rr = {k: v[sl] for k, v in r.items()}
        q = partial_corr_nw(rr["y"], rr["x"], ctrls(rr), lag=4)
        parts[nm] = q.__dict__ | {"contrib": None, "span": [day(int(rr["t"][0])), day(int(rr["t"][-1]))]}
        print(fmt(nm, q) + f" {parts[nm]['span']}")
    res["split"] = parts
    top = sorted(range(len(pc.contrib)), key=lambda i: -abs(pc.contrib[i]))[:3]
    keep = [i for i in range(len(r["y"])) if i not in top]
    rr = {k: [v[i] for i in keep] for k, v in r.items()}
    q = partial_corr_nw(rr["y"], rr["x"], ctrls(rr), lag=4)
    print(fmt("without top-3 weeks", q) + f" dropped {[day(int(r['t'][i])) for i in top]}")
    res["no_top3"] = q.__dict__ | {"contrib": None, "dropped": [day(int(r["t"][i])) for i in top]}
    neg = pc.r < 0
    conf = pc.r <= -0.25 and pc.hi < 0 and parts["IS (first half)"]["r"] < 0 and parts["OOS (second half)"]["r"] < 0 and q.hi < 0
    rej = pc.r > -0.10 or pc.lo <= 0 <= pc.hi or (parts["IS (first half)"]["r"] < 0) != (parts["OOS (second half)"]["r"] < 0) or not neg
    res["decision"] = "confirmed" if conf else "rejected" if rej else "inconclusive"
    print(f"DECISION (registered rule): {res['decision']}")

    print("== robustness (not the verdict) ==")
    rob = {}
    for nm, kw in (
        ("placebo x_{t-8}", {"shift": -8}),
        ("placebo x_{t+8}", {"shift": 8}),
        ("simultaneous x_{t+1} (same week as F_{t+1})", {"shift": 1}),
        ("USDe stamp <= T (no lag)", {"ukey": "usde_nolag"}),
        ("Bybit funding", {"fkey": "f_byb"}),
        ("BTC funding only", {"fkey": "f_btc"}),
        ("ETH funding only", {"fkey": "f_eth"}),
        ("x = dUSDe / USDe (not / OI)", {"norm": "usde"}),
    ):
        rr = h1_rows(w, **kw)
        q = partial_corr_nw(rr["y"], rr["x"], ctrls(rr), lag=4)
        rob[nm] = q.__dict__ | {"contrib": None}
        print(fmt(nm, q))
    r4 = h1_rows_4w(w)
    q = partial_corr_nw(r4["y"], r4["x"], ctrls(r4), lag=4)
    rob["4-week overlapping"] = q.__dict__ | {"contrib": None}
    print(fmt("4-week overlapping (NW 4)", q))
    # exploratory (added after seeing the overlapping result): non-overlapping 4-week samples, one per phase offset
    for off in range(4):
        sub = {k: v[off::4] for k, v in r4.items()}
        q = partial_corr_nw(sub["y"], sub["x"], ctrls(sub), lag=1)
        rob[f"4-week non-overlapping offset {off} (exploratory)"] = q.__dict__ | {"contrib": None}
        print(fmt(f"  4w non-overlap offset {off} (expl.)", q))
    # exploratory diagnostic: does x only proxy the funding LEVEL (mint follows high funding, funding mean-reverts)?
    q = partial_corr_nw(r4["y"], r4["x"], ctrls(r4) + [r4["lvl"]], lag=4)
    rob["4-week overlapping + F level control (exploratory)"] = q.__dict__ | {"contrib": None}
    print(fmt("4w overlapping + F-level control (expl.)", q))
    print(f"  corr(x4, F level) = {np.corrcoef(r4['x'], r4['lvl'])[0, 1]:+.3f}")
    for off in range(4):
        sub = {k: v[off::4] for k, v in r4.items()}
        q = partial_corr_nw(sub["y"], sub["x"], ctrls(sub) + [sub["lvl"]], lag=1)
        print(fmt(f"  4w non-overlap {off} + F level (expl.)", q))
    q = partial_corr_nw(r["y"], r["x"], ctrls(r) + [r["lvl"]], lag=4)
    rob["weekly + F level control (exploratory)"] = q.__dict__ | {"contrib": None}
    print(fmt("weekly + F-level control (expl.)", q))
    rq = h1_rows(w)
    q = partial_corr_nw(rq["y"], rq["x"], [], lag=4)
    rob["no controls"] = q.__dict__ | {"contrib": None}
    print(fmt("no controls", q))
    b, e = rob["BTC funding only"], rob["ETH funding only"]
    print(f"BTC vs ETH r difference {b['r'] - e['r']:+.3f} (approx SE {math.hypot(b['se'], e['se']):.3f}, ignores their correlation)")
    res["robust"] = rob

    print("== H2 (descriptive) ==")
    rows = [
        (w[i]["usde"] / w[i]["oi"], w[i + 1]["f"]) for i in range(len(w) - 1) if w[i]["usde"] and w[i]["oi"] and w[i + 1]["f"] is not None
    ]
    share = [a for a, _ in rows]
    fnext = [b - COST_APR for _, b in rows]
    top_m = tercile_top(share)
    pt, lo, hi = block_bootstrap_diff(fnext, top_m, block=8)
    print(
        f"n={len(rows)} share range {min(share):.3f}..{max(share):.3f}; top-tercile mean net carry {np.mean([f for f, m in zip(fnext, top_m, strict=True) if m]):+.4f}, rest {np.mean([f for f, m in zip(fnext, top_m, strict=True) if not m]):+.4f} APR"
    )
    print(f"full-sample terciles: top - rest = {pt:+.4f} APR, block-bootstrap 95% [{lo:+.4f},{hi:+.4f}]")
    t_rows = [day(w[i]["t"])[:4] for i in range(len(w) - 1) if w[i]["usde"] and w[i]["oi"] and w[i + 1]["f"] is not None]
    by_year = {
        y: (sum(1 for yy, m in zip(t_rows, top_m, strict=True) if yy == y and m), sum(1 for yy in t_rows if yy == y))
        for y in sorted(set(t_rows))
    }
    print(f"top-tercile weeks by year (top, all): {by_year}")
    for y in sorted(set(t_rows)):
        tops = [f for f, m, yy in zip(fnext, top_m, t_rows, strict=True) if yy == y and m]
        rest = [f for f, m, yy in zip(fnext, top_m, t_rows, strict=True) if yy == y and not m]
        if tops and rest:
            print(f"  within {y}: top {np.mean(tops):+.4f} (n={len(tops)}) rest {np.mean(rest):+.4f} (n={len(rest)}) APR")
    ex = expanding_tercile_top(share, 26)
    idx = [i for i, m in enumerate(ex) if m is not None]
    pt2, lo2, hi2 = block_bootstrap_diff([fnext[i] for i in idx], [bool(ex[i]) for i in idx], block=8)
    print(f"expanding terciles (>=26 wks, n={len(idx)}): top - rest = {pt2:+.4f} APR [{lo2:+.4f},{hi2:+.4f}]")
    res["h2"] = {"n": len(rows), "full": [pt, lo, hi], "expanding": [pt2, lo2, hi2, len(idx)]}

    print("== H3 (descriptive) ==")
    ev, base = [], []
    for i, x in enumerate(w):
        if x["dd7"] is None or i < 2 or not w[i - 2]["usde"] or not x["usde"]:
            continue
        hit = x["dd7"] <= -0.08
        base.append(hit)
        if x["usde"] / w[i - 2]["usde"] - 1 <= -0.10:
            ev.append((day(x["t"]), round(x["usde"] / w[i - 2]["usde"] - 1, 3), round(x["dd7"], 3), hit))
    pb = float(np.mean(base))
    pe = float(np.mean([e[3] for e in ev])) if ev else math.nan
    print(f"base weeks n={len(base)} P(dd7<=-8%)={pb:.3f}; event weeks n={len(ev)} P={pe:.3f}; ratio={pe / pb if pb else math.nan:.2f}")
    for e in ev:
        print(f"  event {e}")
    res["h3"] = {"n_base": len(base), "p_base": pb, "n_ev": len(ev), "p_ev": pe, "events": ev}
    wr(cache / "stage1.json", res)
    print(f"peak rss {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("fetch", "stage0", "stage1"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/usde_funding")
    ap.add_argument("--fc-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--now", default="2026-10-25", help="UTC date treated as 'now' (data after it are ignored)")
    a = ap.parse_args()
    now_ms = int(datetime.fromisoformat(a.now).replace(tzinfo=UTC).timestamp() * 1000)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(fetch(a.cache_dir, now_ms))
    elif a.stage == "stage0":
        stage0(a.cache_dir, a.fc_cache, now_ms)
    else:
        stage1(a.cache_dir, a.fc_cache, now_ms)


if __name__ == "__main__":
    main()
