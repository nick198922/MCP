"""Collector health: share of the expected snapshots each collector stored over the last days (pure: no I/O, time
passed in).

Docs: docs/ideas/IDEA-20261102-standing-improvements.md, item 5. The forwards rest on these tables, so a silent gap is
lost weeks. For every read-only job switched on in ``deploy/features.env``, and for the jobs the collector runs by
default without a line there (``default_on``: ``atm_term``, ``basis``, ``funding``, ``supply``, ``traders``,
``trader_positions``; counted unless features.env sets their switch off), a :class:`Spec` says which table it writes,
how its rows map to a time ``stamp`` (unix seconds) and the cadence it promises: one snapshot per UTC-aligned bucket of
``period_s`` seconds, due ``due_s`` seconds after the bucket's start. A series (the job, or one of its ``groups``: a
currency, a venue, a strategy) is checked from the later of the window start and its first stored stamp, so a job
switched on recently is not counted for days before it existed; a bucket that is not due yet is not expected.

The cadences are read from the collectors and the schema (each spec's ``note`` says where). Assumptions:

* ``liquidations`` is a stream, not a snapshot: a venue's UTC hour counts when it has at least one liquidation. A calm
  hour may honestly have none (Bybit carries only BTC/ETH/SOL: 164 of 168 hours on 26.10-02.11), so its share can sit a
  little under 100 % without a fault.
* ``pm_gap`` ticks every 3600 s from the service start (not hour-aligned, plus the run time), so about one UTC hour in a
  hundred has no snapshot without a fault.
* ``option_xvenue`` reads the nearest daily expiry 2-20 h away, which exists only from 12:00 to 06:00 UTC: hours 06-11
  UTC are not expected. Only ``mode = 'hourly'`` rows count (minute rows are triggered, not scheduled).
* ``paper_carry``: a cycle (00/08/16 UTC) counts only with status ``done``; ``skipped`` (missed) and ``failed`` are gaps.
* ``trader_positions`` is a change log (store ``record_trader_positions``): an hour counts when at least one watched
  position changed. With ~200 Hyperliquid and ~200 Polymarket accounts watched that is every hour (167 of 168 hours for
  Hyperliquid on 29.10-05.11), but an hour without any change would read as a gap.
* ``traders`` and ``supply`` run every ``collect_*_seconds`` from the service start (not aligned to the day) and key
  their rows by the UTC day of the run; a day is due by its end.
* ``assistant_paper`` (a paper trade only on a trigger) and ``plan_resolve`` (scores existing plans, writes nothing in a
  quiet hour) have no cadence and are listed in :data:`NOT_CHECKED`.

Alarm (:func:`brief_line`): a series below :data:`THRESHOLD` of its expected buckets, a trailing gap (its latest
buckets missing for at least :data:`STALE_S`, or its latest daily / 8 h bucket, and no snapshot stored after them),
a series with no row at all, or a series that could not be read. A series that has written again in a bucket not due
yet is not "missing now": its run counts as an ordinary gap.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MINUTE = 60
HOUR = 3_600
DAY = 86_400
WINDOW_DAYS = 7
THRESHOLD = 0.90  # share of the expected snapshots below which the morning brief names the series
STALE_S = 3 * HOUR  # a trailing gap at least this long (hourly / minute series) is named even when the share is fine
MAX_BRIEF_ITEMS = 4  # series named in the brief's line; the rest is counted
_DAY_TS = "CAST(strftime('%s', {}) AS INTEGER)"  # a 'YYYY-MM-DD' column -> unix seconds of its 00:00 UTC


@dataclass(frozen=True)
class Spec:
    """One collector job's promise. ``ts_sql`` / ``group_col`` / ``where`` are SQL fragments the read-only loader
    (scripts/collector_health.py) puts into a SELECT; nothing here runs them."""

    name: str  # job name as in collector/scheduler.py
    flag: str  # environment switch of the job (config.Settings), as it would be set in deploy/features.env
    table: str
    ts_sql: str  # expression giving the row's stamp, unix seconds
    period_s: int  # one snapshot expected per UTC-aligned bucket of this length
    due_s: int  # seconds after a bucket's start by which its snapshot should be stored
    group_col: str | None = None
    groups: tuple[str, ...] = ("",)  # series checked separately ('' = the whole table)
    where: str = "1 = 1"
    hours_utc: frozenset[int] | None = None  # buckets expected only when their start hour (UTC) is in this set
    note: str = ""
    default_on: bool = False  # the collector runs it unless ``flag`` is set off (no line in features.env needed)


SPECS: tuple[Spec, ...] = (
    Spec(
        "pm_gap", "TRADEDESK_PM_GAP_ENABLED", "pm_gap_snapshot", "ts", HOUR, HOUR,
        note="job every 3600 s (collector/pm_gap.py); one snapshot per UTC hour",
    ),
    Spec(
        "paper_carry", "TRADEDESK_PAPER_CARRY_ENABLED", "cp_cycle", "cycle_ts / 1000", 8 * HOUR, 2 * HOUR,
        group_col="strategy", groups=("A", "B"), where="status = 'done'",
        note="funding cycles 00/08/16 UTC (cp_cycle.cycle_ts, ms); done ~10-20 min after; skipped/failed = gap",
    ),
    Spec(
        "forecast_log", "TRADEDESK_PAPER_FORECAST_LOG_ENABLED", "forecast_log", _DAY_TS.format("day"), DAY, 11 * HOUR,
        group_col="asset", groups=("BTC", "ETH"),
        note="one issue per UTC day and asset from 09:00 UTC (collector/forecast_log.py DECISION_HOUR_UTC), hourly job",
    ),
    Spec(
        "radar_archive", "TRADEDESK_COLLECT_RADAR_ENABLED", "radar_surface", _DAY_TS.format("day"), DAY, 22 * HOUR,
        group_col="asset", groups=("BTC", "ETH"),
        note="surface at 19:00 UTC, frozen after a 60 min lag (analytics/radar.due_days), hourly job",
    ),
    Spec(
        "option_xvenue", "TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED", "option_xvenue_snapshot", "ts", HOUR, HOUR,
        group_col="currency", groups=("BTC", "ETH"), where="mode = 'hourly'",
        hours_utc=frozenset((*range(0, 6), *range(12, 24))),
        note="hourly snapshot of the daily expiry 2-20 h away (TAU_MIN/MAX_HOURS): none 06-11 UTC",
    ),
    Spec(
        "tradfi_spread", "TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED", "tradfi_spread", "ts", MINUTE, 2 * MINUTE,
        note="one snapshot a minute (collect_tradfi_spread_seconds = 60)",
    ),
    Spec(
        "flow_snapshot", "TRADEDESK_COLLECT_FLOW_SNAPSHOT_ENABLED", "flow_snapshot", "ts", HOUR, HOUR,
        note="ts = start of the UTC hour; job ticks every 10 min",
    ),
    Spec(
        "liquidations", "TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED", "liquidations", "ts", HOUR, HOUR,
        group_col="venue", groups=("binanceusdm", "bybit", "okx"),
        note="WebSocket stream (VENUES=binance,bybit,okx); an hour counts with >= 1 liquidation; calm hours can be empty",
    ),
    Spec(
        "cycle_regime_log", "TRADEDESK_COLLECT_CYCLE_REGIME_ENABLED", "cycle_regime_log", _DAY_TS.format("date"), DAY,
        DAY + HOUR, group_col="base", groups=("BTC",),
        note="one row per closed UTC day, written from 00:05 UTC of the next day",
    ),
    Spec(
        "coinalyze_history", "TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED", "coinalyze_history", "ts", DAY,
        DAY + 3 * HOUR, group_col="symbol", groups=("BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "BTCUSDT.6", "ETHUSDT.6"),
        note="daily bar (ts = 00:00 UTC) first asked 02:00 UTC next day (SETTLE_SECONDS), hourly job; any metric counts",
    ),
    Spec(
        "coingecko_trending", "TRADEDESK_COLLECT_COINGECKO_TRENDING_ENABLED", "coingecko_trending", "ts", HOUR, HOUR,
        note="one snapshot per UTC hour (ts = its start), job ticks every 10 min",
    ),
    Spec(
        "option_legs", "TRADEDESK_COLLECT_OPTION_LEGS_ENABLED", "option_leg_snapshot", _DAY_TS.format("day"), DAY,
        10 * HOUR, group_col="currency", groups=("BTC", "ETH"),
        note="one snapshot per UTC day and currency from 09:00 UTC, job ticks every 10 min",
    ),
    Spec(
        "hl_oi_cap", "TRADEDESK_COLLECT_HL_OI_CAP_ENABLED", "hl_oi_cap_snapshot", "ts", HOUR, HOUR,
        note="one snapshot per UTC hour (ts = its start), job ticks every 10 min",
    ),
    # On by default with the collector (collector/scheduler.build_default_scheduler; no line in features.env).
    Spec(
        "atm_term", "TRADEDESK_COLLECTOR_ENABLED", "atm_term", "ts", HOUR, HOUR,
        group_col="currency", groups=("BTC", "ETH"), default_on=True,
        note="job every 300 s (collect_atm_term_seconds), ts = run time; a UTC hour counts with >= 1 snapshot",
    ),
    Spec(
        "basis", "TRADEDESK_COLLECTOR_ENABLED", "basis_hist", "ts", HOUR, HOUR,
        group_col="currency", groups=("BTC", "ETH"), default_on=True,
        note="job every 300 s (collect_basis_seconds), ts = run time; a UTC hour counts with >= 1 snapshot",
    ),
    Spec(
        "funding", "TRADEDESK_COLLECTOR_ENABLED", "funding_hist", "ts", HOUR, HOUR,
        group_col="venue", groups=("binanceusdm", "bybit", "okx", "hyperliquid", "deribit"), default_on=True,
        note="job every 3600 s from the service start (not hour-aligned, like pm_gap); a venue that fails is skipped silently",
    ),
    Spec(
        "supply", "TRADEDESK_COLLECT_SUPPLY_ENABLED", "supply_snapshot", _DAY_TS.format("day"), DAY, DAY, default_on=True,
        note="CoinGecko snapshot keyed by the UTC day of the run, job every 86400 s from the service start",
    ),
    Spec(
        "traders", "TRADEDESK_COLLECT_TRADERS_ENABLED", "trader_snapshot", _DAY_TS.format("day"), DAY, DAY,
        group_col="venue", groups=("hyperliquid", "polymarket", "polymarket_crypto"), default_on=True,
        note="leaderboards keyed by the UTC day of the run (collect_traders_seconds from the service start); one venue "
        "failing only logs a warning",
    ),
    Spec(
        "trader_positions", "TRADEDESK_COLLECT_TRADERS_ENABLED", "trader_positions", "ts", HOUR, HOUR,
        group_col="venue", groups=("hyperliquid", "polymarket"), default_on=True,
        note="hourly change log; an hour counts with >= 1 changed position; both Polymarket boards stored as 'polymarket'",
    ),
)  # fmt: skip

NOT_CHECKED: dict[str, str] = {
    "TRADEDESK_ASSISTANT_PAPER_ENABLED": "event-driven: a paper trade is booked only on a trigger, no cadence",
    "TRADEDESK_PAPER_PLAN_RESOLVE_ENABLED": "scores stored plans; writes nothing in an hour without a resolution",
}


def enabled_specs(switches: Mapping[str, str], specs: Iterable[Spec] = SPECS) -> list[Spec]:
    """The specs whose switch is on (``1`` / ``true`` / ``yes`` / ``on``) in ``switches`` (parsed features.env). A
    ``default_on`` spec counts when its switch is absent; set to anything else there, it is off (as config.Settings
    reads it). The owner's /etc/tradedesk-mcp.env is not read here."""

    def on(s: Spec) -> bool:
        v = switches.get(s.flag)
        return s.default_on if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")

    return [s for s in specs if on(s)]


