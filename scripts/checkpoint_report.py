"""Checkpoint 2026-11-30 in one report: every forward test at a date, its registered backtest band and a label
«рано» / «узгоджується» / «ні» (docs/CHECKPOINT-2026-11-30.md; item 2 of IDEA-20261102-standing-improvements).

    uv run python scripts/checkpoint_report.py [--db PATH] [--date YYYY-MM-DD] [--out FILE]

``--date``: the state at the end of that UTC day (default: now; never later than the wall clock). Reads the collector
DB through read-only connections (``mode=ro``: no migration, no write), no network, no keys; nothing here places,
changes or cancels an order and nothing under ``execution/`` is imported. The rules live in the pure
``tradedesk.analytics.checkpoint``. Forwards under a no-peeking embargo (Polymarket v1 / v2-g, the condor P&L) show
counters only, and the condor's ``option_leg_snapshot`` is not read before its registered T0 (2026-11-06 10:00 UTC).
The paper engine ``assistant_paper`` (no band in CHECKPOINT) shows counters and its job's last daily decision only:
``rec_paper`` is read without its P&L columns, and the state file ``<DB dir>/assistant/paper_state.json`` read-only.
The idea cards with ``Статус: forward`` (``docs/ideas/``) are listed with the count of their ``idea:`` plans in
``trade_plan``, read without the R / outcome / price columns. The cards whose own journal is a collector table
(``CARD_JOURNALS``: the table each card names) get one more row with counters only: observations to date, the first and
last one, the minimum for the verdict and its date as the card writes them; no price, event or result is read. Cards
with neither a fresh journal nor an ``idea:`` plan are listed as «форвард без збору» (item 312). A journal that differs
from the card's registration says so (touch ladder: 5–10 min earlier, top of book only), and the HLP journal is labelled
«частково» until a run in which every HLP vault answered (``trader_polls``, venue ``hyperliquid_hlp``) is fresh and the
OI / funding table is fresh too, the days before its first complete run staying partial and missed hours named (item 472).
Demo trades still ``open`` more than a day past their hold get a closing note with their count
(``tradedesk.analytics.stuck_demo``, item 133): they are missing from the closed samples. So are the trades the
demo trader set aside as ``orphaned`` (filled, no position and no close found on the venue): a note names them (item 326).
Closed demo trades whose recorded P&L disagrees with ``(exit - entry) * qty`` minus fees get a note «Угоди з розбіжним
P&L: N» (``tradedesk.analytics.demo_pnl_check``, item 140; only when N > 0): in the trades checked on 2026-11-26 the gap
was funding for the holding time (docs/verification/2026-11-26-demo-pnl-mismatch.md), a newly flagged one is unproven.
Each demo setup row with known fees notes the demo's fees against the backtest fee model (mean ``fee_gap_r``, taker
entries; ``tradedesk.analytics.checkpoint.demo_fee_note``, item 341), descriptive only. Each demo setup row with n >= 30
also notes the 95 % CI of its mean R clustered by the signal's UTC day, descriptive as CHECKPOINT registers it (item 343).
The section «S12: правило зняття» applies the owner's rule Q-20261109-1 to the closed S12 demo trades (v9: n >= 30 and
mean R below -0.827 -> stop and review the execution; v4 entry, v4 + v9: n >= 100 and below -0.524R -> drop; else
«рано» or «тримати»), with n, mean R and a 95 % CI (``tradedesk.analytics.checkpoint.s12_stop_rule``, item 313).
The carry rows also count criteria (e) and (ye) (item 365): the dated Bybit ladder snapshots of ``docs/paper/ladder/``
and of the weekly timer's directory (``TRADEDESK_LADDER_DIR``, default ``/var/lib/tradedesk-mcp/ladder``; item 366)
are read for the Bybit leg's 2x position limit (files only, no network). The carry B row notes the recorded Bybit
funding-interval mismatches (``interval_mismatches`` in the cycle payload, written from the 26.11 00:00 UTC cycle on)
and how many missed-payment flags fall on a mismatched symbol (``checkpoint.interval_mismatch_notes``, item 477).
The text (Ukrainian) is printed and, with ``--out``, written to a file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common_helpers as cmn  # noqa: E402

from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, carry_side_cost  # noqa: E402
from tradedesk.analytics.checkpoint import (  # noqa: E402
    COUNTS_ONLY,
    build,
    condor_final_ms,
    forward_cards,
    orphaned_note,  # pure, shared with the demo_status MCP tool (item 409)
    render,
)
from tradedesk.analytics.condor_forward import EARLIEST_T0, ENTRY_CUTOFF_HOUR_UTC, cost_fridays  # noqa: E402
from tradedesk.analytics.pm_gap_report import T0_TS, T0_V2G_TS, verdict_ts, verdict_ts_v2g  # noqa: E402
from tradedesk.collector.carry_paper_engine import PARAMS_A  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
BENCH = ROOT / "docs" / "paper" / "bench.json"
LADDER = ROOT / "docs" / "paper" / "ladder"  # dated Bybit ladder snapshots (scripts/paper_ladder_snapshot.py)
# Weekly snapshots of the user timer deploy/user-units/tradedesk-ladder-weekly.* (outside git); TRADEDESK_LADDER_DIR overrides.
LADDER_SERVER = "/var/lib/tradedesk-mcp/ladder"
IDEAS = ROOT / "docs" / "ideas"
CARRY_KINDS = ("side", "funding", "mark", "close", "event")
# Every table a forward block reads. The migrations create them all, so an absent one means an older schema or another
# DB file: the report names it instead of showing the block as if it merely had no rows yet.
FORWARD_TABLES = (
    "demo_trade", "cp_position", "cp_log", "cp_cycle", "rec_paper", "pm_gap_snapshot", "pm_gap_outcome", "option_leg_snapshot",
    "trade_plan",
)  # fmt: skip


@dataclass(frozen=True)
class Part:
    """Another table a card's journal needs (``also``) or the complete record its registration names (``full``):
    ``sql`` as ``Journal.sql`` (two ``?``; observations, first, last unix s), ``cols`` the names of its further columns."""

    table: str
    sql: str
    unit: str
    cols: tuple[str, ...] = ()


@dataclass(frozen=True)
class Journal:
    """A forward card's own journal, as the card names it. ``sql`` returns one row (observations, first, last unix s)
    for ``since <= ts <= now`` (two ``?``, unix s); ``{+N}`` in ``verdict`` is the date of the first observation + N days.
    An optional fourth column counts every row of the window when the first three count only the good ones (carry:
    ``status = 'done'``), so a journal of failed / skipped cycles is neither fresh nor hidden.

    ``note``: a deviation of the collected data from the card's registration, printed with the counters. ``also``:
    further tables the card needs, counted alike. ``full``: the complete record the registration names (HLP: runs in
    which every vault answered); the journal is «частково» (``partial`` says why) while ``full`` has no observation or
    a stale one, or an ``also`` table is missing, empty or stale; the days before the first complete observation stay
    partial, and spells of more than ``GAP_S`` without one (the tail up to the report moment too) are named.
    ``{F+N}`` in ``verdict`` is the date of the first observation of ``full`` + N days (none: no date)."""

    table: str
    sql: str
    unit: str
    minimum: str
    verdict: str
    since: int = 0  # a registered T0 (unix s): observations before it belong to no forward of the card
    note: str = ""
    also: tuple[Part, ...] = ()
    full: Part | None = None
    partial: str = ""


def _utc(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, UTC).strftime(fmt)


_PM_SQL = "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM pm_gap_snapshot WHERE ts >= ? AND ts <= ?"
_PM_MIN = "16 тижнів входів; ≥ 20 днів розв'язання і ≥ 10 тижневих кластерів (позиції — рядки Polymarket вище)"
# HLP (card hlp-inventory-crowding): the parent vault and its 7 child vaults as ``vaultDetails`` listed them on
# 2026-11-26 (tests/fixtures/hyperliquid_vault_details_hlp_2026-11-26.json). Before item 471 a vault was recorded only
# while it was in the top 100 by pnl_all, under venue 'hyperliquid'; from item 471 on, every vault each run under
# ``HLP_VENUE`` (collector.scheduler.HLP_VENUE, not imported: the scheduler pulls the whole collector in).
HLP_VENUE = "hyperliquid_hlp"
HLP_VAULTS = (
    "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",  # parent
    "0x010461c14e146ac35fe42271bdc1134ee31c703a", "0x2e3d94f0562703b25c83308a05046ddaf9a8dd14",
    "0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308", "0x31ca8395cf837de08b24da3f660e77761dfb974b",
    "0x469f690213c467c39a23efacfd2816896009d7d8", "0x5e177e5e39c0f4e421f5865a6d8beed8d921cb70",
    "0xb0a55f13d22f66e6d495ac98113841b2326e9540",
)  # fmt: skip
_HLP_IN = ", ".join(f"'{a}'" for a in HLP_VAULTS)
# The cards whose own forward journal is a collector table (docs/ideas/IDEA-*-<slug>.md, «Форвард» / «Прогрес» / «Реєстрація»).
# Every other forward card names only ``idea:`` plans (or a journal not built yet): its journal is the plan row above.
CARD_JOURNALS: dict[str, Journal] = {
    "bybit-deribit-daily-option-dislocation-forward": Journal(
        "option_xvenue_snapshot",
        "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM option_xvenue_snapshot "
        "WHERE currency = 'BTC' AND mode = 'hourly' AND ts >= ? AND ts <= ?",
        "годинних знімків BTC",
        "30 днів для кілла (знаменник p ≥ 300 знімків класу A); 90 днів, знаменник ≥ 900 і ≥ 30 паперових угод для вердикту",
        "кілл не раніше {+30}, остаточний не раніше {+90}",
    ),
    "coingecko-trending-attention-fade-forward": Journal(
        "coingecko_trending", "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM coingecko_trending WHERE ts >= ? AND ts <= ?",
        "годинних знімків trending",
        "перше читання n = 40 подій, вердикт n = 150 (малий ефект — 300); події визначає крок 1", "крок 1 (тест) ≈ {+90}",
    ),
    "hlp-inventory-crowding": Journal(
        "trader_positions",
        "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM trader_positions WHERE "
        f"(venue = '{HLP_VENUE}' OR (venue = 'hyperliquid' AND account IN ({_HLP_IN}))) AND ts >= ? AND ts <= ?",
        "запусків із рядками позицій сховищ HLP (журнал змін і щозапускові рядки ≥ $2.5 млн)",
        "≥ 180 діб щогодинних знімків позицій кожного сховища HLP (з ліквідаторами), OI і funding",
        "≥ 180 діб повного запису: не раніше {F+180}",
        also=(
            Part(
                "hl_oi_cap_snapshot", "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM hl_oi_cap_snapshot WHERE ts >= ? AND ts <= ?",
                "годинних знімків OI / funding",
            ),
        ),
        full=Part(
            "trader_polls",
            # complete run ('whole'): every polled vault answered and at least len(HLP_VAULTS) = 8 were polled.
            # Complete runs, first / last, spells > GAP_S = 5400 s between consecutive complete runs and the longest one,
            # all runs, fewest / most vaults polled in a run (literal 8 and 5400: tested against the constants)
            "SELECT COUNT(CASE WHEN whole THEN 1 END), MIN(CASE WHEN whole THEN ts END), MAX(CASE WHEN whole THEN ts END), "
            "SUM(whole AND gap > 5400), MAX(CASE WHEN whole THEN gap END), COUNT(*), MIN(polled), MAX(polled) FROM ("
            "SELECT ts, polled, whole, ts - LAG(ts) OVER (PARTITION BY whole ORDER BY ts) AS gap FROM ("
            "SELECT ts, COUNT(*) AS polled, SUM(ok) = COUNT(*) AND COUNT(*) >= 8 AS whole FROM trader_polls "
            "WHERE venue = 'hyperliquid_hlp' AND ts >= ? AND ts <= ? GROUP BY ts))",
            f"повних запусків (venue = '{HLP_VENUE}': ≥ {len(HLP_VAULTS)} сховищ опитано, усі ok = 1)",
            cols=("gaps", "longest", "runs", "fewest", "most"),
        ),
        partial=(
            "лише сховища HLP, що випадково потрапляли в топ-100 за pnl_all (venue = 'hyperliquid'); три з семи дочірніх "
            "так не опитувались жодного разу (звірка 26.11)"
        ),
    ),
    "hl-oi-cap-state-forward": Journal(
        "hl_oi_cap_snapshot", "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM hl_oi_cap_snapshot WHERE ts >= ? AND ts <= ?",
        "годинних знімків", "ворота G1/G2 після 14 діб збору; вердикт H1 при n ≥ 250 подій (події визначає крок 1)",
        "ворота G1/G2 {+14}; H1 — коли n ≥ 250 подій (дати в картці нема)",
    ),
    "hl-whale-liquidation-magnet": Journal(
        "trader_positions",
        "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM trader_positions WHERE venue = 'hyperliquid' AND ts >= ? AND ts <= ?",
        "запусків із рядками позицій HL (зміни, з 25.11 і щозапускові рядки позицій ≥ $2.5 млн; liq_watch кроку 1 не збудовано)",
        "крок 0: ≥ 5 епізодів за 10 днів; вердикт при ≥ 90 подіях (епізоди визначає крок 0)",
        "дати в картці нема (≈ 12 міс. на 60 подій)",
    ),
    "polymarket-static-noarb-ladders": Journal(
        "pm_gap_snapshot", _PM_SQL, "годинних знімків", "30 днів і ≥ 300 пар з rules_standard = 1 (пари рахує крок 0)",
        "H1 не раніше {+30}",
    ),
    "polymarket-v2g-forward": Journal(
        "pm_gap_snapshot", _PM_SQL, "годинних знімків від T0 v2-g", _PM_MIN,
        "не раніше {V2G} UTC", since=T0_V2G_TS,
    ),
    "polymarket-vs-options-forward": Journal(
        "pm_gap_snapshot", _PM_SQL, "годинних знімків від T0", _PM_MIN, "не раніше {VOPT} UTC", since=T0_TS,
    ),
    "polymarket-touch-ladder-event-days": Journal(
        "pm_gap_snapshot",
        # the card's ladder is the daily one («What price will Bitcoin hit on <date>?», window 00:00 ET + 1 day), not the weekly
        "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM pm_gap_snapshot WHERE kind = 'touch' AND asset = 'BTC' "
        "AND ROUND((end_ts - window_start) / 86400.0) = 1 AND ts >= ? AND ts <= ?",
        "знімків (запусків колектора, ≈ щогодини) денних драбин BTC на дотик (kind = 'touch')",
        "перший погляд при 24 днях подій CPI / NFP (≈ 12 міс.), остаточний при ≥ 40 (≈ 20 міс.); дні подій і 2 контрольні "
        "дні на місяць визначає крок аналітика картки",
        "перший погляд ≈ {+365}",
        note=(
            "відхилення від реєстрації: знімки біля 00:00 і 07:00 ET спостережено о :20–:25 (02–26.11; фаза від старту "
            "служби, а не розклад), тобто на 5–10 хв раніше за зареєстровані 00:30 і 07:30 ET, і лише верх книги "
            "(best bid / ask і USD на ньому), без last і без глибини"
        ),
    ),
    "forward-paper-carry": Journal(
        "cp_cycle",
        # only 'done' cycles count and set the freshness (an engine failing every cycle collects nothing); all statuses aside
        "SELECT COUNT(*) FILTER (WHERE status = 'done'), MIN(cycle_ts) FILTER (WHERE status = 'done') / 1000, "
        "MAX(cycle_ts) FILTER (WHERE status = 'done') / 1000, COUNT(*) "
        "FROM cp_cycle WHERE cycle_ts >= ? * 1000 AND cycle_ts <= ? * 1000",
        "циклів A і B зі статусом done", "критерії CHECKPOINT (рядки «Папір carry» вище)", "чекпоінт 2026-11-30",
    ),
}  # fmt: skip
JOURNALS_NAME = "Картки forward: власні журнали"
STALE_S = 2 * 86_400  # every journal above writes at least every 8 h: two silent days = not collecting
GAP_S = 5_400  # hourly complete record (HLP): more than 90 min between complete runs = a missed hour


def connect_ro(db: str) -> sqlite3.Connection:
    """A read-only connection; raises ``sqlite3.Error`` / ``FileNotFoundError`` when the DB is not there."""
    if not Path(db).exists():
        raise FileNotFoundError(db)
    con = cmn.connect_ro(Path(db))
    con.row_factory = sqlite3.Row
    return con


def _json(x: Any) -> Any:
    if not isinstance(x, str):
        return x
    try:
        return json.loads(x)
    except ValueError:
        return None


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def load_demo(con: sqlite3.Connection) -> list[dict[str, Any]]:
    if "demo_trade" not in _tables(con):
        return []
    return [dict(r) for r in con.execute("SELECT * FROM demo_trade ORDER BY signal_ts")]


def load_carry(con: sqlite3.Connection, strategy: str) -> dict[str, Any]:
    names = _tables(con)
    out: dict[str, Any] = {"positions": [], "logs": {k: [] for k in CARRY_KINDS}, "cycles": []}
    if "cp_position" in names:
        for r in con.execute("SELECT * FROM cp_position WHERE strategy = ? ORDER BY opened_ts, trade_id", (strategy,)):
            d = dict(r)
            d["state"], d["trade"] = _json(d.get("state")) or {}, _json(d.get("trade"))
            out["positions"].append(d)
    if "cp_log" in names:
        q = "SELECT cycle_ts, kind, trade_id, payload FROM cp_log WHERE strategy = ? AND kind = ? ORDER BY id"
        for k in CARRY_KINDS:
            out["logs"][k] = [{**dict(r), "payload": _json(r["payload"]) or {}} for r in con.execute(q, (strategy, k))]
    if "cp_cycle" in names:
        q = "SELECT strategy, cycle_ts, status, reason, finished_ms, payload FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts"
        out["cycles"] = [{**dict(r), "payload": _json(r["payload"]) or {}} for r in con.execute(q, (strategy,))]
    return out


def load_rec_paper(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """The assistant_paper rows without any P&L column: the report shows counters only."""
    if "rec_paper" not in _tables(con):
        return []
    q = "SELECT id, opened_ts, expiry_ts, status, source, trigger FROM rec_paper ORDER BY opened_ts, id"
    return [dict(r) for r in con.execute(q)]


def load_plans(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """The trade plans tagged ``idea:…`` without any R, outcome or price column: the report shows counters only."""
    if "trade_plan" not in _tables(con):
        return []
    q = "SELECT id, created_ts, resolved_ts, verdict, status, tags FROM trade_plan WHERE tags LIKE '%idea:%' ORDER BY id"
    return [{**dict(r), "tags": _json(r["tags"]) or []} for r in con.execute(q)]


def _count(con: sqlite3.Connection, names: set[str], table: str, sql: str, since: int, now_s: int, cols: Sequence[str]) -> dict[str, Any]:
    """``{"n", "first", "last"}`` of one journal query (plus its further columns named by ``cols``), ``{"absent": True}``
    without its table, ``{"error": name}`` when it fails."""
    if table not in names:
        return {"absent": True}
    try:
        n, first, last, *rest = con.execute(sql, (since, now_s)).fetchone()
    except sqlite3.Error as e:
        return {"error": type(e).__name__}
    return {"n": int(n or 0), "first": first, "last": last} | {c: int(v or 0) for c, v in zip(cols, rest, strict=False)}


def load_journals(con: sqlite3.Connection, now_ms: int) -> dict[str, dict[str, Any]]:
    """Per card of ``CARD_JOURNALS``: ``{"n", "first", "last"}`` up to ``now_ms`` (plus ``"all"`` when the query has a
    fourth column), ``{"absent": True}`` without its table, or ``{"error": name}`` when its query fails (one card's
    failure does not hide the others); a journal with ``also`` / ``full`` parts adds ``"also"`` (a list, same shape) and
    ``"full"`` (same shape, its further columns by ``Part.cols``). Counters only."""
    names, out, now_s = _tables(con), {}, now_ms // 1000
    for slug, j in CARD_JOURNALS.items():
        out[slug] = _count(con, names, j.table, j.sql, j.since, now_s, ("all",))
        if not out[slug].get("n"):  # the parts are shown only next to a journal with observations
            continue
        if j.also:
            out[slug]["also"] = [_count(con, names, p.table, p.sql, j.since, now_s, p.cols) for p in j.also]
        if j.full is not None:
            out[slug]["full"] = _count(con, names, j.full.table, j.full.sql, j.since, now_s, j.full.cols)
    return out


def _plan_count(card: Mapping[str, Any], plans: Sequence[Mapping[str, Any]], now_s: int) -> int:
    tags = card.get("tags") or frozenset()
    ok = [p for p in plans if isinstance(c := p.get("created_ts"), (int, float)) and not isinstance(c, bool) and c <= now_s]
    return sum(1 for p in ok if {t for t in (p.get("tags") or []) if isinstance(t, str)} & tags)


def _part_text(p: Part, got: Mapping[str, Any]) -> str:
    """One ``also`` table of a journal by counters: observations, first and last."""
    if not got:
        return f"{p.table}: не прочитано"
    if got.get("absent"):
        return f"{p.table}: таблиці в цій БД нема"
    if "error" in got:
        return f"{p.table}: запит не вдався ({got['error']})"
    if not got.get("n"):
        return f"{p.table}: {p.unit} 0"
    return f"{p.table}: {p.unit} {got['n']}, з {_utc(int(got['first']))} до {_utc(int(got['last']))} UTC"


def _full_text(j: Journal, full: Mapping[str, Any], also: Sequence[Mapping[str, Any]], now_s: int, text: str) -> tuple[str, str]:
    """(prefix, text) of a journal whose registration names a complete record (``j.full``, HLP: runs in which every
    vault answered). «частково — » while there is no complete run, the last one is older than ``STALE_S`` or an
    ``also`` table is missing, empty or stale; else «повний запис з <first complete run> — », «(з пропусками)» when
    consecutive complete runs (or the last one and the report moment) are more than ``GAP_S`` apart. Runs, not hours,
    are compared by time: extra runs after a restart do not cover a missed hour. The days before the first complete run
    are named as partial, so the label never covers more than the complete record does."""
    p = j.full
    assert p is not None
    # an also table unread, missing, empty or stale (its counters follow in the text)
    lack = [q.table for q, g in zip(j.also, also, strict=True) if not (g.get("n") and now_s - int(g["last"]) <= STALE_S)]
    need = f"; для повного запису бракує свіжого {', '.join(lack)} (лічильники далі)" if lack else ""
    polled = f"опитано сховищ за запуск {full.get('fewest', 0)}–{full.get('most', 0)}"
    if full.get("absent") or "error" in full or not full.get("n"):
        state = (
            "не прочитано" if not full
            else f"таблиці {p.table} у цій БД нема" if full.get("absent")
            else f"запит до {p.table} не вдався ({full['error']})" if "error" in full
            else f"0 з {full['runs']} запусків ({polled})" if full.get("runs")
            else "у БД ще нема"
        )  # fmt: skip
        return "частково — ", f"{text}; {j.partial}; повного запису ({p.table}: {p.unit}) {state}{need}"
    f0, f1, n = int(full["first"]), int(full["last"]), int(full["n"])
    tail = now_s - f1
    gaps = int(full.get("gaps", 0)) + (tail > GAP_S)
    longest = max(int(full.get("longest", 0)), tail)
    got = f"{p.table}: {p.unit} {n} з {full.get('runs', n)} запусків, з {_utc(f0)} до {_utc(f1)} UTC, {polled}"
    if gaps:
        got += f"; розривів між повними запусками понад {GAP_S // 60} хв (з хвостом до моменту звіту) {gaps}, найдовший {longest / 3600:.1f} год"
    before = f"до {_utc(f0)} UTC — частково: {j.partial}"
    if tail > STALE_S:
        return "частково — ", f"{text}; повний запис зупинився ({got}){need}; {before}"
    if lack:
        return "частково — ", f"{text}; повний запис {got}{need}; {before}"
    holes = " (з пропусками)" if gaps else ""
    return f"повний запис з {_utc(f0)} UTC{holes} — ", f"{text}; повний запис {got}; {before}"


def journal_row(
    cards: Sequence[Mapping[str, Any]], journals: Mapping[str, Mapping[str, Any]] | None, plans: Sequence[Mapping[str, Any]], now_ms: int
) -> dict[str, Any]:
    """One report row: the forward cards' own journals by counters (observations to date, first / last, minimum and
    verdict date as the card writes them) and «форвард без збору»: no fresh journal and no ``idea:`` plan. ``journals``
    None = the loader could not read the DB (each journal card is then named as unread, not as empty)."""
    now_s = now_ms // 1000
    counters: dict[str, Any] = {}
    idle: list[str] = []
    collecting = partial = 0
    for card in cards:
        slug, j = card["slug"], CARD_JOURNALS.get(card["slug"])
        got = (journals or {}).get(slug) if j is not None else None
        fresh, why = False, None  # why: the reason it collects nothing; None with fresh False = unknown (journal unread)
        if j is None:
            why = "журналу колектора нема, планів idea: 0"
        elif got is None or "error" in got:
            reason = "БД не прочитано" if got is None else f"запит до {j.table}: {got['error']}"
            counters[slug] = f"журнал {j.table}: {reason}; мінімум: {j.minimum}"
        elif got.get("absent"):
            counters[slug] = f"журналу {j.table} у цій БД нема; мінімум: {j.minimum}"
            why = f"журналу {j.table} у БД нема"
        elif not got["n"]:
            other = f" (усіх записів {got['all']}, жодного зарахованого)" if got.get("all") else ""
            counters[slug] = f"журнал {j.table}: {j.unit} 0{other}; мінімум: {j.minimum}"
            why = f"у журналі {j.table} {j.unit} 0 з {got['all']} записів" if other else f"журнал {j.table} порожній"
        else:
            first, last = int(got["first"]), int(got["last"])
            verdict = re.sub(r"\{\+(\d+)\}", lambda m, t0=first: _utc(t0 + int(m.group(1)) * 86_400, "%Y-%m-%d"), j.verdict)
            verdict = verdict.replace("{V2G}", _utc(verdict_ts_v2g(T0_V2G_TS, T0_TS))).replace("{VOPT}", _utc(verdict_ts(T0_TS)))
            full = got.get("full") or {}
            if "{F+" in verdict:
                if full.get("n"):
                    f0 = int(full["first"])
                    verdict = re.sub(r"\{F\+(\d+)\}", lambda m, t0=f0: _utc(t0 + int(m.group(1)) * 86_400, "%Y-%m-%d"), verdict)
                else:
                    verdict = "дату не визначено, доки нема повного запису"
            other = f" (усіх записів {got['all']})" if "all" in got else ""
            text = f"журнал {j.table}: {j.unit} {got['n']}{other}, з {_utc(first)} до {_utc(last)} UTC"
            also = got.get("also") or [{}] * len(j.also)
            head, text = _full_text(j, full, also, now_s, text) if j.full is not None else ("", text)
            for p, g in zip(j.also, also, strict=True):
                text += f"; {_part_text(p, g)}"
            if j.note:
                text += f"; {j.note}"
            counters[slug] = f"{head}{text}; мінімум: {j.minimum}; вердикт: {verdict}"
            # a fresh complete record counts too: unchanged vault positions under $2.5M write no trader_positions row
            fresh = now_s - max(last, int(full["last"]) if full.get("n") else 0) <= STALE_S
            done = " зі статусом done" if "all" in got else ""
            why = None if fresh else f"останній запис {j.table}{done} {_utc(last)} UTC"
            partial += int(fresh and head.startswith("частково"))
        if fresh or _plan_count(card, plans, now_s):  # idea: plans are a journal too (the row above)
            collecting += 1
        elif why is not None:
            idle.append(f"{slug} ({why})")
    if idle:
        counters[f"форвард без збору ({len(idle)})"] = "; ".join(idle)
    of_them = f" (з них частково {partial})" if partial else ""
    row: dict[str, Any] = {
        "key": "ideas:journals", "group": "ideas", "name": JOURNALS_NAME, "n": collecting, "metric": None, "band": None,
        "metric_name": f"лише лічильники; карток {len(cards)}, збирають дані {collecting}{of_them}", "verdict": COUNTS_ONLY,
        "side": None, "embargo": False, "counters": counters, "criteria": [], "notes": [],
    }  # fmt: skip
    if not cards:
        row["notes"].append("карток зі статусом forward не знайдено (каталог docs/ideas не прочитано?)")
    row["notes"].append(
        "журнал — таблиця колектора, яку називає сама картка; лише кількість спостережень і їхні дати, без цін, подій і "
        "результатів (події з журналу визначає крок аналітика картки). n — карток, що збирають дані: свіжий журнал "
        f"(останній запис не старший за {STALE_S // 86_400} доби) або хоч один план idea: (рядок вище)"
    )
    return row


def build_report(inputs: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    """``checkpoint.build`` plus the row of the cards' own journals (``journal_row``) and the orphaned demo trades
    (``orphaned_note``)."""
    rep = build(inputs, now_ms)
    rep["forwards"].append(journal_row(inputs.get("idea_cards") or [], inputs.get("journals"), inputs.get("plans") or [], now_ms))
    if note := orphaned_note(inputs.get("demo_trades") or [], now_ms):
        rep["notes"].append(note)
    return rep


def load_idea_cards(ideas: Path = IDEAS) -> list[dict[str, Any]]:
    """The idea cards with status ``forward`` and their ``idea:`` tags; [] when the directory cannot be read."""
    texts = {}
    for p in sorted(ideas.glob("IDEA-*.md")):
        try:
            texts[p.name] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return forward_cards(texts)


def ladder_dirs() -> list[Path]:
    """Where the ladder snapshots live, in order of precedence: the repository's ``docs/paper/ladder/`` and the weekly
    timer's directory (``TRADEDESK_LADDER_DIR``, default ``/var/lib/tradedesk-mcp/ladder``)."""
    return [LADDER, Path(os.environ.get("TRADEDESK_LADDER_DIR") or LADDER_SERVER)]


