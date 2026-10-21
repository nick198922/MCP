"""Weekly DESCRIPTIVE report and the one-time verdict of the Polymarket <-> Deribit paper test.

Docs: docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md, section "Реєстрація". Pure functions over
``pm_gap_snapshot`` rows and ``pm_gap_outcome`` rows (dicts with the tables' column names); no I/O, every time is a
unix-seconds parameter. The rules and metrics themselves live in :mod:`.pm_gap_paper` and are used unchanged here.

What the weekly report may show is fixed by registration section 1: counts of snapshots and positions, resolution days
and weeks, shares of the closing-line sources (close / late / missing) and of ``missing`` at +24 h, SD per position, the
share of snapshots dropped for quote skew and of rows without read times. It must NOT show the mean of the main metric,
its CI, the P&L or the Brier comparison before the verdict: :func:`weekly_descriptive` never computes them, and
:func:`final_metrics` refuses to run before :func:`verdict_ts`.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .pm_gap_paper import (
    HORIZON_TOL_S,
    HOUR_S,
    PRIMARY_H,
    SECONDARY_H,
    VOL_HAIRCUT_K,
    EntryRule,
    accepted_rule_hashes,
    brier_by_market,
    closing_value,
    cluster_mean_ci,
    eligible,
    horizon_clv,
    open_positions,
    position_metrics,
    verdict,
)

DAY_S = 86_400
MAIN_PERIOD_DAYS = 112  # registration section 1: positions opened in [T0, T0 + 112 days) form the main test
VERDICT_DAYS = 126  # the verdict is taken once, at T0 + 126 days (last main-period market resolved by then)


def main_period(t0: int) -> tuple[int, int]:
    """[start, end) of the entry period of the main test, unix seconds."""
    return int(t0), int(t0) + MAIN_PERIOD_DAYS * DAY_S


def verdict_ts(t0: int) -> int:
    """The single moment the verdict may be computed: T0 + 126 days."""
    return int(t0) + VERDICT_DAYS * DAY_S


# ----------------------------------------------------------------------------- settled results


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
    (cluster = resolution week); k = 0.92 alongside. Robustness and stress (sections 6 and 12) are not computed here."""
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
        "Стійкість і стрес (розділи 6 і 12) тут не пораховано.",
    ]
    return "\n".join(lines) + "\n"