def expected_buckets(spec: Spec, start: int, now: int) -> list[int]:
    """Starts of the buckets that should hold a snapshot: aligned to ``period_s``, starting at or after ``start``, due by
    ``now`` (start + ``due_s`` <= now) and, with ``hours_utc``, starting in one of those UTC hours."""
    p = spec.period_s
    b = -(-start // p) * p  # first aligned bucket at or after start
    out: list[int] = []
    while b + spec.due_s <= now:
        if spec.hours_utc is None or (b % DAY) // HOUR in spec.hours_utc:
            out.append(b)
        b += p
    return out


def series_health(
    spec: Spec,
    group: str,
    stamps: Iterable[int],
    first_ts: int | None,
    last_ts: int | None,
    now: int,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """Health of one series. ``stamps``: its row stamps (unix s) in the window; ``first_ts`` / ``last_ts``: its earliest
    and latest stamp ever (None = no row at all). Keys: ``label``, ``status`` ('ok' | 'low' | 'stale' | 'never' |
    'new'), ``share`` (present / expected; None when nothing is expected yet), ``expected``, ``present``, ``gaps``
    (runs of missing buckets), ``longest_gap_s``, ``longest_gap_start``, ``trailing_gap_s`` (the missing run that
    reaches the last expected bucket, 0 = none or when ``last_ts`` already lies in a later bucket that is not due yet,
    i.e. the series has recovered), ``since`` (first bucket checked), ``first_ts``, ``last_ts``."""
    label = f"{spec.name}:{group}" if group else spec.name
    p = spec.period_s
    out: dict[str, Any] = {
        "name": spec.name, "group": group, "label": label, "period_s": p, "first_ts": first_ts, "last_ts": last_ts,
        "share": None,
        "expected": 0, "present": 0, "gaps": 0, "longest_gap_s": 0, "longest_gap_start": None, "trailing_gap_s": 0,
        "since": None,
    }  # fmt: skip
    if first_ts is None:
        return out | {"status": "never"}
    start = max(now - window_days * DAY, (first_ts // p) * p)
    exp = expected_buckets(spec, start, now)
    have = {(int(s) // p) * p for s in stamps}
    out["since"] = exp[0] if exp else start
    if not exp:
        return out | {"status": "new"}
    runs: list[list[int]] = []  # [first missing bucket, number of buckets]
    prev_missing = False
    for b in exp:
        if b in have:
            prev_missing = False
            continue
        if prev_missing:
            runs[-1][1] += 1
        else:
            runs.append([b, 1])
        prev_missing = True
    present = len(exp) - sum(n for _, n in runs)
    longest = max(runs, key=lambda r: r[1]) if runs else None
    trailing = runs[-1][1] * p if runs and prev_missing else 0
    if trailing and last_ts is not None and (int(last_ts) // p) * p > exp[-1]:
        # A snapshot already sits in a later bucket that is not due yet: the series is back, so the run is a closed
        # gap (still in ``gaps`` / ``longest_gap_s``), not "missing now" (05.11: Polymarket traders after the v2 fix).
        trailing = 0
    share = present / len(exp)
    stale = trailing >= (p if p >= 8 * HOUR else STALE_S)
    status = "low" if share < THRESHOLD else ("stale" if stale else "ok")
    return out | {
        "status": status, "share": share, "expected": len(exp), "present": present, "gaps": len(runs),
        "longest_gap_s": longest[1] * p if longest else 0, "longest_gap_start": longest[0] if longest else None,
        "trailing_gap_s": trailing,
    }  # fmt: skip


def error_health(spec: Spec, group: str, why: str) -> dict[str, Any]:
    """A series that could not be read (table missing, query failed): status 'error' with the reason."""
    label = f"{spec.name}:{group}" if group else spec.name
    return {"name": spec.name, "group": group, "label": label, "status": "error", "error": why, "share": None}


def alarms(results: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The series the morning brief should name: below the threshold, a trailing gap, no row at all, unreadable."""
    return [r for r in results if r.get("status") in ("low", "stale", "never", "error")]


def fmt_dur(seconds: float) -> str:
    """Ukrainian duration: '45 хв', '6 год', '2.5 дн'."""
    if seconds < HOUR:
        return f"{max(1, round(seconds / MINUTE))} хв"
    if seconds < 2 * DAY:
        return f"{seconds / HOUR:.0f} год"
    return f"{seconds / DAY:.1f} дн"


def fmt_ts(ts: int | None, period_s: int = HOUR) -> str:
    """'02.11 05:00 UTC' (or '02.11' for daily series); '—' without a time."""
    if ts is None:
        return "—"
    d = datetime.fromtimestamp(ts, UTC)
    return d.strftime("%d.%m") if period_s >= DAY else d.strftime("%d.%m %H:%M UTC")


def _pct(x: float) -> str:
    """Share in whole percent, rounded down so 0.899 never shows as 90 %."""
    return f"{math.floor(x * 100 + 1e-9)} %"


def describe(r: Mapping[str, Any]) -> str:
    """One series in Ukrainian for the brief: why it is named and its last snapshot."""
    st, label = r.get("status"), str(r.get("label"))
    if st == "error":
        return f"{label} не перевірено ({r.get('error')})"
    if st == "never":
        return f"{label}: жодного запису"
    p = int(r.get("period_s") or HOUR)
    parts = [f"{label} {_pct(r['share'])}"]
    if r.get("trailing_gap_s"):
        parts.append(f"немає {fmt_dur(r['trailing_gap_s'])} поспіль")
    elif r.get("longest_gap_s"):
        parts.append(f"дір {r['gaps']}, найдовша {fmt_dur(r['longest_gap_s'])}")
    parts.append(f"останній {fmt_ts(r.get('last_ts'), p)}")
    return parts[0] + " (" + "; ".join(parts[1:]) + ")"


def brief_line(results: Iterable[Mapping[str, Any]], window_days: int = WINDOW_DAYS) -> str | None:
    """The morning brief's line (plain text, Ukrainian) naming at most :data:`MAX_BRIEF_ITEMS` alarmed series, the
    lowest share first; None when every series is fine."""
    bad = sorted(alarms(results), key=lambda r: (r.get("share") is not None, r.get("share") or 0.0))
    if not bad:
        return None
    named = "; ".join(describe(r) for r in bad[:MAX_BRIEF_ITEMS])
    more = f"; і ще {len(bad) - MAX_BRIEF_ITEMS}" if len(bad) > MAX_BRIEF_ITEMS else ""
    return f"Колектори: частка знімків за {window_days} дн нижче {_pct(THRESHOLD)} або свіжа дірка — {named}{more}."


def render(results: Iterable[Mapping[str, Any]], now: int, window_days: int = WINDOW_DAYS) -> str:
    """Full text report (Ukrainian), one line per series."""
    rows = list(results)
    out = [f"Здоров'я колекторів на {fmt_ts(now)} · вікно {window_days} дн · поріг {_pct(THRESHOLD)}", ""]
    for r in rows:
        st = r.get("status")
        if st in ("error", "never"):
            out.append(f"✗ {describe(r)}")
            continue
        p = int(r.get("period_s") or HOUR)
        if st == "new":
            out.append(f"· {r['label']}: ще нічого не очікується (перший запис {fmt_ts(r.get('first_ts'), p)})")
            continue
        mark = "✓" if st == "ok" else "✗"
        line = f"{mark} {r['label']}: {_pct(r['share'])} ({r['present']}/{r['expected']} з {fmt_ts(r.get('since'), p)})"
        if r.get("gaps"):
            line += f", дір {r['gaps']}, найдовша {fmt_dur(r['longest_gap_s'])} з {fmt_ts(r.get('longest_gap_start'), p)}"
        if r.get("trailing_gap_s"):
            line += f", зараз немає {fmt_dur(r['trailing_gap_s'])}"
        out.append(line + f"; останній {fmt_ts(r.get('last_ts'), p)}")
    line = brief_line(rows, window_days)
    out += ["", f"У брифі: {line}" if line else "У брифі: нічого (усі серії в нормі)."]
    return "\n".join(out) + "\n"