def load_ladder(folders: Path | Sequence[Path] | None = None, errors: list[str] | None = None) -> list[dict[str, Any]]:
    """The dated Bybit ladder snapshots ``YYYY-MM-DD-bybit.json`` for carry criterion (ye): per file its date, the moment
    it was taken (``taken_utc``, UTC ms) and the largest 2x position per Bybit symbol (``max_position_usd``; None when no
    tier allows 2x). ``folders`` defaults to ``ladder_dirs()``; a missing directory gives nothing, without an error. A
    date found in several directories is taken once, from the first one that reads; the result is sorted by date. A file
    that cannot be read is skipped and named in ``errors``."""
    if folders is None:
        folders = ladder_dirs()
    elif isinstance(folders, Path):
        folders = [folders]
    got: dict[str, dict[str, Any]] = {}
    failed: list[tuple[Path, str]] = []
    for folder in folders:
        try:
            files = sorted(folder.glob("*-bybit.json"))
        except OSError:
            files = []
        for p in files:
            if p.name[:10] in got:
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                taken = datetime.strptime(d["taken_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                limits = {str(s): v.get("max_position_usd") for s, v in d["symbols"].items()}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                failed.append((p, type(e).__name__))
                continue
            got[p.name[:10]] = {"date": p.name[:10], "taken_ms": int(taken.timestamp() * 1000), "limits": limits}
    if errors is not None:
        for p, err in failed:
            tail = "узято копію з іншого каталогу" if p.name[:10] in got else "для (є) його немає"
            errors.append(f"знімок ladder {p} не прочитано ({err}): {tail}")
    return [got[k] for k in sorted(got)]


def load_paper_state(db: str) -> dict[str, Any] | None:
    """The assistant_paper job's state file next to the DB (``collector.assistant_paper.state_path``), only the keys the
    report shows; None when it is absent or unreadable."""
    try:
        d = json.loads((Path(db).resolve().parent / "assistant" / "paper_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {k: d.get(k) for k in ("date", "asof_utc", "state", "action")} if isinstance(d, dict) else None


def load_inputs(db: str, now_ms: int) -> dict[str, Any]:
    """Everything ``checkpoint.build`` needs; a part that cannot be read is empty and named in ``errors`` (never raises)."""
    inp: dict[str, Any] = {
        "errors": [],
        "side_cost_a": carry_side_cost(PARAMS_A.fee_1, PARAMS_A.fee_2, PARAMS_A.slippage),
        "decision_delay_ms": DECISION_DELAY_MS,
    }
    try:
        bench = json.loads(BENCH.read_text(encoding="utf-8"))
        inp["bench"] = {s: bench[s]["bench"] for s in ("A", "B")}
    except (OSError, ValueError, KeyError) as e:
        inp["errors"].append(f"бенч carry не прочитано ({type(e).__name__}): критерій (в) — ◯")
    inp["ladder"] = load_ladder(errors=inp["errors"])
    if not inp["ladder"]:
        inp["errors"].append("знімків ladder Bybit не знайдено: (є) для Bybit-ноги — без даних")
    inp["paper_state"] = load_paper_state(db)
    inp["idea_cards"] = load_idea_cards()
    try:
        con = connect_ro(db)
    except (sqlite3.Error, OSError) as e:
        inp["errors"].append(f"БД {db} не відкрито ({type(e).__name__})")
        return inp
    names: set[str] | None = None  # None: the schema could not be listed, the loaders below name the failure
    try:
        names = _tables(con)
        inp["errors"] += [f"{t}: таблиці немає в БД (старіша схема чи інша БД), блок без даних" for t in FORWARD_TABLES if t not in names]
    except sqlite3.Error:
        pass
    try:
        loaders = (
            ("demo_trade", "demo_trades", lambda: load_demo(con)),
            ("carry", "carry", lambda: {s: load_carry(con, s) for s in ("A", "B")}),
            ("rec_paper", "rec_paper", lambda: load_rec_paper(con)),
            ("trade_plan", "plans", lambda: load_plans(con)),
            ("журнали карток forward", "journals", lambda: load_journals(con, now_ms)),
        )
        for what, key, fn in loaders:
            try:
                inp[key] = fn()
            except sqlite3.Error as e:
                inp["errors"].append(f"{what}: запит не вдався ({type(e).__name__})")
    finally:
        con.close()
    try:
        import pm_gap_paper_report  # noqa: PLC0415 - its own read-only loader

        inp["pm_rows"], inp["pm_outcomes"] = pm_gap_paper_report.load(db)
    except sqlite3.Error as e:
        inp["errors"].append(f"pm_gap: запит не вдався ({type(e).__name__})")
    cut = datetime(EARLIEST_T0.year, EARLIEST_T0.month, EARLIEST_T0.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC)
    now = datetime.fromtimestamp(now_ms / 1000, UTC)
    # The registration forbids reading the condor snapshots before T0; without the table there is nothing to read (named above).
    if now >= cut and (names is None or "option_leg_snapshot" in names):
        try:
            import condor_forward_costs as cfc  # noqa: PLC0415 - its read-only T0 detection and loader

            t0 = cfc.detect_t0(Path(db), now)
            inp["condor_t0"] = t0
            if t0 is not None:
                final = now_ms >= condor_final_ms(t0)  # hold_* (Thursday) rows only once part (a) may be decided
                fridays = [
                    f for f in cost_fridays(t0) if final or datetime(f.year, f.month, f.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC) <= now
                ]
                inp["condor_rows"] = cfc.load_rows(Path(db), fridays, with_hold=final) if fridays else []
        except sqlite3.Error as e:
            inp["errors"].append(f"option_leg_snapshot: запит не вдався ({type(e).__name__})")
    return inp


def at_date(s: str | None, wall_ms: int) -> int:
    """End of the UTC day ``s`` (YYYY-MM-DD) in ms, never later than ``wall_ms``; None = ``wall_ms``."""
    if not s:
        return wall_ms
    end = int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000) + 86_400_000 - 1
    return min(end, wall_ms)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--date", help="state at the end of this UTC day, YYYY-MM-DD (default: now; capped at the wall clock)")
    ap.add_argument("--out", help="also write the text to this file")
    a = ap.parse_args(argv)
    now_ms = at_date(a.date, int(time.time() * 1000))
    text = render(build_report(load_inputs(a.db, now_ms), now_ms))
    print(text, end="")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
