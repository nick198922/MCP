"""Timestamp (lookahead) check of CLOB ``/prices-history`` points against data-api ``/trades`` prints.

Part of IDEA-20261015-polymarket-yes-calibration-all-categories (triage condition 7). Committed replacement of the
scratch script ``scripts/_pm_ts_check.py`` that produced ``ts_check.log`` on 2026-10-15 (that file was not kept; this
version re-implements the same two tables with the pure functions ``lag_profile`` / ``jump_proximity``).

For N random markets (seed 7) with cached h = 4 points in [t0 - 30 min, t0] it fetches the trade prints in
[t0 - 35 min, t0] and prints:
  1. mean |p - last print <= t + k| for k in -120..+300 s: a late-stamped series (lookahead) fits best at k > 0;
  2. around trade-price jumps >= 2 cents, the share of points closer to the new price, by window before/after the jump.

Network only through the runtime provider (``rt.polymarket.trades``), paced; per-market JSON cache under
``<cache-dir>/ts_check/``.

Usage:
    uv run python scripts/verify_pm_ts_check.py --n 150
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_pm_yes_calibration import DEFAULT_CACHE, H_MAIN, _retry, _write, load_markets, qpath, sample_ids, t0_of  # noqa: E402

from tradedesk.analytics.pm_calibration import jump_proximity, lag_profile  # noqa: E402

KS = (-120, -60, 0, 30, 60, 120, 300)
WINDOW_S = 1800 + 300


def pick(cache: Path, n: int) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    ms, _, _, _ = load_markets(cache)
    sel = sample_ids(cache)
    pool = []
    for m in ms:
        if not m["yes_no"] or (sel is not None and m["event_id"] not in sel):
            continue
        p = qpath(cache, m["market_id"])
        if not p.exists():
            continue
        pts = json.loads(p.read_text()).get("pts", {}).get(str(H_MAIN)) or []
        if pts:
            pool.append((m, pts))
    pool.sort(key=lambda x: x[0]["market_id"])
    return random.Random(7).sample(pool, min(n, len(pool)))


async def fetch(cache: Path, picked: list[tuple[dict[str, Any], list[dict[str, Any]]]], rate: float) -> dict[str, list[dict[str, Any]]]:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    out: dict[str, list[dict[str, Any]]] = {}
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    try:
        for i, (m, _) in enumerate(picked, 1):
            p = cache / "ts_check" / f"{m['market_id']}.json"
            if p.exists():
                out[m["market_id"]] = json.loads(p.read_text())["trades"]
                continue
            t0 = t0_of(m, H_MAIN)
            f = await _retry(lambda m=m, t0=t0: rt.polymarket.trades(m["condition_id"], limit=500, start=t0 - WINDOW_S, end=t0), "trades")
            out[m["market_id"]] = list(f.data)
            _write(p, {"trades": out[m["market_id"]]})
            if i % 25 == 0:
                print(f"  fetched {i}/{len(picked)}", flush=True)
    finally:
        await rt.aclose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--rate", type=float, default=5.0)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    picked = pick(cache, a.n)
    print(f"picked {len(picked)} markets (seed 7, h = {H_MAIN} points)", flush=True)
    trades = asyncio.run(fetch(cache, picked, a.rate))
    print(f"trades {sum(len(v) for v in trades.values())}", flush=True)
    lag: dict[int, list[float]] = {k: [] for k in KS}
    jp: dict[str, list[bool]] = {}
    for m, pts in picked:
        tr = trades.get(m["market_id"]) or []
        for k, v in lag_profile(pts, tr, m["yes_token"], KS).items():
            lag[k].extend(v)
        for k, v in jump_proximity(pts, tr, m["yes_token"]).items():
            jp.setdefault(k, []).extend(v)
    print("| k, s | точок | середнє abs(p - остання угода <= t + k) |\n|---|---|---|")
    for k in KS:
        print(f"| {k:+d} | {len(lag[k])} | {statistics.mean(lag[k]) if lag[k] else float('nan'):.4f} |")
    print("\n| Точка відносно стрибка угоди (>= 2 ц) | точок | частка ближче до нової ціни |\n|---|---|---|")
    for k, v in jp.items():
        print(f"| {k} | {len(v)} | {(sum(v) / len(v) if v else float('nan')):.1%} |")


if __name__ == "__main__":
    main()
