"""Exploratory (not registered) companions to the carry B cost breakdown, report of 2026-11-25.

Costs and counters only: no funding rates, no price, no P&L. Nothing here is a verdict or changes a
rule. The collector database is opened read-only by the main breakdown script's loader.

Usage: uv run python scripts/verify_carry_b_exploratory.py
"""

import random
from collections import Counter
from datetime import UTC, datetime

import verify_carry_a_cost_breakdown as v

from tradedesk.analytics import carry_cost_breakdown as cb

AS_OF = 1795564800000
TOP2 = ("BIRB", "BLUAI")
N_BOOT = 10_000


def ratio(rows: list[dict]) -> float:
    return sum(x["actual_cost"] for x in rows) / sum(x["model_cost"] for x in rows)


def bootstrap(rows: list[dict], key: str, seed: int) -> tuple[int, float, float, float]:
    """Cluster bootstrap of the ratio: clusters, 2.5 %, 97.5 %, share of repeats above 1.5."""
    groups: dict = {}
    for x in rows:
        groups.setdefault(x[key], []).append(x)
    names = sorted(groups)
    rng = random.Random(seed)
    out = sorted(ratio([x for k in (rng.choice(names) for _ in names) for x in groups[k]]) for _ in range(N_BOOT))
    above = sum(r > 1.5 for r in out) / N_BOOT
    return len(names), round(out[250], 3), round(out[9750], 3), round(above, 3)


def stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%a %d.%m %H:%M")


def main() -> None:
    data = v.load(v.DEFAULT_DB, "B")
    recs = cb.side_records(data["sides"], as_of_ms=AS_OF)
    full = cb._full(recs)
    bybit = [x for x in recs if x["venue"] == "bybit"]

    per_trade = [b["ratio"] for b in cb.by_group(recs, lambda x: x["trade_id"]).values()]
    per_trade = sorted(r for r in per_trade if r is not None)
    print("pool ratio", round(ratio(full), 3), "full sides", len(full))
    print(
        "per-trade ratio: trades",
        len(per_trade),
        "above 1.5:",
        sum(r > 1.5 for r in per_trade),
        "median",
        round(per_trade[len(per_trade) // 2], 3),
    )
    print("bootstrap pool by trade (k, lo, hi, P):", bootstrap(full, "trade_id", 20261125))
    print("bootstrap pool by coin (k, lo, hi, P):", bootstrap(full, "coin", 20261125))
    print("bootstrap bybit by trade (k, lo, hi, P):", bootstrap(cb._full(bybit), "trade_id", 1))

    pairs: dict = {}
    for x in full:
        excess = x["actual_cost"] - x["model_cost"]
        pairs.setdefault(x["trade_id"], {}).setdefault(x["venue"], []).append(excess)
    diffs = [
        sum(p["bybit"]) / len(p["bybit"]) - sum(p["binanceusdm"]) / len(p["binanceusdm"])
        for p in pairs.values()
        if "bybit" in p and "binanceusdm" in p
    ]
    print(
        "paired trades",
        len(diffs),
        "bybit excess above binance:",
        sum(d > 0 for d in diffs),
        "mean difference bp",
        round(1e4 * sum(diffs) / len(diffs), 1),
    )

    conc = cb.concentration(bybit)
    print(
        "bybit concentration:",
        {k: conc[k] for k in ("pooled", "without_top", "eligible", "above_threshold")},
        cb.concentration_reading(conc),
    )
    rest = [x for x in recs if x["coin"] not in TOP2]
    by_venue = cb.by_group(rest, lambda x: x["venue"])
    print("without", TOP2, {k: round(b["ratio"], 3) for k, b in by_venue.items()})

    cut = cb.time_split(recs)["cut_ts"]
    for name, part in (
        ("first half", [x for x in full if x["cycle_ts"] < cut]),
        ("second half", [x for x in full if x["cycle_ts"] >= cut]),
    ):
        groups = cb.by_group(part, lambda x: x["venue"])
        print(name, {k: (b["n"], round(b["ratio"], 3)) for k, b in groups.items()})

    for venue in ("binanceusdm", "bybit"):
        curve = cb.size_curve([x for x in recs if x["venue"] == venue])
        print("size", venue, [(r["multiplier"], round(r["ratio"], 3)) for r in curve if r["ratio"]])

    opens: Counter = Counter()
    for x in cb._full(bybit):
        if x["event"] == "open":
            opens[x["coin"]] += x["actual_cost"] - x["model_cost"]
    total = sum(opens.values())
    print("bybit/open excess share:", [(c, round(e / total, 2)) for c, e in opens.most_common(4)])

    cycles = sorted((c for c in data["cycles"] if int(c["cycle_ts"]) <= AS_OF), key=_cycle_ts)
    for c in cycles:
        flags = (c.get("payload") or {}).get("flags") or []
        if flags:
            keys = sorted(f"{f.get('issue')}:{f.get('field') or f.get('symbol')}" for f in flags)
            print("flagged cycle", stamp(_cycle_ts(c)), keys)


def _cycle_ts(cycle: dict) -> int:
    return int(cycle["cycle_ts"])


if __name__ == "__main__":
    main()
