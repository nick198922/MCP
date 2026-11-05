"""Chain-level DeFi metrics from DefiLlama payloads (pure: no I/O).

All changes are decimals (0.10 = +10 %). Daily DEX volume and fees are single-day figures and noisy: the 7d/30d
change compares the latest 24h with the one day 7/30 days ago, so `week_over_week` (sum of the last 7 days vs the
7 days before) is given as the steadier reading. Stablecoin supply (`stablecoin_total`, `stablecoin_asset`) comes from
the separate stablecoins.llama.fi payloads.
"""

from __future__ import annotations

from typing import Any

# Friendly aliases -> DefiLlama chain names (case-sensitive upstream). Unknown names pass through unchanged.
CHAIN_ALIASES = {
    "sol": "Solana",
    "eth": "Ethereum",
    "bnb": "BSC",
    "bsc": "BSC",
    "arb": "Arbitrum",
    "op": "Optimism",
    "avax": "Avalanche",
    "matic": "Polygon",
    "polygon": "Polygon",
    "base": "Base",
    "sui": "Sui",
    "ton": "TON",
    "tron": "Tron",
    "trx": "Tron",
    "hyperliquid": "Hyperliquid L1",
    "hyperevm": "Hyperliquid L1",
}

DAY = 86400


def resolve_chain(name: str) -> str:
    n = (name or "").strip()
    return CHAIN_ALIASES.get(n.lower(), n)


def _ratio(cur: float | None, ref: float | None) -> float | None:
    if cur is None or ref is None or ref <= 0:
        return None
    return round(cur / ref - 1.0, 6)


def _f(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


MAX_GAP = 2 * DAY  # reference point may be at most this much older than the target, else the change is None
STALE_AFTER = 2 * DAY  # latest TVL point older than this (vs `now`) is reported as stale


def tvl_summary(rows: list[Any], now_ts: float | None = None) -> dict[str, Any] | None:
    """`/v2/historicalChainTvl/{chain}` rows `{date: unix, tvl: usd}` -> latest TVL and 7d/30d change.

    A change is None when the series has no point within MAX_GAP before the target. With `now_ts` the result
    carries `age_days` of the latest point and `stale` (older than STALE_AFTER).
    """
    pts = sorted(
        (
            (int(r["date"]), float(r["tvl"]))
            for r in rows
            if isinstance(r, dict) and _f(r.get("date")) is not None and _f(r.get("tvl")) is not None
        ),
    )
    if not pts:
        return None
    ts, cur = pts[-1]

    def at_or_before(t: int) -> float | None:
        best = None
        for d, v in pts:
            if d <= t:
                best = (d, v)
            else:
                break
        if best is None or t - best[0] > MAX_GAP:
            return None
        return best[1]

    out: dict[str, Any] = {
        "usd": round(cur, 2),
        "as_of_ts": ts,
        "change_7d": _ratio(cur, at_or_before(ts - 7 * DAY)),
        "change_30d": _ratio(cur, at_or_before(ts - 30 * DAY)),
    }
    if now_ts is not None:
        age = max(now_ts - ts, 0.0)
        out["age_days"] = round(age / DAY, 2)
        out["stale"] = age > STALE_AFTER
    return out


def _peg_usd(x: Any) -> float | None:
    """`{"peggedUSD": n}` -> n (USD-pegged supply in USD), else None."""
    return _f(x.get("peggedUSD")) if isinstance(x, dict) else None


def stablecoin_total(chart: list[Any], now_ts: float | None = None) -> dict[str, Any] | None:
    """`/stablecoincharts/all` rows -> latest total supply (USD, all peg types summed) and 7d/30d change.

    `date` arrives as a string of unix seconds; rows without a numeric date or any numeric USD value are skipped.
    Same gap and staleness rules as `tvl_summary`.
    """
    rows = []
    for r in chart:
        if not isinstance(r, dict) or not isinstance(r.get("totalCirculatingUSD"), dict):
            continue
        try:
            ts = int(float(r.get("date")))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        vals = [v for v in (_f(x) for x in r["totalCirculatingUSD"].values()) if v is not None]
        if vals:
            rows.append({"date": ts, "tvl": sum(vals)})
    return tvl_summary(rows, now_ts)


def stablecoin_asset(assets: list[Any], symbol: str) -> dict[str, Any] | None:
    """One USD-pegged stablecoin from `/stablecoins` rows: circulating USD and changes vs DefiLlama's own snapshots
    1, 7 and 30 days ago (`circulatingPrevDay/Week/Month`). When several assets share the symbol, the largest wins."""
    sym = symbol.upper()
    cands = [
        a
        for a in assets
        if isinstance(a, dict)
        and str(a.get("symbol") or "").strip().upper() == sym
        and a.get("pegType") == "peggedUSD"
        and _peg_usd(a.get("circulating")) is not None
    ]
    if not cands:
        return None
    a = max(cands, key=lambda x: _peg_usd(x["circulating"]) or 0.0)
    cur = _peg_usd(a["circulating"])
    price = _f(a.get("price"))
    return {
        "symbol": sym,
        "defillama_id": str(a.get("id")),
        "usd": round(cur, 2) if cur is not None else None,
        "price": None if price is None else round(price, 6),
        "change_1d": _ratio(cur, _peg_usd(a.get("circulatingPrevDay"))),
        "change_7d": _ratio(cur, _peg_usd(a.get("circulatingPrevWeek"))),
        "change_30d": _ratio(cur, _peg_usd(a.get("circulatingPrevMonth"))),
    }


def overview_summary(body: dict[str, Any]) -> dict[str, Any] | None:
    """`/overview/dexs/{chain}` or `/overview/fees/{chain}` -> 24h value and changes (decimals)."""
    cur = _f(body.get("total24h"))
    if cur is None:
        return None
    w, w_prev = _f(body.get("total7d")), _f(body.get("total14dto7d"))
    return {
        "usd_24h": round(cur, 2),
        "usd_7d_sum": None if w is None else round(w, 2),
        "usd_30d_sum": None if _f(body.get("total30d")) is None else round(float(body["total30d"]), 2),
        "change_1d": _ratio(cur, _f(body.get("total48hto24h"))),
        "change_7d": _ratio(cur, _f(body.get("total7DaysAgo"))),
        "change_30d": _ratio(cur, _f(body.get("total30DaysAgo"))),
        "week_over_week": _ratio(w, w_prev),
    }
