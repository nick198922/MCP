"""Item 383a: paper carry A / B price P&L at the bar close (engine) vs at the recorded book mid, same open / close sides.

Reads the collector DB READ-ONLY (``mode=ro``): ``cp_position``, ``cp_log`` and ``cp_cycle`` through
``checkpoint_report.load_carry``. No network. Registration: ``docs/history/STANDING-IMPROVEMENTS-done.md`` (archived standing card),
«Попередня реєстрація — пункт 383a». ``--smoke`` prints only counts and consistency checks (no outcome metric).

    uv run python scripts/verify_carry_paper_mid.py [--date 2026-11-19] [--smoke]
"""

from __future__ import annotations

import argparse
import copy
import json
import resource
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import checkpoint_report as cr  # noqa: E402

from tradedesk.analytics import carry_paper_mid as cpm  # noqa: E402
from tradedesk.analytics.checkpoint import carry_forward  # noqa: E402
from tradedesk.analytics.demo_rr_threshold import mean_ci  # noqa: E402
from tradedesk.analytics.premarket_fade import bootstrap_mean_ci  # noqa: E402

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "carry_paper_mid"
DAY = 86_400_000


def _t(ms: int | None) -> str:
    return "-" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M")


def _pc(x: float | None, nd: int = 3) -> str:
    return "-" if x is None else f"{x * 100:+.{nd}f} %"


def _rss() -> str:
    return f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def v_text(strategy: str, data: dict[str, Any], bench: dict[str, Any], now_ms: int) -> str:
    row = carry_forward(strategy, data["positions"], data["logs"], data["cycles"], bench, now_ms)
    return next((f"{c['status']}: {c['text']}" for c in row["criteria"] if c["id"] == "в"), "-")


def scaled(strategy: str, data: dict[str, Any], trades: list[dict[str, Any]], unit: bool) -> dict[str, Any]:
    """Copy of the carry data with each trade's funding x funding_scale (x 1.0 when ``unit``): closed trades' ``trade.funding``,
    open trades' ``mark.funding_usd``."""
    k = {t["trade_id"]: (1.0 if unit else cpm.funding_scale(strategy, t["open"])) for t in trades}
    out = {"positions": copy.deepcopy(data["positions"]), "logs": dict(data["logs"]), "cycles": data["cycles"]}
    for p in out["positions"]:
        s = k.get(str(p["trade_id"]))
        if s is not None and isinstance(p.get("trade"), dict) and p["trade"].get("funding") is not None:
            p["trade"]["funding"] = p["trade"]["funding"] * s
    marks = []
    for r in data["logs"]["mark"]:
        s = k.get(str(r.get("trade_id")))
        f = (r.get("payload") or {}).get("funding_usd")
        marks.append(r if s is None or f is None else {**r, "payload": {**r["payload"], "funding_usd": f * s}})
    out["logs"]["mark"] = marks
    return out


def stats(xs: list[float], clusters: list[Any]) -> dict[str, Any]:
    iid = mean_ci(xs)
    cl = mean_ci(xs, clusters) if xs else {}
    bs = bootstrap_mean_ci(xs, n_boot=10_000, seed=20261030) if len(xs) >= 2 else (None, None)
    top = sorted(range(len(xs)), key=lambda i: -abs(xs[i]))
    drop = {k: statistics.fmean([x for i, x in enumerate(xs) if i not in set(top[:k])]) if len(xs) > k else None for k in (1, 2)}
    return {
        "n": len(xs), "mean": iid["mean"], "lo": iid["lo"], "hi": iid["hi"], "sd": statistics.stdev(xs) if len(xs) > 1 else None,
        "median": statistics.median(xs) if xs else None, "neg_share": sum(x < 0 for x in xs) / len(xs) if xs else None,
        "cr1": [cl.get("lo"), cl.get("hi"), cl.get("g")], "boot": list(bs), "drop_top1": drop[1], "drop_top2": drop[2],
        "verdict": cpm.verdict(len(xs), iid["mean"], iid["lo"], iid["hi"]),
    }  # fmt: skip


