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
with neither a fresh journal nor an ``idea:`` plan are listed as «форвард без збору» (item 312).
Demo trades still ``open`` more than a day past their hold get a closing note with their count
(``tradedesk.analytics.stuck_demo``, item 133): they are missing from the closed samples. So are the trades the
demo trader set aside as ``orphaned`` (filled, no position and no close found on the venue): a note names them (item 326).
Closed demo trades whose recorded P&L disagrees with ``(exit - entry) * qty`` minus fees get a note «Угоди з розбіжним
P&L: N» (``tradedesk.analytics.demo_pnl_check``, item 140; only when N > 0): their exit price and reason are suspect.
Each demo setup row with known fees notes the demo's fees against the backtest fee model (mean ``fee_gap_r``, taker
entries; ``tradedesk.analytics.checkpoint.demo_fee_note``, item 341), descriptive only.
The section «S12: правило зняття» applies the owner's rule Q-20261109-1 to the closed S12 demo trades (v9: n >= 30 and
mean R below -0.827 -> stop and review the execution; v4 entry, v4 + v9: n >= 100 and below -0.524R -> drop; else
«рано» or «тримати»), with n, mean R and a 95 % CI (``tradedesk.analytics.checkpoint.s12_stop_rule``, item 313).
The text (Ukrainian) is printed and, with ``--out``, written to a file.
"""

from __future__ import annotations

import argparse
import json
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
from tradedesk.analytics.checkpoint import COUNTS_ONLY, build, condor_final_ms, forward_cards, render  # noqa: E402
from tradedesk.analytics.condor_forward import EARLIEST_T0, ENTRY_CUTOFF_HOUR_UTC, cost_fridays  # noqa: E402
from tradedesk.analytics.pm_gap_report import T0_TS, T0_V2G_TS, verdict_ts, verdict_ts_v2g  # noqa: E402
from tradedesk.collector.carry_paper_engine import PARAMS_A  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
BENCH = ROOT / "docs" / "paper" / "bench.json"
IDEAS = ROOT / "docs" / "ideas"
CARRY_KINDS = ("side", "funding", "mark", "close", "event")
# Every table a forward block reads. The migrations create them all, so an absent one means an older schema or another
# DB file: the report names it instead of showing the block as if it merely had no rows yet.
FORWARD_TABLES = (
    "demo_trade", "cp_position", "cp_log", "cp_cycle", "rec_paper", "pm_gap_snapshot", "pm_gap_outcome", "option_leg_snapshot",
    "trade_plan",
)  # fmt: skip


@dataclass(frozen=True)
class Journal:
    """A forward card's own journal, as the card names it. ``sql`` returns one row (observations, first, last unix s)
    for ``since <= ts <= now`` (two ``?``, unix s); ``{+N}`` in ``verdict`` is the date of the first observation + N days.
    An optional fourth column counts every row of the window when the first three count only the good ones (carry:
    ``status = 'done'``), so a journal of failed / skipped cycles is neither fresh nor hidden."""

    table: str
    sql: str
    unit: str
    minimum: str
    verdict: str
    since: int = 0  # a registered T0 (unix s): observations before it belong to no forward of the card


def _utc(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, UTC).strftime(fmt)


_PM_SQL = "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM pm_gap_snapshot WHERE ts >= ? AND ts <= ?"
_PM_MIN = "16 тижнів входів; ≥ 20 днів розв'язання і ≥ 10 тижневих кластерів (позиції — рядки Polymarket вище)"
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
    "hl-oi-cap-state-forward": Journal(
        "hl_oi_cap_snapshot", "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM hl_oi_cap_snapshot WHERE ts >= ? AND ts <= ?",
        "годинних знімків", "ворота G1/G2 після 14 діб збору; вердикт H1 при n ≥ 250 подій (події визначає крок 1)",
        "ворота G1/G2 {+14}; H1 — коли n ≥ 250 подій (дати в картці нема)",
    ),
    "hl-whale-liquidation-magnet": Journal(
        "trader_positions",
        "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM trader_positions WHERE venue = 'hyperliquid' AND ts >= ? AND ts <= ?",
        "записів змін позицій HL (liq_watch кроку 1 не збудовано)",
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


def load_journals(con: sqlite3.Connection, now_ms: int) -> dict[str, dict[str, Any]]:
    """Per card of ``CARD_JOURNALS``: ``{"n", "first", "last"}`` up to ``now_ms`` (plus ``"all"`` when the query has a
    fourth column), ``{"absent": True}`` without its table, or ``{"error": name}`` when its query fails (one card's
    failure does not hide the others). Counters only."""
    names, out = _tables(con), {}
    for slug, j in CARD_JOURNALS.items():
        if j.table not in names:
            out[slug] = {"absent": True}
            continue
        try:
            n, first, last, *rest = con.execute(j.sql, (j.since, now_ms // 1000)).fetchone()
        except sqlite3.Error as e:
            out[slug] = {"error": type(e).__name__}
            continue
        out[slug] = {"n": int(n or 0), "first": first, "last": last} | ({"all": int(rest[0] or 0)} if rest else {})
    return out


def _plan_count(card: Mapping[str, Any], plans: Sequence[Mapping[str, Any]], now_s: int) -> int:
    tags = card.get("tags") or frozenset()
    ok = [p for p in plans if isinstance(c := p.get("created_ts"), (int, float)) and not isinstance(c, bool) and c <= now_s]
    return sum(1 for p in ok if {t for t in (p.get("tags") or []) if isinstance(t, str)} & tags)


def journal_row(
    cards: Sequence[Mapping[str, Any]], journals: Mapping[str, Mapping[str, Any]] | None, plans: Sequence[Mapping[str, Any]], now_ms: int
) -> dict[str, Any]:
    """One report row: the forward cards' own journals by counters (observations to date, first / last, minimum and
    verdict date as the card writes them) and «форвард без збору»: no fresh journal and no ``idea:`` plan. ``journals``
    None = the loader could not read the DB (each journal card is then named as unread, not as empty)."""
    now_s = now_ms // 1000
    counters: dict[str, Any] = {}
    idle: list[str] = []
    collecting = 0
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
            other = f" (усіх записів {got['all']})" if "all" in got else ""
            counters[slug] = (
                f"журнал {j.table}: {j.unit} {got['n']}{other}, з {_utc(first)} до {_utc(last)} UTC; мінімум: {j.minimum}; "
                f"вердикт: {verdict}"
            )
            fresh = now_s - last <= STALE_S
            done = " зі статусом done" if "all" in got else ""
            why = None if fresh else f"останній запис {j.table}{done} {_utc(last)} UTC"
        if fresh or _plan_count(card, plans, now_s):  # idea: plans are a journal too (the row above)
            collecting += 1
        elif why is not None:
            idle.append(f"{slug} ({why})")
    if idle:
        counters[f"форвард без збору ({len(idle)})"] = "; ".join(idle)
    row: dict[str, Any] = {
        "key": "ideas:journals", "group": "ideas", "name": JOURNALS_NAME, "n": collecting, "metric": None, "band": None,
        "metric_name": f"лише лічильники; карток {len(cards)}, збирають дані {collecting}", "verdict": COUNTS_ONLY,
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


def orphaned_note(trades: Sequence[Mapping[str, Any]], now_ms: int) -> str | None:
    """Item 326: demo trades the trader set aside as ``orphaned`` (filled, then neither the position nor its close found
    on the venue for a day; ``execution/demo_trader.py``, ``_orphan``) and filled by ``now_ms`` (a row without its fill
    time is dated by its signal bar). They are in no closed sample and, no longer ``open``, not among the stuck trades
    either, so without this note the demo n would be short without a word. None when there is none."""

    def t0(t: Mapping[str, Any]) -> float | None:
        for k in ("opened_ts", "signal_ts"):
            if isinstance(v := t.get(k), (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    rows = sorted(
        (t for t in trades if t.get("status") == "orphaned" and (o := t0(t)) is not None and o <= now_ms),
        key=lambda t: (t0(t) or 0.0, str(t.get("link_id"))),
    )
    if not rows:
        return None
    named = ", ".join(f"{t.get('setup') or '?'} {t.get('symbol') or t.get('link_id')}" for t in rows)
    return (
        f"демо-угоди orphaned (заповнені, а ні позиції, ні закриття на біржі не знайдено): {len(rows)} — {named}; "
        "вони не входять у закриті вибірки й не рахуються завислими, тож n демо неповне: закриття перевірити вручну"
    )


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
