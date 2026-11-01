"""Weekly DESCRIPTIVE report and the one-time verdict of the Polymarket <-> Deribit paper test.

Docs: docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md, section "Реєстрація". Pure functions over
``pm_gap_snapshot`` rows and ``pm_gap_outcome`` rows (dicts with the tables' column names); no I/O, every time is a
unix-seconds parameter. The rules and metrics themselves live in :mod:`.pm_gap_paper` and are used unchanged here.

What the weekly report may show is fixed by registration section 1: counts of snapshots and positions, resolution days
and weeks, shares of the closing-line sources (close / late / missing) and of ``missing`` at +24 h, SD per position, the
share of snapshots dropped for quote skew and of rows without read times. It must NOT show the mean of the main metric,
its CI, the P&L or the Brier comparison before the verdict: :func:`weekly_descriptive` never computes them, and
:func:`final_metrics` refuses to run before :func:`verdict_ts`.

The labelled variant v2-g (forward x (1 + g), see :mod:`.pm_gap_paper`) is reported beside v1 under the SAME gate: its
positions largely coincide with v1's, so its CLV, P&L or Brier would reveal v1's. :func:`weekly_v2g` shows counts, the
entry edge (known at entry, no outcome in it) and SDs; :func:`final_metrics_v2g` (refused before :func:`verdict_ts`)
adds CLV, P&L, Brier and the v1 positions whose edge held only thanks to the g shift, on entries before
:data:`T0_V2G_TS` only. Nothing of v1 changes.

v2-g's own forward test (docs/ideas/IDEA-20261101-polymarket-v2g-forward.md) has its own T0 (:data:`T0_V2G_TS`),
main period and one-time verdict (:func:`final_metrics_v2g_own`, gate :func:`verdict_ts_v2g`); the weekly v2-g block
switches to that period from its T0 (:func:`weekly_v2g` with ``v1_t0``).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .pm_gap_paper import (
    G_STATUSES,
    HORIZON_TOL_S,
    HOUR_S,
    MODEL_V2G,
    PRIMARY_H,
    SECONDARY_H,
    VOL_HAIRCUT_K,
    WORSE_ENTRY,
    EntryRule,
    accepted_rule_hashes,
    asset_permutation_p,
    binance_gap,
    brier_by_market,
    closing_value,
    cluster_mean_ci,
    delayed_entry,
    eligible,
    g_split,
    horizon_clv,
    open_positions,
    position_metrics,
    power_stage0,
    verdict,
    with_snapshot_g,
    worse_entry,
)
from .pm_threshold import _json_list, outcome_yes

DAY_S = 86_400
MAIN_PERIOD_DAYS = 112  # registration section 1: positions opened in [T0, T0 + 112 days) form the main test
VERDICT_DAYS = 126  # the verdict is taken once, at T0 + 126 days (last main-period market resolved by then)
T0_TS = 1792520435  # registered T0: the first pm_gap_snapshot, 2026-10-20 18:20:35 UTC (card and HANDOFF)
# Registered T0 of the variant v2-g's own forward test (docs/ideas/IDEA-20261101-polymarket-v2g-forward.md, section 1):
# the first pm_gap_snapshot with ts >= the merge of the registration PR #542 into main (2026-11-01 03:39:00 UTC, the
# merge commit's committer date = 1793504340; read from the agents' clone, GitHub's mergedAt may correct it).
T0_V2G_TS = 1793506831  # 2026-11-01 04:20:31 UTC: min(ts) >= 1793504340, read-only from the collector DB on 2026-11-01
STAGE0_DAYS = 28  # v2-g card, "Прогрес": stage 0 (power, descriptive) from 4 weeks after T0 v2-g
POWER_KEYS = ("n", "n_clusters", "sd_position", "sd_cluster", "per_cluster", "mde_now", "mde_projected", "projected_clusters")
TOP_WEEKS = 3  # registration section 12: "without the 3 weeks of the largest contribution"
SENS_THRESHOLDS = (0.03, 0.08)  # registration sections 4 and 12: descriptive thresholds beside the primary 5 pp
STRESS_DEPTH_USD = 500.0  # registration section 12: USD at the best ask >= $500 instead of $200


def main_period(t0: int) -> tuple[int, int]:
    """[start, end) of the entry period of the main test, unix seconds."""
    return int(t0), int(t0) + MAIN_PERIOD_DAYS * DAY_S


def verdict_ts(t0: int) -> int:
    """The single moment the verdict may be computed: T0 + 126 days."""
    return int(t0) + VERDICT_DAYS * DAY_S


def verdict_ts_v2g(t0_v2g: int, t0_v1: int) -> int:
    """The single moment the verdict of v2-g's own test may be computed: not before its T0 + 126 days AND not before
    v1's verdict (card IDEA-20261101-polymarket-v2g-forward, section 1)."""
    return max(verdict_ts(t0_v2g), verdict_ts(t0_v1))


# ----------------------------------------------------------------------------- settled results


def gamma_outcome(market: Mapping[str, Any]) -> int | None:
    """Settled result of a Gamma market: 1 if YES paid 1, 0 if NO did, None otherwise. Requires ``closed``, the labels
    ``outcomes`` = ["Yes", "No"] in that order (the prices are read by position), and, when Gamma gives the field,
    ``umaResolutionStatus`` = "resolved" (a result is stored once and never revised, so a proposed one is not taken)."""
    if not market.get("closed"):
        return None
    labels = _json_list(market.get("outcomes"))
    if not labels or [str(x).strip().lower() for x in labels] != ["yes", "no"]:
        return None
    uma = market.get("umaResolutionStatus")
    if uma is not None and str(uma).lower() != "resolved":
        return None
    return outcome_yes(market.get("outcomePrices"))