def run(a: argparse.Namespace) -> dict[str, Any]:
    now_ms = int(datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000) + DAY - 1
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    data = {s: cr.load_carry(con, s) for s in ("A", "B")}
    con.close()
    bench_all = json.loads(cr.BENCH.read_text(encoding="utf-8"))
    res: dict[str, Any] = {"as_of": now_ms, "strategies": {}}
    print(f"as of {_t(now_ms)} ({a.date}), {_rss()}")
    for s in ("A", "B"):
        d, bench = data[s], bench_all[s]["bench"]
        trades = cpm.select_trades(d["positions"], d["logs"]["side"], now_ms)
        closed = [t for t in trades if t["closed"]]
        rows, bad, no_book = [], [], []
        for t in closed:
            tr = t["trade"] or {}
            direction = tr.get("dir")
            r = cpm.trade_delta(s, t["open"], t["close"], direction)
            eng = tr.get("basis") if s == "A" else tr.get("price_pnl")
            if r["p_model"] is None or eng is None or abs(r["p_model"] - eng) > 1e-9:
                bad.append((t["trade_id"], r["p_model"], eng))
            if r["delta"] is None:
                no_book.append(t["trade_id"])
                continue
            lat = [x.get("book_latency_ms") for x in t["open"] + t["close"]]
            rows.append(
                {"trade_id": t["trade_id"], "symbol": t["symbol"], "opened": t["opened_ts"], "closed": t["closed_ts"],
                 "close_day": t["closed_ts"] // DAY, "dir": direction, "exit_reason": tr.get("exit_reason"),
                 "pnl": tr.get("pnl"), "funding": tr.get("funding"), "fees": tr.get("fees"), **r,
                 "latency_s": [None if x is None else x / 1000 for x in lat]}
            )  # fmt: skip
        open_d = [(t["trade_id"], cpm.open_gap_delta(s, t["open"])) for t in trades if not t["closed"]]
        v_base = v_text(s, d, bench, now_ms)
        v_unit = v_text(s, scaled(s, d, trades, unit=True), bench, now_ms)
        print(
            f"== {s}: trades {len(trades)}, closed {len(closed)}, closed with full books {len(rows)}, without {len(no_book)} {no_book}, "
            f"open {len(open_d)}; engine price P&L reproduced from model_price: {len(closed) - len(bad)} of {len(closed)}"
            + (f" MISMATCH {bad}" if bad else "")
            + f"; (v) machinery x1.0 == checkpoint: {v_base == v_unit}; {_rss()}"
        )
        out: dict[str, Any] = {"n_trades": len(trades), "n_closed": len(closed), "no_book": no_book, "mismatch": bad, "v_base": v_base}
        if not a.smoke:
            out["rows"] = rows
            out["open_entry_gap"] = open_d
            for key in ("delta", "delta_open", "delta_close", "delta_vwap"):
                xs = [r[key] for r in rows if r[key] is not None]
                out[key] = stats(xs, [r["close_day"] for r in rows if r[key] is not None])
            pnl_e = [r["pnl"] for r in rows if r["pnl"] is not None]
            pnl_m = [r["pnl"] + r["delta"] for r in rows if r["pnl"] is not None]
            out["pnl"] = {
                "engine_sum": sum(pnl_e), "mid_sum": sum(pnl_m), "engine_mean": statistics.fmean(pnl_e) if pnl_e else None,
                "mid_mean": statistics.fmean(pnl_m) if pnl_m else None, "engine_wins": sum(x > 0 for x in pnl_e),
                "mid_wins": sum(x > 0 for x in pnl_m), "sign_flips": sum((x > 0) != (y > 0) for x, y in zip(pnl_e, pnl_m, strict=True)),
            }  # fmt: skip
            out["v_mid"] = v_text(s, scaled(s, d, trades, unit=False), bench, now_ms)
        res["strategies"][s] = out
    return res


def show(res: dict[str, Any]) -> None:
    for s, o in res["strategies"].items():
        if "rows" not in o:
            continue
        print(f"\n== {s}: per trade (fractions of slot notional, %)")
        print("  symbol      opened      closed      exit    pnl_eng   p_model    p_mid     delta    d_open   d_close   d_vwap  lat_s")
        for r in o["rows"]:
            lat = [x for x in r["latency_s"] if x is not None]
            print(
                f"  {str(r['symbol']).split('/')[0]:<10} {_t(r['opened'])} {_t(r['closed'])} {str(r['exit_reason'])[:6]:<6} "
                + " ".join(f"{_pc(r[k], 2):>9}" for k in ("pnl", "p_model", "p_mid", "delta", "delta_open", "delta_close", "delta_vwap"))
                + f"  {min(lat):.0f}-{max(lat):.0f}"
            )
        for key in ("delta", "delta_open", "delta_close", "delta_vwap"):
            st = o[key]
            print(
                f"  {key:<11} n {st['n']}, mean {_pc(st['mean'])}, 95% t [{_pc(st['lo'])}; {_pc(st['hi'])}], sd {_pc(st['sd'])}, "
                f"median {_pc(st['median'])}, share<0 {st['neg_share']}, CR1 [{_pc(st['cr1'][0])}; {_pc(st['cr1'][1])}] g {st['cr1'][2]}, "
                f"boot [{_pc(st['boot'][0])}; {_pc(st['boot'][1])}], w/o top1 {_pc(st['drop_top1'])}, w/o top2 {_pc(st['drop_top2'])}, "
                f"verdict {st['verdict'] if key == 'delta' else '(descriptive)'}"
            )
        sd = o["delta"]["sd"]
        if sd:
            print(f"  n for CI within ±0.10 %: {(1.96 * sd / cpm.DELTA_MATERIAL) ** 2:.0f}")
        p = o["pnl"]
        print(
            f"  P&L (fraction of notional): engine sum {_pc(p['engine_sum'])} mean {_pc(p['engine_mean'])} wins {p['engine_wins']}; "
            f"mid sum {_pc(p['mid_sum'])} mean {_pc(p['mid_mean'])} wins {p['mid_wins']}; sign flips {p['sign_flips']}"
        )
        print(
            "  open trades, entry pair mispricing (mid vs model): "
            + ", ".join(f"{t.split(':')[1].split('/')[0]} {_pc(x, 2)}" for t, x in o["open_entry_gap"])
        )
        print(f"  (v) engine: {o['v_base']}")
        print(f"  (v) mid-normalised funding: {o['v_mid']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--smoke", action="store_true", help="counts and consistency checks only, no outcome metric")
    a = ap.parse_args()
    res = run(a)
    if a.smoke:
        return
    show(res)
    out = Path(a.cache_dir) / f"result_{a.date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(f"\nfull output: {out}; {_rss()}")


if __name__ == "__main__":
    main()
