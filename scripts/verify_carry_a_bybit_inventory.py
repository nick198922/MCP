"""Item 8q of IDEA-20261031-structural-edges-depth: Bybit inventory for carry A (counts only, no P&L).

For the units announced by Bybit delisting announcements 2024-10-17 ... 2026-10-17 (perp units: the 232 linear
USDT perps of `perp_delist`; spot units: bases of `spot_delist` with a linear perp): did the spot pair exist on Bybit
at the announcement, was the symbol "hot" by rule A (trailing 7d APR >= 0.20) before it, could A have entered after
it (no slots), which leg disappeared first; a rough 5-slot estimate with the registered Binance ratios.

Stages (in order):
  units     parse the cached announcements -> units.json
  validate  seeded random spot probes: 10 of the 232 perps and 10 spot-delisted bases -> validate.json
  fetch     per unit: spot and linear daily klines [t_a - 200 d; t_a + 60 d], funding [t_a - 188 d; t_a + 31 d]
  count     unit_inventory per unit -> inventory.json and a compact table

Network only through core.http.Http, public Bybit v5, no keys. One JSON file per request under --cache-dir.
Pre-registration: the card, section "Пункт 8q". Announcement titles and bodies are untrusted: only tickers are used.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common_helpers as cmn  # noqa: E402
import verify_news_events as nev  # noqa: E402

from tradedesk.analytics.carry_bybit_inventory import DAY_MS, active_days, has_bar_in, slot_estimate, unit_inventory  # noqa: E402
from tradedesk.analytics.news_events import MULTIPLIERS, classify_delisting, perp_base  # noqa: E402

HOME = Path.home() / ".cache" / "tradedesk-agents"
DEFAULT_CACHE = HOME / "carry_a_bybit_inventory"
NEWS = HOME / "news_events"
SEED = 20261102
LO = int(dt.datetime(2024, 10, 17, tzinfo=dt.UTC).timestamp() * 1000)
HI = int(dt.datetime(2026, 10, 18, tzinfo=dt.UTC).timestamp() * 1000)
MERGE_MS = 30 * DAY_MS
GATE_MIN = 10


def fmt(t: int | None) -> str:
    return "-" if t is None else dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- stage: units


def stage_units(cache: Path) -> None:
    rows = [r for r in nev.load_ann(NEWS) if r["source"] == "bybit" and LO <= r["ms"] < HI]
    kinds: Counter[str] = Counter()
    perps: dict[str, int] = {}
    spots: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r["ms"]):
        kind, ticks = classify_delisting("bybit", r["title"])
        if kind == "needs_body":
            p = nev.body_path(NEWS, r)
            if not p.exists():
                kinds["needs_body_missing"] += 1
                continue
            b = cmn.read_json(p)
            kind, ticks = ("perp_delist", b["perps"]) if "Perpetual" in r["title"] else ("spot_delist", b["pairs"])
        kinds[kind] += 1
        for t in ticks:
            t = str(t)
            if kind == "perp_delist" and t.endswith("USDT"):
                perps.setdefault(t, r["ms"])
            elif kind == "spot_delist":
                spots.setdefault(t.upper(), r["ms"])
    units: list[dict[str, Any]] = []
    merged: set[str] = set()
    for sym, t in perps.items():
        base, mult = perp_base(sym)
        u = {"base": base, "perp": sym, "mult": mult, "t_a": t, "type": "perp", "t_perp_ann": t, "t_spot_ann": None}
        ts = spots.get(base)
        if ts is not None and abs(ts - t) <= MERGE_MS:
            u |= {"type": "both", "t_a": min(t, ts), "t_spot_ann": ts}
            merged.add(base)
        units.append(u)
    for base, t in spots.items():
        if base not in merged:
            units.append({"base": base, "perp": None, "mult": None, "t_a": t, "type": "spot", "t_perp_ann": None, "t_spot_ann": t})
    units.sort(key=lambda u: (u["t_a"], u["base"]))
    (cache / "units.json").write_text(
        json.dumps({"kinds": dict(kinds), "n_perps": len(perps), "n_spot_bases": len(spots), "units": units}, indent=1)
    )
    c = Counter(u["type"] for u in units)
    print(f"announcements {len(rows)}; kinds {dict(kinds)}; perps named {len(perps)}; spot bases named {len(spots)}")
    print(f"units {len(units)}: {dict(c)} (spot units still need a linear perp, see fetch)")


# ---------------------------------------------------------------- network helpers


class Api:
    def __init__(self, cache: Path) -> None:
        from tradedesk.core.http import Http

        self.http = Http("bybit_inventory", "https://api.bybit.com", rate=2, burst=2, timeout=30, retries=2)
        self.dir = cache / "raw"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    async def get(self, name: str, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        p = self.dir / f"{name}.json"
        if p.exists():
            return cmn.read_json(p)
        try:
            r = await self.http.get_json(path, params=params)
            data = r.data if isinstance(r.data, dict) else {"retCode": -1}
        except Exception as e:  # provider failure: not measured, never substituted
            print(f"  ! {name}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            return None
        self.calls += 1
        out = {"retCode": data.get("retCode"), "retMsg": data.get("retMsg"), "list": (data.get("result") or {}).get("list") or []}
        p.write_text(json.dumps(out))
        return out

    async def kline_days(self, cat: str, sym: str, t_a: int) -> list[tuple[int, float, float]] | None:
        start, end = t_a - 200 * DAY_MS, t_a + 60 * DAY_MS
        r = await self.get(
            f"k_{cat}_{sym}_{t_a}",
            "/v5/market/kline",
            {"category": cat, "symbol": sym, "interval": "D", "start": start, "end": end, "limit": 1000},
        )
        if r is None:
            return None
        if r["retCode"] not in (0, 10001):
            return None
        return sorted((int(x[0]), float(x[4]), float(x[5])) for x in r["list"])

    async def funding(self, sym: str, t_a: int) -> list[tuple[int, float]] | None:
        end, lo = t_a + 31 * DAY_MS, t_a - 188 * DAY_MS
        seen: dict[int, float] = {}
        for page in range(60):
            r = await self.get(
                f"f_{sym}_{t_a}_{page}", "/v5/market/funding/history", {"category": "linear", "symbol": sym, "endTime": end, "limit": 200}
            )
            if r is None or r["retCode"] != 0:
                return None
            for x in r["list"]:
                seen[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
            if len(r["list"]) < 200:
                break
            oldest = min(int(x["fundingRateTimestamp"]) for x in r["list"])
            if oldest <= lo:
                break
            end = oldest - 1
        return sorted(seen.items())

    async def aclose(self) -> None:
        await self.http.aclose()


# ---------------------------------------------------------------- stage: validate


async def stage_validate(cache: Path) -> None:
    u = cmn.read_json(cache / "units.json")["units"]
    rng = random.Random(SEED)
    perp_units = [x for x in u if x["type"] in ("perp", "both")]
    spot_units = [x for x in u if x["type"] in ("spot", "both")]
    sa = rng.sample(perp_units, 10)
    sb = rng.sample(spot_units, 10)
    api = Api(cache)
    res: dict[str, list[dict[str, Any]]] = {"a_perps": [], "b_spot_delisted": []}
    try:
        for key, sample in (("a_perps", sa), ("b_spot_delisted", sb)):
            for x in sample:
                sym = x["base"] + "USDT"
                t_ref = x["t_spot_ann"] if key == "b_spot_delisted" else x["t_a"]
                b = await api.kline_days("spot", sym, x["t_a"])
                d = active_days(b or [])
                near = has_bar_in(d, t_ref - 60 * DAY_MS, t_ref) if b is not None else None
                res[key].append(
                    {
                        "base": x["base"],
                        "type": x["type"],
                        "t_a": fmt(x["t_a"]),
                        "rows": None if b is None else len(b),
                        "bars_60d_before": near,
                        "last": fmt(max(d) if d else None),
                    }
                )
                print(
                    f"  {key} {sym:16s} {x['type']:5s} ann {fmt(x['t_a'])} rows {res[key][-1]['rows']} bars<=ann {near} last {res[key][-1]['last']}",
                    flush=True,
                )
    finally:
        await api.aclose()
    nb = sum(1 for r in res["b_spot_delisted"] if r["bars_60d_before"])
    res["b_with_bars"] = nb
    res["spot_empty_means_absent"] = nb >= 9
    (cache / "validate.json").write_text(json.dumps(res, indent=1))
    print(f"validation (b): {nb}/10 delisted spot pairs return daily bars before the announcement -> empty = absent: {nb >= 9}")


# ---------------------------------------------------------------- stage: fetch


async def stage_fetch(cache: Path, limit: int = 0) -> None:
    units = cmn.read_json(cache / "units.json")["units"]
    if limit:
        units = random.Random(SEED + 1).sample(units, limit)
    api = Api(cache)
    t0 = dt.datetime.now(dt.UTC)
    try:
        for i, u in enumerate(units):
            t_a = u["t_a"]
            spot = await api.kline_days("spot", u["base"] + "USDT", t_a)
            if u["perp"] is None:  # spot unit: find a linear perp alive at the announcement
                for m in MULTIPLIERS:
                    sym = (str(m) if m > 1 else "") + u["base"] + "USDT"
                    pb = await api.kline_days("linear", sym, t_a)
                    if pb and has_bar_in(active_days(pb), t_a - 7 * DAY_MS, t_a):
                        u["perp"], u["mult"] = sym, m
                        break
            if u["perp"] is not None and spot and has_bar_in(active_days(spot), t_a - 7 * DAY_MS, t_a):
                await api.kline_days("linear", u["perp"], t_a)
                await api.funding(u["perp"], t_a)
            if i % 20 == 0 or i == len(units) - 1:
                el = (dt.datetime.now(dt.UTC) - t0).total_seconds()
                print(f"  {i + 1}/{len(units)} {u['base']} calls {api.calls} {el:.0f}s rss {cmn.rss_mb():.0f} MB", flush=True)
    finally:
        await api.aclose()
    if not limit:
        (cache / "units_resolved.json").write_text(json.dumps(units, indent=1))
    print("fetch done", flush=True)


# ---------------------------------------------------------------- stage: count


def stage_count(cache: Path) -> None:
    units = cmn.read_json(cache / "units_resolved.json")
    val = cmn.read_json(cache / "validate.json")
    absent_ok = bool(val["spot_empty_means_absent"])
    raw = cache / "raw"

    def kl(cat: str, sym: str, t_a: int) -> list[tuple[int, float, float]] | None:
        p = raw / f"k_{cat}_{sym}_{t_a}.json"
        if not p.exists():
            return None
        r = cmn.read_json(p)
        return None if r["retCode"] not in (0, 10001) else [(int(x[0]), float(x[4]), float(x[5])) for x in r["list"]]

    def fu(sym: str, t_a: int) -> list[tuple[int, float]] | None:
        seen: dict[int, float] = {}
        for page in range(60):
            p = raw / f"f_{sym}_{t_a}_{page}.json"
            if not p.exists():
                break
            r = cmn.read_json(p)
            if r["retCode"] != 0:
                return None
            for x in r["list"]:
                seen[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        return sorted(seen.items()) if seen else None

    out = []
    for u in units:
        t_a = u["t_a"]
        if u["perp"] is None:
            out.append(u | {"spot": "no_perp"})
            continue
        sb = kl("spot", u["base"] + "USDT", t_a)
        u = u | {"spot_rows": None if sb is None else len(sb)}
        res = unit_inventory(
            t_a,
            perp_bars=kl("linear", u["perp"], t_a) or [],
            spot_bars=sb,
            funding=fu(u["perp"], t_a),
            mult=u["mult"],
            spot_empty_means_absent=absent_ok,
            spot_by_announcement=u["type"] in ("spot", "both"),
        )
        out.append(u | res)
    (cache / "inventory.json").write_text(json.dumps(out, indent=1))

    def summary(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
        sp = Counter(r["spot"] for r in rows)
        yes = [r for r in rows if r["spot"] == "yes"]
        fund = [r for r in yes if r["hot"] is not None]
        hot = [r for r in fund if r["hot"]]
        entry = [r for r in fund if r["entry_after"] is not None]
        entry_hot = [r for r in entry if r["hot"]]
        leg = Counter(r["first_leg_gone"] for r in yes)
        iii_pool = [r for r in hot if r["first_leg_gone"] == "perp"]
        est = slot_estimate(len(entry), len(iii_pool))
        s = {"label": label, "n": len(rows), "spot": dict(sp), "funding_measured": len(fund), "hot": len(hot), "entry_after_U": len(entry),
             "entry_after_and_hot": len(entry_hot), "first_leg_gone": dict(leg), "iii_pool_hot_perp_first": len(iii_pool), "estimate": est}  # fmt: skip
        empty = sum(1 for r in rows if r["spot"] == "unmeasured" and r.get("spot_rows") == 0)
        s["unmeasured_empty_answer"] = empty
        print(f"[{label}] unmeasured with an empty spot answer (exploratory 'no spot'): {empty}")
        print(
            f"[{label}] n {len(rows)}; spot {dict(sp)}; funding measured {len(fund)}; hot {len(hot)}; U (entry after ann) {len(entry)} (of them hot {len(entry_hot)})"
        )
        print(
            f"[{label}]   first leg gone (spot=yes) {dict(leg)}; hot & perp first {len(iii_pool)}; 5-slot estimate: ban events {est['ban_events']:.2f}, removed trades {est['removed_trades']:.2f}, (iii) {est['iii_events']:.2f}"
        )
        return s

    print(f"spot source validated (empty = absent): {absent_ok}")
    for r in out:  # consistency with the 8o probe: last funding 2024-10-22 / 10-28 / 11-04
        if r.get("perp") in ("1000BEERUSDT", "KLAYUSDT", "UNFIUSDT"):
            f = fu(r["perp"], r["t_a"])
            print(f"  check {r['perp']}: spot {r['spot']}, perp_end {fmt(r.get('perp_end'))}, last funding {fmt(f[-1][0] if f else None)}")
    sums = [summary(out, "all")] + [summary([r for r in out if r["type"] == k], k) for k in ("perp", "both", "spot")]
    # Response codes behind the counts (review 8q, remarks 1 and 4): retCode 10001 is read as an empty answer.
    rc_empty: Counter[str] = Counter()
    for r in out:
        if r["spot"] == "unmeasured" and r.get("spot_rows") == 0:
            j = cmn.read_json(raw / f"k_spot_{r['base']}USDT_{r['t_a']}.json")
            rc_empty[f"{j['retCode']} {j['retMsg']!r}"] += 1
    no_perp: Counter[str] = Counter()
    for r in out:
        if r["spot"] != "no_perp":
            continue
        probes = []
        for m in MULTIPLIERS:
            p = raw / f"k_linear_{(str(m) if m > 1 else '') + r['base']}USDT_{r['t_a']}.json"
            probes.append(cmn.read_json(p) if p.exists() else None)
        if any(j is None for j in probes):
            no_perp["probe_missing"] += 1
        elif any(j["list"] for j in probes):
            no_perp["bars_not_near_t_a"] += 1
        elif all(j["retCode"] == 10001 for j in probes):
            no_perp["10001_on_all_multipliers"] += 1
        else:
            no_perp["empty_retCode_0"] += 1
    print(f"retCode of empty spot answers (units with a perp): {dict(rc_empty)}")
    print(f"'no perp' spot bases by linear probe (5 multipliers): {dict(no_perp)}")
    sums[0]["retcode_empty_spot"] = dict(rc_empty)
    sums[0]["no_perp_by_linear_probe"] = dict(no_perp)
    print("units with a possible entry after the announcement (U):")
    for r in out:
        if r.get("entry_after") is not None:
            print(f"  {r['perp']:18s} {r['type']:5s} ann {fmt(r['t_a'])} entry {fmt(r['entry_after'])} hot {r['hot']} maxAPR<ann {r['max_apr_before']:.3f} perp_end {fmt(r['perp_end'])} spot_end {fmt(r['spot_end'])} first {r['first_leg_gone']}")  # fmt: skip
    total = sums[0]
    verdict = "next_item" if total["estimate"]["ban_events"] >= GATE_MIN else "inconclusive"
    (cache / "summary.json").write_text(json.dumps({"summaries": sums, "verdict": verdict, "spot_empty_means_absent": absent_ok}, indent=1))
    print(
        f"gate: estimated ban events {total['estimate']['ban_events']:.2f} (U {total['entry_after_U']}) vs {GATE_MIN} -> {verdict}; peak rss {cmn.rss_mb():.0f} MB"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["units", "validate", "fetch", "count"])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--limit", type=int, default=0, help="fetch: seeded random sample of units (timing)")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "units":
        stage_units(a.cache_dir)
    elif a.stage == "validate":
        asyncio.run(stage_validate(a.cache_dir))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.limit))
    else:
        stage_count(a.cache_dir)


if __name__ == "__main__":
    main()