def binance_closes(bars: Iterable[Sequence[Any]], end_ts: int) -> tuple[float | None, float | None]:
    """Closes of the Binance 1m candles ``[open_ms, o, h, l, c, v]`` that close at ``end_ts`` (open = end - 60 s) and
    that open at ``end_ts``. The texts name either "the 1 minute candle ... 12:00" (2025) or "the 1 minute candle ...
    closing at 12:00" (since 2026-10-07), so both are kept for the cross-check. None where the bar is absent."""
    at_end = nxt = None
    for b in bars:
        try:
            t, c = int(float(b[0]) // 1000), float(b[4])
        except (IndexError, TypeError, ValueError):
            continue
        if not math.isfinite(c):
            continue
        if t == end_ts - 60:
            at_end = c
        elif t == end_ts:
            nxt = c
    return at_end, nxt


def binance_check(outcome_yes: int | None, strike: float, close_end: float | None, close_next: float | None) -> str:
    """Cross-check of a threshold market's Gamma result against Binance (registration section 7).

    YES iff close > strike. 'match' when every available candle agrees with Gamma, 'mismatch' when none does (the
    market is excluded and listed), 'ambiguous' when the two candles disagree with each other (the strike was crossed in
    that minute) and Gamma agrees with one of them (kept, counted), 'unchecked' without a result or a candle."""
    if outcome_yes is None:
        return "unchecked"
    implied = {int(c > strike) for c in (close_end, close_next) if c is not None}
    if not implied:
        return "unchecked"
    if implied == {int(outcome_yes)}:
        return "match"
    return "ambiguous" if int(outcome_yes) in implied else "mismatch"


def settled_outcomes(outcome_rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, Any]]:
    """Results usable for P&L and Brier: ``{market_id: 1 | 0}`` without the threshold markets whose Gamma result
    contradicts Binance, and a summary ``{"settled", "check": {status: n}, "excluded": [market ids]}``."""
    out: dict[str, int] = {}
    checks: Counter = Counter()
    excluded: list[str] = []
    for o in outcome_rows:
        y = o.get("outcome_yes")
        if y is None:
            continue
        mid = str(o["market_id"])
        if o.get("kind") == "above":
            st = binance_check(int(y), float(o["strike"]), o.get("binance_close_end"), o.get("binance_close_next"))
            checks[st] += 1
            if st == "mismatch":
                excluded.append(mid)
                continue
        out[mid] = int(y)
    return out, {"settled": len(out) + len(excluded), "check": dict(sorted(checks.items())), "excluded": sorted(excluded)}


# ----------------------------------------------------------------------------- weekly descriptive report


def _sd(values: Sequence[float]) -> float | None:
    return float(statistics.stdev(values)) if len(values) >= 2 else None


def _share(k: int, n: int) -> float | None:
    return k / n if n else None


def _horizon_status(pos: Mapping[str, Any], market_rows: Sequence[Mapping[str, Any]], hours: int, now_ts: int) -> tuple[float | None, str]:
    """:func:`horizon_clv`, but 'pending' while the horizon point (+ its 1 h tolerance) is still in the future."""
    ts, end_ts = int(pos["ts"]), int(pos["end_ts"])
    if end_ts - ts < (hours + 2) * HOUR_S:
        return None, "n/a"
    if now_ts < ts + hours * HOUR_S + HORIZON_TOL_S:
        return None, "pending"
    return horizon_clv(pos, market_rows, hours)


def _close_status(pos: Mapping[str, Any], market_rows: Sequence[Mapping[str, Any]], now_ts: int) -> tuple[float | None, str]:
    """:func:`closing_value` minus the entry cost, 'pending' while the closing window (ends at end - 1 h) is open."""
    end_ts = int(pos["end_ts"])
    if now_ts < end_ts - HOUR_S:
        return None, "pending"
    v, src = closing_value(market_rows, str(pos["side"]), end_ts, int(pos["ts"]))
    return (None if v is None else v - float(pos["cost"])), src


