"""Does a vault leader's position sizing (adds on dips, trims on rallies, leverage changes) add anything?

Idea I1 of ``docs/library/LIB-20261023-cryptobred-vaults.md``. The leader's daily gross leverage path L_t
(gross notional / equity, rebuilt by ``vault_profile.py``) is applied to a fixed daily return series and compared
with mechanical sizing of the same average leverage on the same returns:

* constant leverage, re-levered daily / weekly / every 30 days;
* buy-and-hold from day 0 (leverage drifts with the market, no decisions);
* the same leverage path circularly shifted by k days (k = 1 … n−1): same levels and persistence, timing broken.

Return series (each tested separately):
* ``alts`` — equal-weight ``ALT_BASKET`` open→close (isolates sizing from coin selection);
* ``own``  — his book's daily PnL / gross notional at the open (his own coin mix, so only the sizing is swapped).

Main variant uses L_{t−1} (known before day t opens); ``same-day`` L_t is reported for reference only, because the
day's average size already reacts to that day's price. Pre-registered rule (written before the first run):
sizing counts as skill if the actual path's Calmar (total return / max drawdown) is at or above the 90th
percentile of the circular shifts on BOTH return series, and its max drawdown is below the constant-leverage one.

Usage:
    uv run python scripts/research/vault_sizing.py <vault_address> --data-dir DIR
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_profile import (  # noqa: E402
    ALT_BASKET,
    DAY_MS,
    anchored_equity,
    daily_book,
    daily_equity,
    daily_flows,
    daily_positions,
    day_returns,
    ledger_flows,
    load_candles,
    load_data,
    max_drawdown,
    series,
    trim_after_close,
    twr_curve,
    ymd,
)

# ------------------------------------------------------------------------------------------------ pure analytics


def nav_path(lev: np.ndarray, ret: np.ndarray) -> np.ndarray:
    """NAV (start 1.0) when each day's leverage ``lev[t]`` is applied to ``ret[t]``; floored at 0 (liquidated)."""
    out, nav = np.empty(len(ret)), 1.0
    for t, (k, r) in enumerate(zip(lev, ret, strict=True)):
        nav = max(0.0, nav * (1 + k * r)) if nav > 0 else 0.0
        out[t] = nav
    return out


def rebalanced_nav(level: float, ret: np.ndarray, every: int) -> np.ndarray:
    """Constant target leverage, reset to ``level`` every ``every`` days; leverage drifts in between."""
    out, nav, k = np.empty(len(ret)), 1.0, level
    for t, r in enumerate(ret):
        if t % every == 0:
            k = level
        if nav <= 0:
            out[t] = 0.0
            continue
        new = nav * (1 + k * r)
        # exposure grows with the basket, equity with exposure × return: k_new = k (1 + r) / (1 + k r)
        k = k * (1 + r) / (1 + k * r) if 1 + k * r > 0 else 0.0
        nav = max(0.0, new)
        out[t] = nav
    return out


def stats(nav: np.ndarray) -> dict[str, float]:
    navs = [1.0, *nav.tolist()]
    dd = max_drawdown(navs)[0]
    total = navs[-1] - 1
    return {"ret": total, "dd": dd, "calmar": total / dd if dd > 0 else float("nan")}


def shift_percentile(lev: np.ndarray, ret: np.ndarray, actual: float, key: str) -> tuple[float, list[float]]:
    """Share of circular shifts of ``lev`` whose ``key`` statistic is below the actual one (mid-rank, 0…1)."""
    vals = [stats(nav_path(np.roll(lev, k), ret))[key] for k in range(1, len(lev))]
    vals = [v for v in vals if not math.isnan(v)]
    below = sum(v < actual for v in vals) + 0.5 * sum(v == actual for v in vals)
    return below / len(vals), vals


# ------------------------------------------------------------------------------------------------------- report


