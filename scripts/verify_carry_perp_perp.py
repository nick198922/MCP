"""Cross-venue funding carry Binance <-> Bybit: long the perp where funding is lower, short where it is higher.

Pre-registered before running:
  primary = 5 slots, trailing 7d funding spread >= 10 % APR to enter, <= 0 % to exit, taker fees
  (Binance 0.05 %, Bybit 0.055 %) plus 0.05 % slippage per leg per side.
  H1: mean APR on notional > 0 after costs.
  Practical bar: >= 10 % APR on notional (= on capital with 2x margin on each leg).
Universe: crypto USDT perps listed on both venues today (delisted ones are missing).

Usage:
    uv run python scripts/verify_carry_perp_perp.py --cache-dir <dir> --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from verify_carry_alts import paced
from verify_funding_carry import funding_path
from verify_lectures import TOP20, cache_path, fetch_history

from tradedesk.analytics.carry import DAY_MS, YEAR_MS, _Leg, simulate_perp_spread_portfolio
from tradedesk.server import build_server

A, B = "binanceusdm", "bybit"
PAGE = {A: 1000, B: 200}


def venue_funding_path(cache: Path, venue: str, sym: str, days: int) -> Path:
    return (
        funding_path(cache, sym, days) if venue == A else cache / f"{venue}_{sym.replace('/', '-').replace(':', '_')}_funding_{days}d.json"
    )


async def fetch_funding_venue(rt: Any, venue: str, sym: str, days: int) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * DAY_MS
    rows: dict[int, float] = {}
    # ccxt sizes Bybit's request window from the symbol's *current* funding interval, so a short page
    # does not mean the history ended; walk to now and hop over empty windows (later listings).
    while since < now_ms:
        fetched = await rt.exchanges.funding_rate_history(venue, sym, since, PAGE[venue])
        batch = [x for x in (fetched.data or []) if x.get("timestamp") is not None and x.get("fundingRate") is not None]
        if not batch:
            since += 30 * DAY_MS
            continue
        for x in batch:
            rows[int(x["timestamp"])] = float(x["fundingRate"])
        nxt = max(int(x["timestamp"]) for x in batch) + 1
        if nxt <= since:
            break
        since = nxt
    return [{"ts_ms": t, "rate": r} for t, r in sorted(rows.items())]


async def universe(rt: Any) -> list[str]:
    a = (await rt.exchanges.markets(A)).data or {}
    b = (await rt.exchanges.markets(B)).data or {}
    out = []
    for sym, m in a.items():
        info = (m or {}).get("info") or {}
        if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL" or info.get("underlyingType") != "COIN":
            continue
        if (m or {}).get("active") is False:
            continue
        mb = b.get(sym)
        if mb and mb.get("swap") and mb.get("active") is not False:
            out.append(sym)
    return sorted(out)


def max_spread(fa: list[dict[str, Any]], fb: list[dict[str, Any]], lookback_days: float = 7.0) -> float:
    la = _Leg({"funding": fa, "bars": []}, 4 * 3_600_000)
    lb = _Leg({"funding": fb, "bars": []}, 4 * 3_600_000)
    if not la.f_ts or not lb.f_ts:
        return 0.0
    lk = int(lookback_days * DAY_MS)
    t, end, best = max(la.f_ts[0], lb.f_ts[0]) + lk, min(la.f_ts[-1], lb.f_ts[-1]), 0.0
    while t <= end:
        ra, rb = la.apr(t, lk), lb.apr(t, lk)
        if ra is not None and rb is not None:
            best = max(best, abs(rb - ra))
        t += 8 * 3_600_000
    return best


async def _save(go: Any, path: Path, pause: float) -> None:
    if await asyncio.to_thread(path.exists):
        return
    data = await paced(go, pause=pause)
    if data is not None:
        await asyncio.to_thread(path.write_text, json.dumps(data))


async def fetch(cache: Path, days: int, min_spread: float, workers: int = 6) -> tuple[list[str], list[str]]:
    _, rt = build_server()
    try:
        uni = await universe(rt)
        print(f"universe: {len(uni)} crypto perps on both venues", flush=True)
        done = 0

        async def binance_all() -> None:
            # Binance funding history is limited to ~500 requests per 5 minutes: keep it sequential and paced.
            for sym in uni:
                await _save(lambda s=sym: fetch_funding_venue(rt, A, s, days), venue_funding_path(cache, A, sym, days), 1.9)

        sem = asyncio.Semaphore(workers)

        async def bybit_one(sym: str) -> None:
            nonlocal done
            async with sem:
                await _save(lambda: fetch_funding_venue(rt, B, sym, days), venue_funding_path(cache, B, sym, days), 0.05)
            done += 1
            if done % 25 == 0:
                print(f"  bybit funding {done}/{len(uni)}", flush=True)

        await asyncio.gather(binance_all(), *[bybit_one(s) for s in uni])
        hot = []
        for sym in uni:
            pa, pb = venue_funding_path(cache, A, sym, days), venue_funding_path(cache, B, sym, days)
            if pa.exists() and pb.exists():
                fa = json.loads(await asyncio.to_thread(pa.read_text))
                fb = json.loads(await asyncio.to_thread(pb.read_text))
                if max_spread(fa, fb) >= min_spread:
                    hot.append(sym)
        print(f"pairs whose trailing 7d spread ever reached {min_spread:.0%}: {len(hot)}", flush=True)
        sems = {A: asyncio.Semaphore(3), B: asyncio.Semaphore(workers)}
        done = 0

        async def prices_one(sym: str, venue: str, page: int) -> None:
            nonlocal done
            async with sems[venue]:
                await _save(lambda: fetch_history(rt, venue, sym, "4h", days, page=page), cache_path(cache, venue, sym, "4h", days), 0.1)
            done += 1
            if done % 50 == 0:
                print(f"  prices {done}/{2 * len(hot)}", flush=True)

        await asyncio.gather(*[prices_one(s, v, pg) for s in hot for v, pg in ((A, 1500), (B, 1000))])
        return uni, hot
    finally:
        await rt.aclose()


def load(cache: Path, days: int, syms: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for sym in syms:
        paths = [venue_funding_path(cache, A, sym, days), venue_funding_path(cache, B, sym, days)]
        paths += [cache_path(cache, A, sym, "4h", days), cache_path(cache, B, sym, "4h", days)]
        if not all(p.exists() for p in paths):
            continue
        fa, fb, ba, bb = (json.loads(p.read_text()) for p in paths)
        if not (fa and fb and ba and bb):
            continue
        pb = {x["ts"]: x["close"] for x in bb}
        ratios = [x["close"] / pb[x["ts"]] for x in ba if pb.get(x["ts"])]
        if len(ratios) < 20 or abs(statistics.median(ratios) - 1.0) > 0.005:
            MISMATCHED.append((sym.split("/")[0], statistics.median(ratios) if ratios else None))
            continue
        out[sym] = {"a": {"funding": fa, "bars": ba}, "b": {"funding": fb, "bars": bb}}
    return out


MISMATCHED: list[tuple[str, float | None]] = []


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


HDR = (
    "| Варіант | APR на номінал | макс. DD | угод | сер. днів | зайнятість слотів | funding / рік | ціна / рік | витрати / рік | угод у мінусі |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def row(label: str, res: dict[str, Any]) -> str:
    st, tr = res["stats"], res["trades"]
    if not st:
        return f"| {label} | — |"
    hold = statistics.mean((t["close_ts"] - t["open_ts"]) / DAY_MS for t in tr) if tr else 0.0
    neg = sum(1 for t in tr if t["pnl"] < 0) / len(tr) if tr else 0.0
    y = st["years"]
    return (
        f"| {label} | {st['apr_on_notional'] * 100:+.1f}% | {st['max_drawdown'] * 100:.1f}% | {st['n_trades']} | {hold:.1f} | "
        f"{st['slot_utilisation']:.0%} | {st['funding'] / y * 100:+.1f}% | {st['price_pnl'] / y * 100:+.1f}% | "
        f"{-st['costs'] / y * 100:+.1f}% | {neg:.0%} |"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    uni, hot = asyncio.run(fetch(cache, a.days, min_spread=0.10))
    mk = load(cache, a.days, hot)
    majors = {s: d for s, d in mk.items() if s.split("/")[0] in TOP20}
    base = dict(slots=5, lookback_days=7, entry_apr=0.10, exit_apr=0.0, fee_a=0.0005, fee_b=0.00055, slippage=0.0005, max_entry_gap=0.02)
    primary = simulate_perp_spread_portfolio(mk, **base)

    fmt = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    L = ["# Крос-біржовий funding carry Binance ↔ Bybit (лонг перп там, де funding нижчий, шорт — де вищий)\n"]
    L.append(
        f"Всесвіт: {len(uni)} крипто-перпів на обох біржах; пар, де trailing 7d спред хоч раз сягав 10%: {len(hot)}; "
        f"з повними даними: {len(mk)} (мейджорів: {len(majors)}).\n"
    )
    if MISMATCHED:
        L.append(
            "Виключено як різні інструменти під одним тікером (медіана ціни Binance/Bybit ≠ 1 більш ніж на 0.5%): "
            + ", ".join(f"{n} ({r:.4f})" if r is not None else n for n, r in MISMATCHED)
            + ". Вхід у пару пропускається, якщо ціни перпів розходяться більш ніж на 2%.\n"
        )
    L.append(
        "APR — на номінал портфеля (номінал слота = 1/K). Кожна нога тримає маржу на своїй біржі, тож капітал ≈ "
        "2 × номінал / плече: при 2x на ногу капітал = номіналу, при 1x — удвічі більший.\n"
    )
    L.append("## Основний варіант (зафіксований до запуску)\n")
    L.append(HDR)
    L.append(row("**5 слотів, спред ≥10% / ≤0%, слипедж 0.05%, розрив цін ≤2%**", primary))
    L.append(row("лише мейджори, те саме правило", simulate_perp_spread_portfolio(majors, **base)))

    L.append("\n## Чутливість\n")
    L.append(HDR)
    for entry in (0.05, 0.10, 0.20, 0.30, 0.50):
        for slots in (3, 5, 10):
            L.append(
                row(f"спред ≥{entry:.0%}, {slots} слотів", simulate_perp_spread_portfolio(mk, **dict(base, entry_apr=entry, slots=slots)))
            )
    for slip in (0.0, 0.002, 0.005):
        L.append(row(f"основний, слипедж {slip * 100:.2f}%/нога/сторона", simulate_perp_spread_portfolio(mk, **dict(base, slippage=slip))))
    L.append(row("основний, вихід ≤5%", simulate_perp_spread_portfolio(mk, **dict(base, exit_apr=0.05))))
    L.append(row("основний, вікно 3д", simulate_perp_spread_portfolio(mk, **dict(base, lookback_days=3))))
    L.append(row("основний без обмеження розриву цін", simulate_perp_spread_portfolio(mk, **dict(base, max_entry_gap=None))))
    L.append("\n## Реалістичність: закриття, коли нога пройшла поріг ліквідації (штраф 1% номіналу)\n")
    L.append(
        HDR.replace("| угод у мінусі |", "| угод у мінусі | закрито стопом |").replace(
            "|---|---|---|---|---|---|---|---|---|---|", "|---|---|---|---|---|---|---|---|---|---|---|"
        )
    )
    for lev, move in ((1, 1.0), (2, 0.5), (3, 1 / 3)):
        res = simulate_perp_spread_portfolio(mk, **dict(base, stop_move=move))
        L.append(row(f"основний, плече {lev}x на ногу (стоп при {move:.0%} проти)", res) + f" {res['stats']['n_stopped']} |")

    real = simulate_perp_spread_portfolio(mk, **dict(base, stop_move=0.5))
    contrib: dict[str, float] = {}
    for t in real["trades"]:
        contrib[t["symbol"]] = contrib.get(t["symbol"], 0.0) + t["pnl"]
    top5 = [s_ for s_, _ in sorted(contrib.items(), key=lambda x: -x[1])[:5]]
    no_top = simulate_perp_spread_portfolio({k: v for k, v in mk.items() if k not in top5}, **dict(base, stop_move=0.5))
    capped = sum(min(t["pnl"], 0.10) for t in real["trades"]) / base["slots"] / real["stats"]["years"]
    med = statistics.median(t["pnl"] for t in real["trades"])
    L.append("\n## Стійкість (варіант 2x зі стопом)\n")
    L.append(HDR)
    L.append(row("2x зі стопом (база)", real))
    L.append(row(f"без топ-5 символів ({', '.join(x.split('/')[0] for x in top5)})", no_top))
    L.append(
        f"\nЯкщо обрізати прибуток кожної угоди до +10% на слот: APR {capped * 100:+.1f}% на номінал. Медіанна угода: {med:+.2%} на слот.\n"
    )

    tr = primary["trades"]
    w = 1 / base["slots"]
    L.append("\n## Ризик основного варіанта\n")
    if tr:
        up, dn, gap = [t["max_up"] for t in tr], [t["max_down"] for t in tr], [t["max_gap"] for t in tr]
        L.append(f"- Ріст ціни проти шорт-ноги за угоду: медіана {q(up, 0.5):.0%}, p90 {q(up, 0.9):.0%}, максимум {max(up):.0%}.")
        L.append(f"- Падіння проти лонг-ноги: медіана {q(dn, 0.5):.0%}, p90 {q(dn, 0.9):.0%}, максимум {max(dn):.0%}.")
        both = [max(u, d) for u, d in zip(up, dn, strict=True)]
        L.append(
            f"- Частка угод, де одна з ніг пройшла ≥33% проти себе (ліквідація ізольованої ноги на 3x без поповнення): "
            f"{sum(1 for x in both if x >= 0.33) / len(both):.0%}; ≥50% (2x): {sum(1 for x in both if x >= 0.5) / len(both):.0%}."
        )
        L.append(
            f"- Розбіжність цін перпів між біржами (зміна A/B від входу): медіана {q(gap, 0.5):.2%}, p99 {q(gap, 0.99):.2%}, максимум {max(gap):.2%}."
        )
        L.append(f"- Найгірша угода {min(t['pnl'] for t in tr):+.2%} на слот, найкраща {max(t['pnl'] for t in tr):+.2%}.")
        L.append(f"- Шорт на Bybit / шорт на Binance: {sum(1 for t in tr if t['dir'] == 1)} / {sum(1 for t in tr if t['dir'] == -1)} угод.")
        by: dict[str, float] = {}
        for t in tr:
            by[t["symbol"]] = by.get(t["symbol"], 0.0) + w * t["pnl"]
        top = sorted(((s.split("/")[0], v) for s, v in by.items()), key=lambda x: -x[1])
        L.append("- Найбільший внесок (на номінал): " + ", ".join(f"{n} {v:+.2%}" for n, v in top[:8]) + ".")
        L.append("- Найбільший мінус: " + ", ".join(f"{n} {v:+.2%}" for n, v in top[-5:]) + ".")
        tot = sum(v for _, v in top)
        L.append(f"- Різних символів: {len(by)}; частка P&L від топ-5: {sum(v for _, v in top[:5]) / tot:.0%}." if tot > 0 else "")

    L.append("\n## Стабільність у часі\n")
    L.append("| Період | APR на номінал | угод |\n|---|---|---|")
    if primary["equity"]:
        start, end = primary["equity"][0][0], primary["equity"][-1][0]
        mid = (start + end) // 2
        for label, lo, hi in (("перша половина", start, mid), ("друга половина", mid, end)):
            part = [t for t in tr if lo <= t["open_ts"] < hi]
            L.append(
                f"| {label} ({fmt(lo)} … {fmt(hi)}) | {sum(w * t['pnl'] for t in part) / ((hi - lo) / YEAR_MS) * 100:+.1f}% | {len(part)} |"
            )

    report = "\n".join(x for x in L if x is not None)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