def _by_market(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        out[str(r["market_id"])].append(r)
    return out


MISSING_REASONS = ("collector_gap", "market_absent", "no_mid")


def horizon_missing_reasons(
    positions: Iterable[Mapping[str, Any]],
    by_mkt: Mapping[str, Sequence[Mapping[str, Any]]],
    snapshot_times: Iterable[int],
    hours: int,
    now_ts: int,
) -> dict[str, int]:
    """Why the CLV point at entry + ``hours`` is 'missing' (counts only, no value): 'collector_gap' = no collector
    snapshot at all in [entry + hours - 1 h, entry + hours + 1 h]; 'market_absent' = snapshots exist there but none of
    this market (it dropped out of the collector's list); 'no_mid' = the market's rows are there but the side has no mid."""
    times = sorted({int(t) for t in snapshot_times})
    out = dict.fromkeys(MISSING_REASONS, 0)
    for p in positions:
        mk = by_mkt.get(str(p["market_id"]), [])
        if _horizon_status(p, mk, hours, now_ts)[1] != "missing":
            continue
        lo, hi = int(p["ts"]) + hours * HOUR_S - HORIZON_TOL_S, int(p["ts"]) + hours * HOUR_S + HORIZON_TOL_S
        i = bisect_left(times, lo)
        if i >= len(times) or times[i] > hi:
            out["collector_gap"] += 1
        elif not any(lo <= int(r["ts"]) <= hi for r in mk):
            out["market_absent"] += 1
        else:
            out["no_mid"] += 1
    return out


def weekly_descriptive(
    rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    now_ts: int,
    t0: int,
    rule: EntryRule | None = None,
) -> dict[str, Any]:
    """Everything the weekly report may show (registration section 1), and nothing else: no mean of any CLV, no CI,
    no P&L, no Brier. SDs are of positions whose value is already known."""
    rule = rule or EntryRule()
    start, end = main_period(t0)
    acc = accepted_rule_hashes(rows)
    period = [r for r in rows if start <= int(r["ts"]) < end and int(r["ts"]) <= now_ts]
    by_mkt = _by_market(r for r in rows if int(r["ts"]) <= now_ts)
    no_skew = replace(rule, max_quote_skew_s=math.inf)

    snaps: Counter = Counter()
    elig: Counter = Counter()
    without_ts = would_be = dropped_skew = 0
    for r in period:
        key = f"{r.get('asset')}/{r.get('kind')}"
        snaps[key] += 1
        if r.get("book_ts") is None or r.get("surface_ts") is None:
            without_ts += 1
        if eligible(r, no_skew, acc):
            would_be += 1
            if eligible(r, rule, acc):
                elig[key] += 1
            else:
                dropped_skew += 1

    positions = open_positions(period, rule, acc)
    per_group: Counter = Counter(f"{p['asset']}/{p['kind']}/{p['side']}" for p in positions)
    per_week: Counter = Counter(p["week_entry"] for p in positions)
    src24: Counter = Counter()
    src6: Counter = Counter()
    src_close: Counter = Counter()
    v24: list[float] = []
    v6: list[float] = []
    vclose: list[float] = []
    for p in positions:
        mk = by_mkt.get(p["market_id"], [])
        c24, s24 = _horizon_status(p, mk, PRIMARY_H, now_ts)
        c6, s6 = _horizon_status(p, mk, SECONDARY_H, now_ts)
        cc, sc = _close_status(p, mk, now_ts)
        src24[s24] += 1
        src6[s6] += 1
        src_close[sc] += 1
        v24 += [c24] if c24 is not None else []
        v6 += [c6] if c6 is not None else []
        vclose += [cc] if cc is not None else []

    _, settled = settled_outcomes(outcome_rows)
    pos_markets = {p["market_id"] for p in positions}
    settled_pos = sum(1 for o in outcome_rows if o.get("outcome_yes") is not None and str(o["market_id"]) in pos_markets)
    ended_pos = {p["market_id"] for p in positions if int(p["end_ts"]) <= now_ts}
    defined24 = src24["ok"] + src24["missing"]
    closed = src_close["close"] + src_close["late"] + src_close["missing"]
    return {
        "t0": int(t0),
        "now": int(now_ts),
        "period_end": end,
        "verdict_ts": verdict_ts(t0),
        "days_since_t0": (now_ts - t0) / DAY_S,
        "snapshots": {
            "rows": len(period),
            "times": len({int(r["ts"]) for r in period}),
            "markets": len({str(r["market_id"]) for r in period}),
            "by_asset_kind": dict(sorted(snaps.items())),
            "eligible_by_asset_kind": dict(sorted(elig.items())),
            "eligible": sum(elig.values()),
            "accepted_rule_texts": len(acc),
            "without_read_times": without_ts,
            "share_without_read_times": _share(without_ts, len(period)),
            "dropped_for_skew": dropped_skew,
            "share_dropped_for_skew": _share(dropped_skew, would_be),
        },
        "positions": {
            "n": len(positions),
            "by_asset_kind_side": dict(sorted(per_group.items())),
            "by_entry_week": dict(sorted(per_week.items())),
            "markets_both_sides": sum(1 for v in Counter(p["market_id"] for p in positions).values() if v > 1),
            "entry_weeks": len(per_week),
            "resolution_days": len({p["day"] for p in positions}),
            "resolution_weeks": len({p["week_res"] for p in positions}),
            "ended": len(ended_pos),
            "resolution_days_ended": len({p["day"] for p in positions if p["market_id"] in ended_pos}),
            "with_settled_result": settled_pos,
        },
        "sources": {
            "h24": dict(sorted(src24.items())),
            "h24_share_missing": _share(src24["missing"], defined24),
            "h24_missing_reasons": horizon_missing_reasons(
                positions, by_mkt, (int(r["ts"]) for r in rows if int(r["ts"]) <= now_ts), PRIMARY_H, now_ts
            ),
            "h6": dict(sorted(src6.items())),
            "close": dict(sorted(src_close.items())),
            "close_shares": {k: _share(src_close[k], closed) for k in ("close", "late", "missing")},
        },
        "sd_per_position": {
            "h24": {"n": len(v24), "sd": _sd(v24)},
            "h6": {"n": len(v6), "sd": _sd(v6)},
            "close": {"n": len(vclose), "sd": _sd(vclose)},
        },
        "results": settled,
    }


def _d(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _p(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{nd}f} %"


def _pp(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.2f} п. п."


def _kv(d: Mapping[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "—"


def _missing_line(m: Mapping[str, int]) -> str:
    return (
        f"  - причини missing +24 год: у вікні ±1 год немає жодного знімка колектора {m['collector_gap']}; знімки є, "
        f"а ринку в них немає {m['market_absent']}; ринок є, але без mid {m['no_mid']}"
    )


NO_MEAN_NOTE = (
    "Звіт описовий (розділ 1 реєстрації): основна метрика, її довірчий інтервал, прибуток і порівняння точності "
    "з'являться лише у вердикті, один раз, після {verdict}."
)


def render_weekly(d: Mapping[str, Any]) -> str:
    """Markdown (Ukrainian) of :func:`weekly_descriptive`. Shows only the fields of that dict."""
    s, p, src, sd, res = d["snapshots"], d["positions"], d["sources"], d["sd_per_position"], d["results"]
    lines = [
        "## Polymarket проти опціонів Deribit: тижневий звіт (паперові позиції)",
        "",
        f"Станом на {_d(d['now'])}; T0 = {_d(d['t0'])} (день {d['days_since_t0']:.1f} з 112 входу); "
        f"вхід до {_d(d['period_end'])}; вердикт — не раніше {_d(d['verdict_ts'])}.",
        "Картка: docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md. Позиції паперові, ордерів немає.",
        "",
        NO_MEAN_NOTE.format(verdict=_d(d["verdict_ts"])),
        "",
        "**Знімки**",
        f"- рядків {s['rows']}, моментів {s['times']}, ринків {s['markets']}; за активом/типом: {_kv(s['by_asset_kind'])}",
        f"- допущено правилом (розділ 2): {s['eligible']} ({_kv(s['eligible_by_asset_kind'])}); прийнятих текстів правил: "
        f"{s['accepted_rule_texts']}",
        f"- без часу читання книги/поверхні: {s['without_read_times']} ({_p(s['share_without_read_times'])}); "
        f"відкинуто за розсинхроном > 60 с: {s['dropped_for_skew']} ({_p(s['share_dropped_for_skew'])} від інакше допущених)",
        "",
        "**Позиції**",
        f"- усього {p['n']}; за активом/типом/боком: {_kv(p['by_asset_kind_side'])}; ринків з обома боками: {p['markets_both_sides']}",
        f"- за тижнем входу: {_kv(p['by_entry_week'])} (тижнів входу {p['entry_weeks']})",
        f"- днів розв'язання {p['resolution_days']} (уже минули: {p['resolution_days_ended']}), тижнів розв'язання "
        f"{p['resolution_weeks']}; ринків, що завершились: {p['ended']}, з отриманим результатом: {p['with_settled_result']}",
        "",
        "**Джерела точок CLV**",
        f"- +24 год (основна): {_kv(src['h24'])}; частка missing серед визначених {_p(src['h24_share_missing'])}",
        _missing_line(src["h24_missing_reasons"]),
        f"- +6 год: {_kv(src['h6'])}",
        f"- до закриття: {_kv(src['close'])}; частки close / late / missing: "
        f"{_p(src['close_shares']['close'])} / {_p(src['close_shares']['late'])} / {_p(src['close_shares']['missing'])}",
        "",
        "**SD на позицію** (розкид, без середнього)",
        f"- +24 год: {_pp(sd['h24']['sd'])} (n {sd['h24']['n']}); +6 год: {_pp(sd['h6']['sd'])} (n {sd['h6']['n']}); "
        f"до закриття: {_pp(sd['close']['sd'])} (n {sd['close']['n']})",
        "",
        "**Результати ринків** (Gamma outcomePrices; звірка порогових з Binance 1m)",
        f"- отримано {res['settled']}; звірка: {_kv(res['check'])}; виключено через розбіжність: {', '.join(res['excluded']) or 'немає'}",
    ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- the verdict (once)


def final_metrics(
    rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    now_ts: int,
    t0: int,
    rule: EntryRule | None = None,
) -> dict[str, Any]:
    """The registered decision (sections 8-10), computed ONCE at :func:`verdict_ts`. Raises ValueError before then.

    Primary: CLV +24 h per position (cluster = ISO week of the entry, ET). Secondary: P&L and the closing CLV (cluster =
    resolution week), Brier D = model (k = 1) - Polymarket per resolved market over the main period's eligible snapshots
    (cluster = resolution week); k = 0.92 alongside. Stress and robustness (sections 6 and 12) under ``robustness``:
    descriptive, they never change the verdict (see :func:`_robustness`).

    A pure function cannot know the real time: the gate is held by the runner (scripts/pm_gap_paper_report.py), which
    calls this only with the wall clock and the registered :data:`T0_TS`, never with values from the command line."""
    if now_ts < verdict_ts(t0):
        raise ValueError(f"the verdict may be computed only from {verdict_ts(t0)} (T0 + {VERDICT_DAYS} days), now {now_ts}")
    rule = rule or EntryRule()
    start, end = main_period(t0)
    acc = accepted_rule_hashes(rows)
    period = [r for r in rows if start <= int(r["ts"]) < end]
    by_mkt = _by_market(rows)
    outcomes, res = settled_outcomes(outcome_rows)
    excluded = set(res["excluded"])
    positions = [p for p in open_positions(period, rule, acc) if p["market_id"] not in excluded]
    pm = [position_metrics(p, by_mkt[p["market_id"]], outcomes.get(p["market_id"])) for p in positions]

    def summ(key: str, cluster: str) -> dict[str, Any]:
        xs = [m for m in pm if m[key] is not None]
        return cluster_mean_ci([m[key] for m in xs], [m[cluster] for m in xs], [m["day"] for m in xs])

    primary = summ("clv_24h", "week_entry")
    pnl = summ("pnl", "week_res")
    brier = {}
    for k in (1.0, VOL_HAIRCUT_K):
        bm = brier_by_market(period, outcomes, rule, acc, k)
        brier[str(k)] = {
            "markets": len(bm),
            **cluster_mean_ci([b["diff"] for b in bm], [b["week_res"] for b in bm], [b["day"] for b in bm]),
        }
    v, reason = verdict(primary, pnl, brier["1.0"])
    ctx = {"period": period, "by_mkt": by_mkt, "outcomes": outcomes, "excluded": excluded, "acc": acc}
    return {
        "t0": int(t0),
        "now": int(now_ts),
        "positions": len(pm),
        "excluded_markets": sorted(excluded),
        "primary_clv_24h": primary,
        "clv_6h": summ("clv_6h", "week_entry"),
        "clv_close": summ("clv", "week_res"),
        "pnl": pnl,
        "brier_diff": brier,
        "verdict": v,
        "reason": reason,
        "robustness": _robustness(ctx, rule, pm, start, end),
    }


def _summ(pm: Sequence[Mapping[str, Any]], key: str, cluster: str) -> dict[str, Any]:
    xs = [m for m in pm if m[key] is not None]
    return cluster_mean_ci([m[key] for m in xs], [m[cluster] for m in xs], [m["day"] for m in xs])


def _block(pm: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Positions, primary CLV +24 h (cluster = entry week) and P&L (cluster = resolution week) of one variant."""
    return {"positions": len(pm), "clv_24h": _summ(pm, "clv_24h", "week_entry"), "pnl": _summ(pm, "pnl", "week_res")}


def _robustness(ctx: Mapping[str, Any], rule: EntryRule, pm: Sequence[Mapping[str, Any]], start: int, end: int) -> dict[str, Any]:
    """Stress (section 6) and robustness (section 12), all descriptive; only called from the gated :func:`final_metrics`.

    * ``variants``: the rule re-run with threshold 3 / 8 pp, k = 1 only, k = 0.92 only, USD at the ask >= $500; the
      primary positions bought 1 cent above the ask (:func:`worse_entry`); re-entered at the next snapshot within 2 h
      (:func:`delayed_entry`, the dropped ones counted).
    * ``halves``: primary positions by entry in the first / second half of the main period; ``by_asset``, ``by_kind``.
    * ``asset_permutation``: BTC - ETH mean primary CLV, asset labels permuted within the entry week.
    * ``cluster_day``: closing CLV, P&L and Brier D (k = 1) with cluster = resolution day; ``clv_close_only_close``.
    Not computed here (see docs/BACKLOG.md): "without the 3 weeks of the largest contribution"; the v2-g verdict on its
    own period adds it (:func:`without_top_weeks`), v1's verdict does not. Every variant is ``replace(rule, ...)``, so
    ``rule.model`` is kept."""
    period, by_mkt, outcomes, excluded, acc = ctx["period"], ctx["by_mkt"], ctx["outcomes"], ctx["excluded"], ctx["acc"]

    def metrics(positions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [position_metrics(p, by_mkt[p["market_id"]], outcomes.get(p["market_id"])) for p in positions]

    def rerun(r: EntryRule) -> dict[str, Any]:
        return _block(metrics(p for p in open_positions(period, r, acc) if p["market_id"] not in excluded))

    variants: dict[str, Any] = {f"threshold_{t}": rerun(replace(rule, threshold=t)) for t in SENS_THRESHOLDS}
    variants["k_1.0"] = rerun(replace(rule, vol_ks=(1.0,)))
    variants[f"k_{VOL_HAIRCUT_K}"] = rerun(replace(rule, vol_ks=(VOL_HAIRCUT_K,)))
    variants["depth_500"] = rerun(replace(rule, min_depth_usd=STRESS_DEPTH_USD))
    variants["entry_plus_1c"] = _block(metrics(worse_entry(p, WORSE_ENTRY) for p in pm))
    delayed = [d for p in pm if (d := delayed_entry(p, by_mkt[p["market_id"]], rule, acc)) is not None]
    variants["next_snapshot"] = {
        **_block(metrics(delayed)),
        "dropped": len(pm) - len(delayed),
        "share_dropped": _share(len(pm) - len(delayed), len(pm)),
    }

    mid = start + (end - start) // 2
    with24 = [m for m in pm if m["clv_24h"] is not None]
    brier = brier_by_market(period, outcomes, rule, acc, 1.0)
    return {
        "variants": variants,
        "halves": {
            "first": _block([m for m in pm if int(m["ts"]) < mid]),
            "second": _block([m for m in pm if int(m["ts"]) >= mid]),
            "split_ts": mid,
        },
        "by_asset": {a: _block([m for m in pm if m["asset"] == a]) for a in ("BTC", "ETH")},
        "by_kind": {k: _block([m for m in pm if m["kind"] == k]) for k in ("above", "touch")},
        "asset_permutation": asset_permutation_p(
            [m["clv_24h"] for m in with24], [m["asset"] for m in with24], [m["week_entry"] for m in with24]
        ),
        "cluster_day": {
            "clv_close": _summ(pm, "clv", "day"),
            "pnl": _summ(pm, "pnl", "day"),
            "brier_k1": cluster_mean_ci([b["diff"] for b in brier], [b["day"] for b in brier], [b["day"] for b in brier]),
        },
        "clv_close_only_close": _summ([m for m in pm if m["close_src"] == "close"], "clv", "week_res"),
    }


def render_verdict(f: Mapping[str, Any]) -> str:
    def row(name: str, s: Mapping[str, Any]) -> str:
        return (
            f"- {name}: n {s['n']}, тижнів {s['n_clusters']}, днів {s['n_days']}; середнє {_pp(s['mean'])}, "
            f"95 % CI [{_pp(s['lo'])}; {_pp(s['hi'])}]"
        )

    lines = [
        "## Polymarket проти опціонів Deribit: вердикт (один раз)",
        "",
        f"T0 = {_d(f['t0'])}, обчислено {_d(f['now'])}. Позицій {f['positions']}; виключено ринків (розбіжність з Binance): "
        f"{', '.join(f['excluded_markets']) or 'немає'}.",
        "",
        f"**Вердикт: {f['verdict']}** ({f['reason']})",
        "",
        row("CLV +24 год (основна, кластер — тиждень входу)", f["primary_clv_24h"]),
        row("CLV +6 год", f["clv_6h"]),
        row("CLV до закриття (кластер — тиждень розв'язання)", f["clv_close"]),
        row("P&L на акцію", f["pnl"]),
        row("Brier D, k = 1 (модель − Polymarket; > 0 — Polymarket точніший)", f["brier_diff"]["1.0"]),
        row(f"Brier D, k = {VOL_HAIRCUT_K}", f["brier_diff"][str(VOL_HAIRCUT_K)]),
        "",
        *render_robustness(f["robustness"]),
    ]
    return "\n".join(lines) + "\n"


VARIANT_NAMES = {
    "threshold_0.03": "поріг 3 п. п.",
    "threshold_0.08": "поріг 8 п. п.",
    "k_1.0": "лише k = 1",
    f"k_{VOL_HAIRCUT_K}": f"лише k = {VOL_HAIRCUT_K}",
    "depth_500": "глибина на аску ≥ 500 $",
    "entry_plus_1c": "вхід на 1 цент гірше",
    "next_snapshot": "вхід на наступному знімку (≤ 2 год)",
}


def render_robustness(r: Mapping[str, Any]) -> list[str]:
    """Markdown lines (Ukrainian) of the ``robustness`` block of :func:`final_metrics`."""

    def ci(s: Mapping[str, Any]) -> str:
        return f"n {s['n']}, кластерів {s['n_clusters']}; середнє {_pp(s['mean'])}, 95 % CI [{_pp(s['lo'])}; {_pp(s['hi'])}]"

    def blk(name: str, b: Mapping[str, Any], extra: str = "") -> str:
        return f"- {name}: позицій {b['positions']}{extra}; CLV +24 год: {ci(b['clv_24h'])}; P&L: {ci(b['pnl'])}"

    v, perm, cd = r["variants"], r["asset_permutation"], r["cluster_day"]
    p_perm = "n/a" if perm["p"] is None else f"{perm['p']:.4f}"
    return [
        "**Стрес і стійкість** (розділи 6 і 12; описово, вердикт за ними не виноситься)",
        *(
            blk(VARIANT_NAMES.get(k, k), b, f" (випало {b['dropped']}, {_p(b['share_dropped'])})" if k == "next_snapshot" else "")
            for k, b in v.items()
        ),
        blk("перша половина періоду (вхід)", r["halves"]["first"]),
        blk("друга половина періоду (вхід)", r["halves"]["second"]),
        *(blk(a, b) for a, b in r["by_asset"].items()),
        *(blk("пороговий" if k == "above" else "дотик", b) for k, b in r["by_kind"].items()),
        f"- BTC − ETH, CLV +24 год: різниця {_pp(perm['diff'])} (n {perm['n_a']} / {perm['n_b']}); перестановка міток "
        f"активу всередині тижня входу: p = {p_perm} ({perm['n_perm']} перестановок)",
        f"- кластер = день розв'язання: CLV до закриття {ci(cd['clv_close'])}; P&L {ci(cd['pnl'])}; Brier D (k = 1) {ci(cd['brier_k1'])}",
        f"- CLV до закриття лише з close: {ci(r['clv_close_only_close'])}",
        "",
        (
            f"- без 3 тижнів входу з найбільшою сумою CLV +24 год ({', '.join(r['without_top3_weeks']['weeks_removed']) or '—'}): "
            f"CLV +24 год {ci(r['without_top3_weeks'])}"
            if "without_top3_weeks" in r
            else "Не пораховано: «без 3 тижнів з найбільшим внеском» (неоднозначно, питання до кванта в BACKLOG)."
        ),
    ]


# ----------------------------------------------------------------------------- variant v2-g (H8), beside v1


def _median(xs: Sequence[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _split_counts(split: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    st: Counter = Counter(s["g_status"] for s in split)
    dep: Counter = Counter(s["group"] for s in split if s["g_status"] == "g_dependent")
    return {"status": {k: st[k] for k in G_STATUSES}, "g_dependent_by_group": dict(sorted(dep.items()))}


def weekly_v2g(
    rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    now_ts: int,
    t0: int,
    rule: EntryRule | None = None,
    *,
    v1_t0: int | None = None,
) -> dict[str, Any]:
    """Weekly block of the variant v2-g, within the limits of :func:`weekly_descriptive` (no mean of any CLV, no CI,
    no P&L, no Brier): eligible snapshots with / without ``binance_last``, g quantiles, v2-g positions against v1's
    (same main period and rule, only the model differs), median entry edge of both, SD per position of the known v2-g
    CLV at +24 h, the sources of that point (shares of 'n/a' and 'missing', reasons of 'missing'), and v1 positions
    split by :func:`.pm_gap_paper.g_split`. g is the snapshot's (ts, asset) value (:func:`.pm_gap_paper.with_snapshot_g`).
    ``outcome_rows`` is unused (same signature).

    ``v1_t0`` given = v2-g's OWN test (``t0`` = :data:`T0_V2G_TS`): ``own`` adds its day of 112, the end of entry and
    the verdict date (:func:`verdict_ts_v2g`), and from 4 weeks on ``power`` = :func:`.pm_gap_paper.power_stage0` of the
    centred CLV +24 h (16 projected week clusters; SDs and MDE only, no mean, no SE)."""
    del outcome_rows
    rule = rule or EntryRule()
    v2 = replace(rule, model=MODEL_V2G)
    start, end = main_period(t0)
    acc = accepted_rule_hashes(rows)
    period = with_snapshot_g(r for r in rows if start <= int(r["ts"]) < end and int(r["ts"]) <= now_ts)
    by_mkt = _by_market(r for r in rows if int(r["ts"]) <= now_ts)
    elig = [r for r in period if eligible(r, rule, acc)]
    gs = [g for r in elig if (g := binance_gap(r)) is not None]
    p1, p2 = open_positions(period, rule, acc), open_positions(period, v2, acc)
    k1, k2 = {(p["market_id"], p["side"]) for p in p1}, {(p["market_id"], p["side"]) for p in p2}
    st24 = [_horizon_status(p, by_mkt.get(p["market_id"], []), PRIMARY_H, now_ts) for p in p2]
    v24 = [c for c, _ in st24 if c is not None]
    src24: Counter = Counter(s for _, s in st24)
    out_own: dict[str, Any] = {}
    if v1_t0 is not None:
        out_own["own"] = {
            "t0": int(t0),
            "period_end": end,
            "verdict_ts": verdict_ts_v2g(t0, v1_t0),
            "days_since_t0": (now_ts - t0) / DAY_S,
        }
        if now_ts >= int(t0) + STAGE0_DAYS * DAY_S:
            ok = [(c, p["week_entry"]) for (c, _), p in zip(st24, p2, strict=True) if c is not None]
            pw = power_stage0([c for c, _ in ok], [w for _, w in ok], MAIN_PERIOD_DAYS // 7)
            out_own["power"] = {k: pw.get(k) for k in POWER_KEYS}
    return {
        **out_own,
        "model": MODEL_V2G,
        "eligible": len(elig),
        "eligible_without_binance": len(elig) - len(gs),
        "g_median": _median(gs),
        "g_min": min(gs) if gs else None,
        "g_max": max(gs) if gs else None,
        "positions": {
            "n": len(p2),
            "by_asset_kind_side": dict(sorted(Counter(f"{p['asset']}/{p['kind']}/{p['side']}" for p in p2).items())),
            "entry_weeks": len({p["week_entry"] for p in p2}),
            "resolution_days": len({p["day"] for p in p2}),
            "same_as_v1": len(k1 & k2),
            "only_v2g": len(k2 - k1),
            "only_v1": len(k1 - k2),
            "edge_median": _median([p["edge"] for p in p2]),
            "edge_median_v1": _median([p["edge"] for p in p1]),
        },
        "sd_per_position": {"h24": {"n": len(v24), "sd": _sd(v24)}},
        "sources": {
            "h24": dict(sorted(src24.items())),
            "h24_share_na": _share(src24["n/a"], len(p2)),
            "h24_share_missing": _share(src24["missing"], src24["ok"] + src24["missing"]),
            "h24_missing_reasons": horizon_missing_reasons(
                p2, by_mkt, (int(r["ts"]) for r in rows if int(r["ts"]) <= now_ts), PRIMARY_H, now_ts
            ),
        },
        "v1_split": _split_counts(g_split(p1, period, rule)),
    }


V2G_TITLE = (
    "**Варіант v2-g** (поправка H8: форвард = індекс Deribit × (1 + g), g = binance_last / spot − 1 того самого знімка (момент і актив; "
    "колектор пише binance_last лише в рядках дотику, порогові рядки беруть g свого знімка); "
    "описово, поруч із замороженим v1, реєстрацію й поріг v1 не змінює)"
)


def _split_line(s: Mapping[str, Any]) -> str:
    st = s["status"]
    return (
        f"- позиції v1 під поправкою g (той самий знімок, той самий аск): edge ≥ порогу лишається {st['robust']}; "
        f"тримався лише на зсуві g {st['g_dependent']} ({_kv(s['g_dependent_by_group'])}); без binance_last {st['no_binance']}; "
        f"не пораховано {st['not_computed']}"
    )


def render_weekly_v2g(d: Mapping[str, Any]) -> str:
    """Markdown (Ukrainian) of :func:`weekly_v2g`. Shows only the fields of that dict."""
    p, sd, src = d["positions"], d["sd_per_position"], d["sources"]
    own = d.get("own")
    head = (
        [
            f"- власний форвард-тест v2-g (docs/ideas/IDEA-20261101-polymarket-v2g-forward.md): T0 v2-g = {_d(own['t0'])} "
            f"(день {own['days_since_t0']:.1f} з 112 входу); вхід до {_d(own['period_end'])}; вердикт v2-g — не раніше "
            f"{_d(own['verdict_ts'])}; позиції v1 нижче — те саме правило на тих самих знімках періоду v2-g"
        ]
        if own
        else []
    )
    pw = d.get("power")
    power = (
        [
            f"- стадія 0 (потужність, без середнього; CLV +24 год центровано): n {pw['n']}, тижнів {pw['n_clusters']}; "
            f"SD на позицію {_pp(pw['sd_position'])}, SD тижневих середніх {_pp(pw['sd_cluster'])}; MDE зараз {_pp(pw['mde_now'])}, "
            f"прогноз на {MAIN_PERIOD_DAYS // 7} тижнів {_pp(pw['mde_projected'])}"
        ]
        if pw
        else []
    )
    lines = [
        V2G_TITLE,
        *head,
        f"- допущених знімків {d['eligible']}, з них без binance_last (v2-g не рахується, g не підставляється): "
        f"{d['eligible_without_binance']}; g: медіана {_p(d['g_median'], 3)}, від {_p(d['g_min'], 3)} до {_p(d['g_max'], 3)}",
        f"- позицій v2-g {p['n']}; за активом/типом/боком: {_kv(p['by_asset_kind_side'])}; тижнів входу {p['entry_weeks']}, "
        f"днів розв'язання {p['resolution_days']}",
        f"- ті самі (ринок, бік), що й у v1: {p['same_as_v1']}; лише v2-g: {p['only_v2g']}; лише v1: {p['only_v1']}",
        f"- edge на вході, медіана: v2-g {_pp(p['edge_median'])}, v1 {_pp(p['edge_median_v1'])}",
        f"- SD CLV +24 год на позицію v2-g: {_pp(sd['h24']['sd'])} (n {sd['h24']['n']})",
        f"- точки CLV +24 год v2-g: {_kv(src['h24'])}; частка n/a (< 26 год до кінця) {_p(src['h24_share_na'])}; "
        f"частка missing серед визначених {_p(src['h24_share_missing'])}",
        _missing_line(src["h24_missing_reasons"]),
        *power,
        _split_line(d["v1_split"]),
        "- CLV, P&L і Brier варіанта v2-g — лише у вердикті: його позиції здебільшого збігаються з v1, тож їхнє середнє розкрило б v1.",
    ]
    return "\n".join(lines) + "\n"


def final_metrics_v2g(
    rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    now_ts: int,
    t0: int,
    rule: EntryRule | None = None,
    *,
    entry_before: int,
) -> dict[str, Any]:
    """The variant v2-g at v1's verdict, descriptive (the decision is v1's :func:`final_metrics`). Raises ValueError
    before :func:`verdict_ts`, like v1. Same exclusions, clusters and metrics as v1, with ``model='v2-g'``: positions,
    mean entry edge, CLV +24 h / +6 h / to close, P&L, Brier D (k = 1 and 0.92, over snapshots that have a v2-g
    probability). ``v1_split``: v1 positions by :func:`.pm_gap_paper.g_split`, with CLV and P&L of the 'g_dependent'
    and 'robust' ones. g is the snapshot's (ts, asset) value (:func:`.pm_gap_paper.with_snapshot_g`).

    Required by the v2-g registration (card IDEA-20261101-polymarket-v2g-forward, item 5): EVERY row here, the
    ``v1_split`` included, uses only snapshots with ts < ``entry_before`` (= :data:`T0_V2G_TS`), i.e. positions entered
    before v2-g's own test, so v1's verdict prints nothing of that test. Positions are the first qualifying snapshot, so
    cutting the snapshots cuts the entries exactly; their later CLV / P&L points are read from all rows, as in v1."""
    if now_ts < verdict_ts(t0):
        raise ValueError(f"the verdict may be computed only from {verdict_ts(t0)} (T0 + {VERDICT_DAYS} days), now {now_ts}")
    rule = rule or EntryRule()
    v2 = replace(rule, model=MODEL_V2G)
    start, end = main_period(t0)
    end = min(end, int(entry_before))
    acc = accepted_rule_hashes(rows)
    period = with_snapshot_g(r for r in rows if start <= int(r["ts"]) < end)
    by_mkt = _by_market(rows)
    outcomes, res = settled_outcomes(outcome_rows)
    excluded = set(res["excluded"])

    def metrics(positions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [position_metrics(p, by_mkt[p["market_id"]], outcomes.get(p["market_id"])) for p in positions]

    p1 = [p for p in open_positions(period, rule, acc) if p["market_id"] not in excluded]
    pm2 = metrics(p for p in open_positions(period, v2, acc) if p["market_id"] not in excluded)
    brier = {}
    for k in (1.0, VOL_HAIRCUT_K):
        bm = brier_by_market(period, outcomes, v2, acc, k)
        brier[str(k)] = {
            "markets": len(bm),
            **cluster_mean_ci([b["diff"] for b in bm], [b["week_res"] for b in bm], [b["day"] for b in bm]),
        }
    split = g_split(p1, period, rule)
    return {
        "model": MODEL_V2G,
        "entry_before": int(entry_before),
        "positions": len(pm2),
        "edge_mean": float(statistics.fmean(m["edge"] for m in pm2)) if pm2 else None,
        "clv_24h": _summ(pm2, "clv_24h", "week_entry"),
        "clv_6h": _summ(pm2, "clv_6h", "week_entry"),
        "clv_close": _summ(pm2, "clv", "week_res"),
        "pnl": _summ(pm2, "pnl", "week_res"),
        "brier_diff": brier,
        "v1_split": {
            **_split_counts(split),
            "g_dependent": _block(metrics(s for s in split if s["g_status"] == "g_dependent")),
            "robust": _block(metrics(s for s in split if s["g_status"] == "robust")),
        },
    }


def render_verdict_v2g(f: Mapping[str, Any]) -> str:
    """Markdown (Ukrainian) of :func:`final_metrics_v2g`."""

    def ci(name: str, s: Mapping[str, Any]) -> str:
        return (
            f"- {name}: n {s['n']}, тижнів {s['n_clusters']}, днів {s['n_days']}; середнє {_pp(s['mean'])}, "
            f"95 % CI [{_pp(s['lo'])}; {_pp(s['hi'])}]"
        )

    def blk(name: str, b: Mapping[str, Any]) -> str:
        c, p = b["clv_24h"], b["pnl"]
        return (
            f"- {name}: позицій {b['positions']}; CLV +24 год {_pp(c['mean'])} [{_pp(c['lo'])}; {_pp(c['hi'])}] (n {c['n']}); "
            f"P&L {_pp(p['mean'])} [{_pp(p['lo'])}; {_pp(p['hi'])}] (n {p['n']})"
        )

    s = f["v1_split"]
    lines = [
        V2G_TITLE,
        "Вердикт виноситься лише за v1 (вище); v2-g — окремі рядки для порівняння.",
        f"Усі рядки v2-g нижче (і розбір позицій v1 за g) — лише знімки й входи до {_d(f['entry_before'])} (T0 власного "
        "форвард-тесту v2-g, docs/ideas/IDEA-20261101-polymarket-v2g-forward.md): той тест має свій вердикт.",
        f"- позицій v2-g {f['positions']}; середній edge на вході {_pp(f['edge_mean'])}",
        ci("v2-g: CLV +24 год (кластер — тиждень входу)", f["clv_24h"]),
        ci("v2-g: CLV +6 год", f["clv_6h"]),
        ci("v2-g: CLV до закриття (кластер — тиждень розв'язання)", f["clv_close"]),
        ci("v2-g: P&L на акцію", f["pnl"]),
        ci(f"v2-g: Brier D, k = 1 (ринків {f['brier_diff']['1.0']['markets']})", f["brier_diff"]["1.0"]),
        ci(
            f"v2-g: Brier D, k = {VOL_HAIRCUT_K} (ринків {f['brier_diff'][str(VOL_HAIRCUT_K)]['markets']})",
            f["brier_diff"][str(VOL_HAIRCUT_K)],
        ),
        _split_line(s),
        blk("v1, edge тримався лише на зсуві g", s["g_dependent"]),
        blk("v1, edge лишається з поправкою g", s["robust"]),
    ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- v2-g: its own forward test (own T0, once)


def without_top_weeks(pm: Sequence[Mapping[str, Any]], n_weeks: int = TOP_WEEKS) -> dict[str, Any]:
    """Registration section 12, descriptive: drop the ``n_weeks`` ISO entry weeks (ET) with the LARGEST sum of the net
    CLV +24 h (ties: the earlier week label first), and recompute the mean and the cluster CI of the main metric (same
    estimator and seed, cluster = entry week) on the rest. Positions without a +24 h point are ignored."""
    xs = [m for m in pm if m.get("clv_24h") is not None]
    tot: dict[str, float] = defaultdict(float)
    for m in xs:
        tot[str(m["week_entry"])] += float(m["clv_24h"])
    drop = set(sorted(tot, key=lambda w: (-tot[w], w))[:n_weeks])
    keep = [m for m in xs if str(m["week_entry"]) not in drop]
    return {
        "weeks_removed": sorted(drop),
        **cluster_mean_ci([m["clv_24h"] for m in keep], [m["week_entry"] for m in keep], [m["day"] for m in keep]),
    }


def final_metrics_v2g_own(
    rows: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    now_ts: int,
    t0_v2g: int,
    t0_v1: int,
    rule: EntryRule | None = None,
) -> dict[str, Any]:
    """The registered decision of v2-g's OWN forward test (card IDEA-20261101-polymarket-v2g-forward), computed ONCE at
    :func:`verdict_ts_v2g`; raises ValueError before then. Everything as v1's :func:`final_metrics` (sections 7-10 and
    12 unchanged), with ``model='v2-g'`` and v2-g's own main period: positions entered in [``t0_v2g``, + 112 days) on
    snapshots of that interval (none before ``t0_v2g`` takes part). Primary CLV +24 h (cluster = entry week), P&L,
    Brier D (k = 1 decides, 0.92 beside, over snapshots with a v2-g probability) and :func:`.pm_gap_paper.verdict`.

    g is the snapshot's (ts, asset) value on EVERY row read, the per-market rows of the stress included, so a threshold
    row (no ``binance_last`` of its own) keeps its snapshot's g at the next-snapshot entry. ``vs_v1``: the v2-g positions
    split into those v1's rule also opens on the same snapshots and those only v2-g opens. ``robustness`` as v1's, plus
    ``without_top3_weeks`` (:func:`without_top_weeks`). ``entries``: (market, side, entry ts) of the positions.

    The runner calls this only with the wall clock, :data:`T0_V2G_TS` and :data:`T0_TS`, never values from the
    command line."""
    gate = verdict_ts_v2g(t0_v2g, t0_v1)
    if now_ts < gate:
        raise ValueError(f"the v2-g verdict may be computed only from {gate} (max of both T0 + {VERDICT_DAYS} days), now {now_ts}")
    rule = rule or EntryRule()
    v2 = replace(rule, model=MODEL_V2G)
    start, end = main_period(t0_v2g)
    acc = accepted_rule_hashes(rows)
    g_rows = with_snapshot_g(rows)
    period = [r for r in g_rows if start <= int(r["ts"]) < end]
    by_mkt = _by_market(g_rows)
    outcomes, res = settled_outcomes(outcome_rows)
    excluded = set(res["excluded"])
    positions = [p for p in open_positions(period, v2, acc) if p["market_id"] not in excluded]
    pm = [position_metrics(p, by_mkt[p["market_id"]], outcomes.get(p["market_id"])) for p in positions]
    primary = _summ(pm, "clv_24h", "week_entry")
    pnl = _summ(pm, "pnl", "week_res")
    brier = {}
    for k in (1.0, VOL_HAIRCUT_K):
        bm = brier_by_market(period, outcomes, v2, acc, k)
        brier[str(k)] = {
            "markets": len(bm),
            **cluster_mean_ci([b["diff"] for b in bm], [b["week_res"] for b in bm], [b["day"] for b in bm]),
        }
    v, reason = verdict(primary, pnl, brier["1.0"])
    k1 = {(p["market_id"], p["side"]) for p in open_positions(period, rule, acc) if p["market_id"] not in excluded}
    k2 = {(p["market_id"], p["side"]) for p in positions}
    ctx = {"period": period, "by_mkt": by_mkt, "outcomes": outcomes, "excluded": excluded, "acc": acc}
    rob = _robustness(ctx, v2, pm, start, end)
    rob["without_top3_weeks"] = without_top_weeks(pm)
    return {
        "model": MODEL_V2G,
        "t0": int(t0_v2g),
        "t0_v1": int(t0_v1),
        "now": int(now_ts),
        "period_end": end,
        "positions": len(pm),
        "entries": [(p["market_id"], p["side"], int(p["ts"])) for p in positions],
        "excluded_markets": sorted(excluded),
        "edge_mean": float(statistics.fmean(m["edge"] for m in pm)) if pm else None,
        "primary_clv_24h": primary,
        "clv_6h": _summ(pm, "clv_6h", "week_entry"),
        "clv_close": _summ(pm, "clv", "week_res"),
        "pnl": pnl,
        "brier_diff": brier,
        "verdict": v,
        "reason": reason,
        "vs_v1": {
            "same_as_v1": _block([m for m in pm if (m["market_id"], m["side"]) in k1]),
            "only_v2g": _block([m for m in pm if (m["market_id"], m["side"]) not in k1]),
            "only_v1": len(k1 - k2),
        },
        "robustness": rob,
    }


def render_verdict_v2g_own(f: Mapping[str, Any]) -> str:
    """Markdown (Ukrainian) of :func:`final_metrics_v2g_own`."""

    def row(name: str, s: Mapping[str, Any]) -> str:
        return (
            f"- {name}: n {s['n']}, тижнів {s['n_clusters']}, днів {s['n_days']}; середнє {_pp(s['mean'])}, "
            f"95 % CI [{_pp(s['lo'])}; {_pp(s['hi'])}]"
        )

    def blk(name: str, b: Mapping[str, Any]) -> str:
        c, p = b["clv_24h"], b["pnl"]
        return (
            f"- {name}: позицій {b['positions']}; CLV +24 год {_pp(c['mean'])} [{_pp(c['lo'])}; {_pp(c['hi'])}] (n {c['n']}); "
            f"P&L {_pp(p['mean'])} [{_pp(p['lo'])}; {_pp(p['hi'])}] (n {p['n']})"
        )

    vs = f["vs_v1"]
    lines = [
        "## Polymarket проти опціонів Deribit, варіант v2-g: вердикт власного форвард-тесту (один раз)",
        "",
        "Картка: docs/ideas/IDEA-20261101-polymarket-v2g-forward.md. Форвард = індекс Deribit × (1 + g), g = binance_last / spot − 1 "
        "того самого знімка. Позиції паперові, ордерів немає.",
        f"T0 v2-g = {_d(f['t0'])}, вхід до {_d(f['period_end'])}; обчислено {_d(f['now'])} (T0 v1 = {_d(f['t0_v1'])}). "
        f"Позицій {f['positions']}; виключено ринків (розбіжність з Binance): {', '.join(f['excluded_markets']) or 'немає'}.",
        "Застереження: ймовірності моделі ризик-нейтральні (k = 0.92 — лише груба знижка); g прибирає зсув форварда, а не премію "
        "за волу. Це не незалежне підтвердження v1: періоди перекриваються, позиції здебільшого спільні.",
        "",
        f"**Вердикт v2-g: {f['verdict']}** ({f['reason']})",
        "",
        row("CLV +24 год (основна, кластер — тиждень входу)", f["primary_clv_24h"]),
        row("CLV +6 год", f["clv_6h"]),
        row("CLV до закриття (кластер — тиждень розв'язання)", f["clv_close"]),
        row("P&L на акцію", f["pnl"]),
        row(
            f"Brier D, k = 1 (модель v2-g − Polymarket; > 0 — Polymarket точніший; ринків {f['brier_diff']['1.0']['markets']})",
            f["brier_diff"]["1.0"],
        ),
        row(f"Brier D, k = {VOL_HAIRCUT_K}", f["brier_diff"][str(VOL_HAIRCUT_K)]),
        f"- середній edge на вході {_pp(f['edge_mean'])}",
        "",
        "**Спільні з v1** (правило v1 на тих самих знімках періоду v2-g; описово)",
        blk("спільні з v1 (ринок, бік)", vs["same_as_v1"]),
        blk("лише v2-g", vs["only_v2g"]),
        f"- лише v1: {vs['only_v1']}",
        "",
        *render_robustness(f["robustness"]),
    ]
    return "\n".join(lines) + "\n"