def build(vault: str, data_dir: Path, anchored: bool = True) -> dict[str, np.ndarray]:
    data = load_data(vault, data_dir)
    det = data["details"]
    av, pnl = trim_after_close(series(det, "accountValueHistory"), series(det, "pnlHistory"))
    pos = daily_positions(data["funding"])
    coins = set(pos) | {"BTC"} | set(ALT_BASKET)
    candles = load_candles(coins, av[0][0], av[-1][0] + 31 * DAY_MS, data_dir)
    book = daily_book(pos, candles)
    days = list(book)
    if anchored:
        equity = anchored_equity(av, ledger_flows(data["ledger"]), {d: b["pnl"] for d, b in book.items()}, days)
    else:
        equity = daily_equity(daily_flows(data["ledger"]), pnl, days)
    # Gross notional at the day's OPEN: the book's MTM PnL is avg size × (close − open), so PnL / gross_open is the
    # book's own return and gross_open / equity × that return reproduces his day exactly. Valuing at the close
    # (``daily_book``'s ``gross``) would inflate leverage on up days and deflate the return.
    gross_open: dict[int, float] = {}
    for coin, by_day in pos.items():
        cs = candles.get(coin) or {}
        for day, (szi, _expo, _f) in by_day.items():
            if day in cs:
                gross_open[day] = gross_open.get(day, 0.0) + abs(szi) * cs[day][0]
    rows = []
    for day in days:
        b, eq, g = book[day], equity.get(day, 0.0), gross_open.get(day, 0.0)
        alt = day_returns(candles, ALT_BASKET, day)
        if eq <= 0 or g <= 0 or math.isnan(alt) or b["unpriced"]:
            continue
        rows.append((day, g / eq, alt, b["pnl"] / g, b["pnl"] / eq))
    a = np.array(rows)
    official = twr_curve(av, pnl, ledger_flows(data["ledger"]))
    off_dd = max_drawdown([v for _, v in official])[0]
    return {
        "day": a[:, 0],
        "lev": a[:, 1],
        "alts": a[:, 2],
        "own": a[:, 3],
        "his": a[:, 4],
        "official": np.array([official[-1][1] - 1, off_dd]),
    }


def report(d: dict[str, np.ndarray]) -> None:
    lev, n = d["lev"], len(d["lev"])
    gaps = int(np.sum(np.diff(d["day"]) > DAY_MS))
    print(f"# Sizing test (I1)  {ymd(d['day'][0])} … {ymd(d['day'][-1])}, n = {n} days, gaps in the day series: {gaps}")
    print(
        f"  gross leverage L_t: mean {lev.mean():.2f}x, median {np.median(lev):.2f}x, p10 {np.quantile(lev, 0.1):.2f}x, p90 {np.quantile(lev, 0.9):.2f}x"
    )
    off = d["official"]
    print(f"  check: his rebuilt daily PnL / equity compounded {fmt(stats(nav_path(np.ones(n), d['his'])))}")
    print(f"         official weekly TWR                         return {off[0]:+7.1%}, max DD {off[1]:5.1%}")
    lagged = np.concatenate([[lev[0]], lev[:-1]])
    level = float(lagged.mean())
    for name in ("alts", "own"):
        r = d[name]
        print(f"\n## Return series: {name}  (basket alone 1x: {fmt(stats(nav_path(np.ones(n), r)))})")
        print(f"  corr(L_(t-1), r_t) {np.corrcoef(lagged, r)[0, 1]:+.3f}; corr(ΔL over 7 d, r over the same 7 d) {corr7(lev, r):+.3f}")
        act = stats(nav_path(lagged, r))
        same = stats(nav_path(lev, r))
        print(f"  his path, L_(t-1) [main]        {fmt(act)}")
        print(f"  his path, L_t same day [ref]    {fmt(same)}")
        for every, label in ((1, "daily"), (7, "weekly"), (30, "30-day")):
            print(f"  constant {level:.2f}x, re-lever {label:7s} {fmt(stats(rebalanced_nav(level, r, every)))}")
        print(f"  buy & hold from day 0 at {level:.2f}x  {fmt(stats(rebalanced_nav(level, r, 10**9)))}")
        for key in ("calmar", "dd", "ret"):
            pct, vals = shift_percentile(lagged, r, act[key], key)
            q = np.quantile(vals, [0.1, 0.5, 0.9])
            print(
                f"  circular shifts ({len(vals)}): {key:6s} actual {act[key]:.2f}, shifts p10/p50/p90 {q[0]:.2f} / {q[1]:.2f} / {q[2]:.2f}, actual at {pct:.0%}"
            )


def corr7(lev: np.ndarray, r: np.ndarray) -> float:
    dl = lev[7:] - lev[:-7]
    rr = np.array([np.prod(1 + r[i - 6 : i + 1]) - 1 for i in range(7, len(r))])
    return float(np.corrcoef(dl, rr)[0, 1])


def fmt(s: dict[str, float]) -> str:
    return f"return {s['ret']:+7.1%}, max DD {s['dd']:5.1%}, calmar {s['calmar']:5.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vault")
    ap.add_argument("--data-dir", type=Path, required=True, help="cache directory filled by vault_profile.py")
    ap.add_argument("--interp", action="store_true", help="old equity denominator (interpolated official PnL; look-ahead)")
    args = ap.parse_args(argv)
    report(build(args.vault.lower(), args.data_dir, anchored=not args.interp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
