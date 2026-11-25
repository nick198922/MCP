"""Smart-money forward, slice 1 (IDEA-20261015-smart-money-persistence: H1, H1w, H2, H3).

Rules: the card's "Попередня реєстрація" (2026-10-17) and "Зріз 1 форварду — уточнення виконання" (2026-11-25,
written before any outcome was computed). Stages:
  uv run python scripts/verify_smart_money_slice1.py --stage coverage   # counts only, no outcome, no network
  uv run python scripts/verify_smart_money_slice1.py --stage full       # fetch outcomes (cached) and the metrics
Common options: ``--as-of 2026-11-25T00:00:00Z`` (fixed cut-off: no collector row and no outcome after it is used),
``--db`` (a copy of the collector store; default: an online ``sqlite3`` backup of the live store made once into the
cache dir), ``--cache-dir`` (default ~/.cache/tradedesk-agents/smart_money_slice1), ``--stage0-cache`` (stage-0
``clearinghouseState`` files of 2026-10-17, for the classes on 2026-10-16), ``--out`` (text report), ``--seed``.
Network (full stage only) only through the runtime providers (Hyperliquid info ``portfolio`` / ``candleSnapshot``,
Polymarket Gamma ``/markets``), one JSON file per response in the cache dir, so a rerun is offline and identical.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import random
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import common_helpers as cmn  # noqa: E402

from tradedesk.analytics import smart_money_forward as smf  # noqa: E402
from tradedesk.analytics.funding_crowding import spearman  # noqa: E402
from tradedesk.analytics.smart_money import (  # noqa: E402
    detect_delta_start,
    finite_num,
    mean_t_ci,
    net_flow,
    parse_portfolio,
)
from tradedesk.core.errors import ProviderError  # noqa: E402

LIVE_DB = Path("/var/lib/tradedesk-mcp/tradedesk.sqlite")
DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "smart_money_slice1"
STAGE0_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "smart_money"
START = date(2026, 10, 16)  # registered first formation day d
H2_FIRST = date(2026, 10, 17)  # first day with a positions run 24 h earlier
H2_HORIZONS = (7, 1, 3)  # primary first
TOP_N = 100  # collector traders_top_n
RUN_LAG_S = 2 * 3600  # a day's positions run: the first run within 2 h after 00:00 UTC
PM_V2_FIRST_RUN = 1793845223  # first Polymarket data-api v2 run: rows are a schema artefact -> a new baseline
# Stage-0 between-period SDs (docs/verification/2026-10-17-smart-money-persistence.md; the larger variant is used).
RETRO_SD = {"h1_ic": 0.3264, "h1_d": 0.1634, "h1w_ic": 0.3908, "h1w_d": 0.0183}
GATES = {"h1_ic": 0.05, "h1_d": 0.02, "h1w_ic": 0.03, "h1w_d": 0.005, "h2_ic": 0.03}
WEIGHT_PER_MIN = 1100.0
N_PERM = 2000
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def f4(x: Any, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    return f"{x * 100:+.2f} %" if pct else f"{x:+.4f}"


def day_start_s(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------------------------------------------------
# Store


def open_store(db: str | None, cache: Path) -> sqlite3.Connection:
    """Read-only connection to a store copy. Without ``--db`` an online backup of the live store is made once."""
    if db:
        src = Path(db)
    else:
        src = cache / "tradedesk.sqlite"
        if not src.exists():
            t0 = time.time()
            cmn.backup_db(LIVE_DB, src)
            emit(f"backup of {LIVE_DB} -> {src} in {time.time() - t0:.0f} s")
    emit(f"db: {src}")
    return cmn.connect_ro(src)


def load_snapshots(conn: sqlite3.Connection, venue: str, as_of_s: int) -> dict[date, dict[str, dict[str, Any]]]:
    cols = ["account", "ts", "account_value", "pnl_day", "pnl_week", "pnl_month", "pnl_all", "vlm_month"]
    out: dict[date, dict[str, dict[str, Any]]] = {}
    cur = conn.execute(f"SELECT day, {', '.join(cols)} FROM trader_snapshot WHERE venue = ? AND ts <= ?", (venue, as_of_s))
    for r in cur:
        row = {c: (finite_num(v) if c not in ("account", "ts") else v) for c, v in zip(cols, r[1:], strict=True)}
        out.setdefault(date.fromisoformat(r[0]), {})[r[1]] = row
    return out


def latest_snap(snaps: dict[date, Any], d: date, max_back: int) -> date | None:
    for k in range(max_back + 1):
        if d - timedelta(days=k) in snaps:
            return d - timedelta(days=k)
    return None


def hl_runs(conn: sqlite3.Connection, as_of_s: int) -> list[int]:
    q = (
        "SELECT ts FROM trader_positions WHERE venue = 'hyperliquid' AND ts <= ? "
        "UNION SELECT ts FROM trader_polls WHERE venue = 'hyperliquid' AND ts <= ?"
    )
    return sorted(int(r[0]) for r in conn.execute(q, (as_of_s, as_of_s)))


def polls_at(conn: sqlite3.Connection, ts: int) -> dict[str, int] | None:
    rows = conn.execute("SELECT account, ok FROM trader_polls WHERE venue = 'hyperliquid' AND ts = ?", (ts,)).fetchall()
    return {a: int(ok) for a, ok in rows} if rows else None


def position_rows(conn: sqlite3.Connection, venue: str, as_of_s: int):
    cur = conn.execute(
        "SELECT ts, account, market, side, size, value_usd, entry_px, title FROM trader_positions "
        "WHERE venue = ? AND ts <= ? ORDER BY ts, account",
        (venue, as_of_s),
    )
    cols = ["ts", "account", "market", "side", "size", "value_usd", "entry_px", "title"]
    for r in cur:
        yield dict(zip(cols, r, strict=True))


def delta_start_of(conn: sqlite3.Connection) -> int | None:
    runs = [int(r[0]) for r in conn.execute("SELECT DISTINCT ts FROM trader_positions")]
    closed = [int(r[0]) for r in conn.execute("SELECT MIN(ts) FROM trader_positions WHERE side = 'closed'") if r[0] is not None]
    return detect_delta_start(runs, closed)


def stage0_positions(stage0: Path, account: str) -> list[dict[str, Any]] | None:
    p = stage0 / "state" / f"{account}.json"
    if not p.exists():
        return None
    try:
        st = cmn.read_json(p)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    out = []
    for ap in st.get("assetPositions") or []:
        pos = ap.get("position") if isinstance(ap, dict) else None
        if isinstance(pos, dict):
            szi, val = finite_num(pos.get("szi") or 0.0), finite_num(pos.get("positionValue") or 0.0)
            if szi and val is not None:
                out.append({"side": "long" if szi > 0 else "short", "value_usd": val})
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Shared preparation (counts only)


class Prep:
    """Everything derived from the store up to the as-of cut-off: snapshots, periods, classes, H2 signals, H3 events."""

    def __init__(self, conn: sqlite3.Connection, as_of: datetime, stage0: Path) -> None:
        self.as_of_s = int(as_of.timestamp())
        self.as_of_day = as_of.date()
        t0 = time.time()
        self.hl = load_snapshots(conn, "hyperliquid", self.as_of_s)
        self.pm = {v: load_snapshots(conn, v, self.as_of_s) for v in ("polymarket", "polymarket_crypto")}
        emit(f"snapshots loaded in {time.time() - t0:.0f} s, peak RSS {cmn.rss_mb():.0f} MB")
        self.h1 = smf.complete_periods(START, 30, self.as_of_day)
        self.h1w = smf.complete_periods(START, 7, self.as_of_day)
        self.delta_start = delta_start_of(conn)
        self.runs = hl_runs(conn, self.as_of_s)
        last_signal_day = self.as_of_day - timedelta(days=1)
        self.h2_days = [H2_FIRST + timedelta(days=k) for k in range((last_signal_day - H2_FIRST).days + 1)]
        self.day_run = {d: smf.first_run_at_or_after(self.runs, day_start_s(d), RUN_LAG_S) for d in [START, *self.h2_days]}
        self.formation_days = sorted({d for d, _ in self.h1} | {d for d, _ in self.h1w})
        self.hl_view = smf.SnapshotsAt(self.hl)
        self.hl_watch = smf.WatchListAt(self.hl, TOP_N, smf.MIN_VALUE)
        self._replay_hl(conn, stage0)
        self._pm_events(conn)

    def tracked_hl(self, d: date) -> frozenset[str]:
        """The collector's Hyperliquid watch list at the first run of day d, rebuilt only from snapshot rows written
        before that run (``Store.tracked_traders`` point-in-time)."""
        return self.hl_watch.at(self.day_run.get(d) or day_start_s(d))

    def day_classes(
        self, d: date, snap: dict[str, Any], state: dict[str, Any] | None, stage0: Path, use_stage0: bool = False
    ) -> tuple[dict[str, str], Counter]:
        """Registered classes on day d, one rule on every d (slice-1 fix round, M2): positions from the replayed
        ``trader_positions`` book at the day's ~00:15 run for accounts watched then; else unknown (turnover >= 100 ->
        'mm', stage 0's rule for unobserved positions). ``use_stage0`` (sensitivity, 2026-10-16 only): stage-0
        ``clearinghouseState`` fetched 2026-10-17 ~07:15 first."""
        st = state or {}
        tracked = self.tracked_hl(d)
        cls, src = {}, Counter()
        for a, row in snap.items():
            pos = stage0_positions(stage0, a) if use_stage0 else None
            if pos is not None:
                src["stage0_state"] += 1
            elif a in tracked and a in st:
                pos = [{"side": p.get("side"), "value_usd": p.get("value_usd")} for p in st[a].values()]
                src["trader_positions"] += 1
            else:
                src["none"] += 1
            cls[a] = smf.h1_class(row, pos)
        return cls, src

    def _replay_hl(self, conn: sqlite3.Connection, stage0: Path) -> None:
        """One pass over the Hyperliquid log: the book at each day's first run; classes on formation days; the H2
        signal of day d from the books at the runs of d − 1 and d (only the previous checkpoint is held)."""
        cps = sorted({t for t in self.day_run.values() if t is not None})
        run_day = {t: d for d, t in sorted(self.day_run.items(), reverse=True) if t is not None}
        self.classes: dict[date, dict[str, str]] = {}
        self.class_src: dict[date, Counter] = {}
        self.classes_stage0: dict[date, dict[str, str]] = {}  # sensitivity (M2): 2026-10-16 with stage-0 positions
        self.h2: dict[date, dict[str, Any]] = {}
        for d in self.h2_days:
            b_ts, a_ts = self.day_run.get(d - timedelta(days=1)), self.day_run.get(d)
            self.h2[d] = {"before": b_ts, "after": a_ts, "skip": "no_run"}
        prev_ts, prev_st = None, None
        for ts, st in smf.replay_checkpoints(position_rows(conn, "hyperliquid", self.as_of_s), self.delta_start, cps):
            d = run_day[ts]
            if d in self.formation_days and d in self.hl:
                self.classes[d], self.class_src[d] = self.day_classes(d, self.hl[d], st, stage0)
                if d == START:
                    self.classes_stage0[d], _ = self.day_classes(d, self.hl[d], st, stage0, use_stage0=True)
            rec = self.h2.get(d)
            if rec is not None and rec["before"] == prev_ts and prev_st is not None:
                self._h2_signal(conn, d, rec, prev_st, st, stage0)
            prev_ts, prev_st = ts, st
        for d in self.formation_days:  # a formation day without a run: classes without trader_positions
            if d not in self.classes and d in self.hl:
                self.classes[d], self.class_src[d] = self.day_classes(d, self.hl[d], None, stage0)
                if d == START:
                    self.classes_stage0[d], _ = self.day_classes(d, self.hl[d], None, stage0, use_stage0=True)

    def _h2_signal(self, conn: sqlite3.Connection, d: date, rec: dict[str, Any], before: Any, after: Any, stage0: Path) -> None:
        sd = latest_snap(self.hl, d, 1)
        if sd is None:
            rec["skip"] = "no_snapshot"
            return
        snap = self.hl[sd]
        if max(int(r["ts"]) for r in snap.values()) >= day_start_s(d + timedelta(days=1)):  # m1: written before entry
            rec["skip"] = "snapshot_after_entry"
            return
        cls, _ = self.day_classes(d, snap, after, stage0)
        tracked = self.tracked_hl(d) & self.tracked_hl(d - timedelta(days=1))
        pb, pa = polls_at(conn, rec["before"]), polls_at(conn, rec["after"])
        polled = {a for a in tracked if (pb is None or pb.get(a) == 1) and (pa is None or pa.get(a) == 1)}
        strong = smf.strong_accounts(snap, cls, polled)
        flow = net_flow(before, after, sorted(strong))
        # M4 sensitivity (descriptive): entry at d 01:00, so the strong set may use only snapshot rows written
        # before the day's run (the previous day's snapshot), not the day-d snapshot written later that day
        _, early_snap = self.hl_view.rows(rec["after"])
        cls_e, _ = self.day_classes(d, dict(early_snap), after, stage0)
        strong_e = smf.strong_accounts(early_snap, cls_e, polled) if early_snap else set()
        flow_e = net_flow(before, after, sorted(strong_e))
        rec.update(
            snapshot=sd.isoformat(),
            markers=(pb is not None, pa is not None),
            n_tracked=len(tracked),
            n_polled=len(polled),
            n_strong=len(strong),
            signal={c: v for c, v in sorted(flow.items()) if v != 0},
            signal_early={c: v for c, v in sorted(flow_e.items()) if v != 0},
        )
        rec["skip"] = None if len(rec["signal"]) >= smf.H2_MIN_COINS else "lt10_coins"

    def _pm_events(self, conn: sqlite3.Connection) -> None:
        self.pm_v2_rows = conn.execute(
            "SELECT COUNT(*) FROM trader_positions WHERE venue = 'polymarket' AND ts = ?", (PM_V2_FIRST_RUN,)
        ).fetchone()[0]
        self.pm_runs = conn.execute(
            "SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM trader_positions WHERE venue = 'polymarket' AND ts <= ?", (self.as_of_s,)
        ).fetchone()
        run_ts = sorted(
            int(r[0])
            for r in conn.execute("SELECT DISTINCT ts FROM trader_positions WHERE venue = 'polymarket' AND ts <= ?", (self.as_of_s,))
        )
        # m2: the v2 baseline is set per wallet at its first observation at or after the first v2 run
        ev = smf.new_positions(
            position_rows(conn, "polymarket", self.as_of_s), self.delta_start, self.as_of_s, rebaseline_from=PM_V2_FIRST_RUN
        )
        # B1: rankings rebuilt at the event's ts from snapshot rows written before it (not the day's snapshot)
        watch = {v: smf.WatchListAt(self.pm[v], TOP_N) for v in ("polymarket", "polymarket_crypto")}

        def either(t: int) -> frozenset[str]:
            return watch["polymarket"].at(t) | watch["polymarket_crypto"].at(t)

        for e in ev:
            t, a = e["ts"], e["account"]
            e["overall"] = a in watch["polymarket"].at(t)
            e["crypto"] = a in watch["polymarket_crypto"].at(t)
            i = bisect.bisect_left(run_ts, t) - 1
            e["tracked_prev_run"] = i >= 0 and a in either(run_ts[i])
            e["tracked_prev_day"] = a in either(t - 86400)
            e["tracked_before"] = e["tracked_prev_run"] and e["tracked_prev_day"]
            p = e["p_obs"]
            e["in_range"] = p is not None and smf.H3_P_MIN <= p <= smf.H3_P_MAX
            e["fresh"] = smf.is_fresh_entry(p, e["entry_px"])
        self.pm_events = ev


# ---------------------------------------------------------------------------------------------------------------------
# Coverage (counts only)


def coverage(prep: Prep) -> dict[str, Any]:
    out: dict[str, Any] = {}
    emit(f"## Покриття на {datetime.fromtimestamp(prep.as_of_s, UTC):%Y-%m-%d %H:%M} UTC (лише кількості)")
    emit("")
    emit("### Знімки trader_snapshot")
    for venue, snaps in [("hyperliquid", prep.hl), *prep.pm.items()]:
        days = sorted(snaps)
        all_days = [days[0] + timedelta(days=k) for k in range((days[-1] - days[0]).days + 1)] if days else []
        missing = [x.isoformat() for x in all_days if x not in snaps]
        sizes = [len(snaps[x]) for x in days]
        hours = [
            datetime.fromtimestamp(r["ts"], UTC).hour + datetime.fromtimestamp(r["ts"], UTC).minute / 60
            for x in days
            for r in list(snaps[x].values())[:1]
        ]
        multi = sum(1 for x in days if len({r["ts"] for r in snaps[x].values()}) > 1)
        emit(
            f"- {venue}: {len(days)} днів {days[0] if days else '—'} … {days[-1] if days else '—'}; пропущені дні: {', '.join(missing) or 'немає'}; "
            f"рядків на день min / медіана / max {min(sizes, default=0)} / {statistics.median(sizes) if sizes else 0:.0f} / {max(sizes, default=0)}; "
            f"година знімка UTC min / max {min(hours, default=0):.2f} / {max(hours, default=0):.2f}; днів з кількома ts {multi}"
        )
        out[f"snap_{venue}"] = {"days": len(days), "missing": missing}
    emit("")
    emit("### H1 / H1w: періоди, універсум, відсутні на кінці")
    emit(
        "| гіпотеза | d → кінець | акаунтів ≥ $500k | без system/mm | джерело позицій для класу (stage0 / trader_positions / нема) | turnover ≥ 100 без позицій (→ mm) | є в знімку кінця з PnL | відсутні (→ portfolio) |"
    )
    emit("|---|---|---:|---:|---|---:|---:|---:|")
    rows = []
    for name, periods, field in (("H1", prep.h1, "pnl_month"), ("H1w", prep.h1w, "pnl_week")):
        for d, e in periods:
            snap, end = prep.hl.get(d), prep.hl.get(e)
            if snap is None or end is None:
                emit(f"| {name} | {d} → {e} | знімка нема: {'d' if snap is None else 'кінця'} | | | | | |")
                rows.append({"h": name, "d": d.isoformat(), "e": e.isoformat(), "skip": "no_snapshot"})
                continue
            cls = prep.classes[d]
            n500 = sum(1 for r in snap.values() if (r["account_value"] or 0) >= smf.MIN_VALUE)
            uni = smf.h1_universe(snap, cls, field)
            src = prep.class_src[d]
            hi_to = sum(
                1 for a, r in snap.items()
                if (r["account_value"] or 0) >= smf.MIN_VALUE and (r["vlm_month"] or 0) / r["account_value"] >= 100 and cls[a] == "mm"
            )  # fmt: skip
            present = sum(1 for a in uni if a in end and end[a].get(field) is not None)
            emit(
                f"| {name} | {d} → {e} | {n500} | {len(uni)} | {src['stage0_state']} / {src['trader_positions']} / {src['none']} | "
                f"{hi_to} (mm усього) | {present} | {len(uni) - present} |"
            )
            rows.append(
                {
                    "h": name,
                    "d": d.isoformat(),
                    "e": e.isoformat(),
                    "n500": n500,
                    "universe": len(uni),
                    "present": present,
                    "missing": len(uni) - present,
                }
            )
    out["h1"] = rows
    cls_counts = Counter(prep.classes[START].values()) if START in prep.classes else Counter()
    emit(f"- класи на {START} (основне правило, як на всіх d): " + ", ".join(f"{k} {v}" for k, v in sorted(cls_counts.items())))
    s0 = Counter(prep.classes_stage0.get(START, {}).values())
    emit(f"- класи на {START} (чутливість: позиції стадії 0 від 17.10): " + ", ".join(f"{k} {v}" for k, v in sorted(s0.items())))
    if START in prep.classes_stage0:
        uni0 = smf.h1_universe(prep.hl[START], prep.classes_stage0[START], "pnl_month")
        emit(f"- універсум H1 на {START} з класами стадії 0: {len(uni0)}")
    emit("")
    emit("### H2: денні прогони позицій і сигнал сильних акаунтів (без дохідностей)")
    emit(f"- прогонів позицій HL до as-of: {len(prep.runs)}; v12 delta_start ts {prep.delta_start}")
    skips = Counter(r.get("skip") or "ok" for r in prep.h2.values())
    emit(
        f"- днів сигналу {H2_FIRST} … {prep.h2_days[-1]}: {len(prep.h2)}; статус: "
        + ", ".join(f"{k} {v}" for k, v in sorted(skips.items()))
    )
    ok = [r for r in prep.h2.values() if r.get("signal") is not None]
    if ok:
        emit(
            "- на днях із прогонами: відстежуваних (обидва дні) медіана "
            f"{statistics.median(r['n_tracked'] for r in ok):.0f}, опитаних ok {statistics.median(r['n_polled'] for r in ok):.0f}, "
            f"сильних {statistics.median(r['n_strong'] for r in ok):.0f}; монет із ненульовим сигналом медіана "
            f"{statistics.median(len(r['signal']) for r in ok):.0f} (min {min(len(r['signal']) for r in ok)}, max {max(len(r['signal']) for r in ok)}); "
            f"днів із маркерами trader_polls {sum(1 for r in ok if all(r['markers']))}"
        )
    for h in H2_HORIZONS:
        md = smf.matured_days(H2_FIRST, prep.h2_days[-1], h, prep.as_of_day)
        n_ok = sum(1 for d in md if prep.h2[d].get("skip") is None)
        emit(f"- h = {h}: дозрілих днів {len(md)} (останній {md[-1] if md else '—'}), з них із ≥ 10 монетами {n_ok}")
        out[f"h2_h{h}"] = {"matured": len(md), "usable": n_ok}
    no_run = [d.isoformat() for d, r in prep.h2.items() if r.get("skip") == "no_run"]
    if no_run:
        emit(f"- дні без прогону ~00:15 (d або d − 1): {', '.join(no_run)}")
    coins = sorted({c for r in ok for c in r["signal"]})
    emit(f"- різних монет у сигналах: {len(coins)}")
    out["h2_coins"] = len(coins)
    emit("")
    emit("### H3: нові позиції Polymarket (без вирішень)")
    n_runs, t_min, t_max = prep.pm_runs
    emit(
        f"- прогонів позицій PM: {n_runs} ({datetime.fromtimestamp(t_min or 0, UTC):%Y-%m-%d %H:%M} … "
        f"{datetime.fromtimestamp(t_max or 0, UTC):%Y-%m-%d %H:%M}); рядків у прогоні v2 {PM_V2_FIRST_RUN}: {prep.pm_v2_rows}"
    )
    ev = prep.pm_events
    fp = lambda e: e["in_range"] and e["first_pair"]  # noqa: E731
    for label, sel in (
        ("усі нові (з повторами)", lambda e: True),
        ("перша поява пари (гаманець, токен)", lambda e: e["first_pair"]),
        ("повтори (описово)", lambda e: not e["first_pair"]),
        ("p_obs у [0.03; 0.97], перша поява", fp),
        ("  … загальний рейтинг на момент ts", lambda e: fp(e) and e["overall"]),
        ("  … крипто-рейтинг на момент ts", lambda e: fp(e) and e["crypto"]),
        ("  … жоден рейтинг на момент ts", lambda e: fp(e) and not (e["overall"] or e["crypto"])),
        ("  … гаманець мав < 100 позицій до появи", lambda e: fp(e) and e["n_before"] < 100),
        ("  … у списку на попередньому прогоні", lambda e: fp(e) and e["tracked_prev_run"]),
        ("  … у списку за 24 год до появи", lambda e: fp(e) and e["tracked_prev_day"]),
        ("  … відстежувався раніше (обидва)", lambda e: fp(e) and e["tracked_before"]),
        ("  … свіжий вхід |p_obs − entry_px| ≤ 0.03", lambda e: fp(e) and e["fresh"]),
        (
            "  … загальний + усі три запобіжники",
            lambda e: fp(e) and e["overall"] and e["n_before"] < 100 and e["tracked_before"] and e["fresh"],
        ),
        (
            "  … крипто + усі три запобіжники",
            lambda e: fp(e) and e["crypto"] and e["n_before"] < 100 and e["tracked_before"] and e["fresh"],
        ),
    ):
        s = [e for e in ev if sel(e)]
        emit(
            f"- {label}: позицій {len(s)}, гаманців {len({e['account'] for e in s})}, ринків (за назвою) {len({e['title'] or e['market'] for e in s})}, токенів {len({e['market'] for e in s})}"
        )
    for rk in ("overall", "crypto"):  # concentration (M1) before endDate / resolution filters: counts only
        c = Counter(e["account"] for e in ev if fp(e) and e[rk])
        tot = sum(c.values())
        top5 = sum(n for _, n in c.most_common(smf.H3_TOP_WALLETS_DROP))
        emit(
            f"- {rk}: найбільша частка одного гаманця {max(c.values()) / tot if tot else 0:.3f}, топ-5 гаманців {top5 / tot if tot else 0:.3f} "
            "(до фільтрів endDate і вирішення)"
        )
    by_week = Counter(datetime.fromtimestamp(e["ts"], UTC).strftime("%G-W%V") for e in ev if fp(e))
    emit("- перша поява в діапазоні p за тижнями: " + ", ".join(f"{k} {v}" for k, v in sorted(by_week.items())))
    emit("- запланований endDate (основна вибірка ≤ as-of − 7 діб) і вирішення: невідомі до `full` (Gamma не запитувалась)")
    out["h3"] = {
        "new": len(ev),
        "first_pair": sum(1 for e in ev if e["first_pair"]),
        "in_range": sum(1 for e in ev if fp(e)),
        "fresh": sum(1 for e in ev if fp(e) and e["fresh"]),
        "tokens": len({e["market"] for e in ev if fp(e)}),
    }
    emit(f"- peak RSS {cmn.rss_mb():.0f} MB")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Full stage: fetch (cached) and metrics


class Pacer:
    def __init__(self, per_min: float) -> None:
        self.per_s, self.next_at = per_min / 60.0, time.monotonic()

    async def spend(self, weight: float) -> None:
        now = time.monotonic()
        if self.next_at > now:
            await asyncio.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + weight / self.per_s


async def fetch(prep: Prep, cache: Path, plan: dict[str, list[str]]) -> None:
    """Fill the per-response cache: ``portfolio/<account>.json`` (parsed), ``candles/<coin>.json`` (raw 1d candles
    16.10 … as-of), ``gamma/<token>.json`` (raw Gamma ``/markets`` list). Existing files are not refetched."""
    from tradedesk.server import build_server  # noqa: PLC0415

    _, rt = build_server()
    pacer, t0, failed = Pacer(WEIGHT_PER_MIN), time.time(), Counter()
    start_ms, end_ms = day_start_s(START - timedelta(days=1)) * 1000, prep.as_of_s * 1000
    try:
        for kind in ("portfolio", "candles", "candles1h"):
            keys = plan[kind]
            (cache / kind).mkdir(parents=True, exist_ok=True)
            for i, k in enumerate(keys):
                path = cache_path(cache, kind, k)
                if path.exists():
                    continue
                try:
                    await pacer.spend(20)
                    if kind == "portfolio":
                        data: Any = parse_portfolio((await rt.hyperliquid._info({"type": "portfolio", "user": k}, ttl=0)).data)
                    else:  # 1h: M4 sensitivity (entry d 01:00), ~960 candles per coin < the 5000 limit
                        data = (await rt.hyperliquid.candles(k, "1d" if kind == "candles" else "1h", start_ms, end_ms)).data
                except ProviderError as e:
                    failed[kind] += 1
                    if failed[kind] <= 3:
                        emit(f"  {kind} {k}: {e}")
                    continue
                cmn.write_json(path, data)
                if (i + 1) % 100 == 0 or i + 1 == len(keys):
                    emit(f"  {kind}: {i + 1}/{len(keys)}, {time.time() - t0:.0f} s, failed {failed[kind]}, peak RSS {cmn.rss_mb():.0f} MB")
        await fetch_gamma(rt, cache, plan["gamma"], failed)
    finally:
        await rt.aclose()
    emit(f"fetch done: failed {dict(failed)}")


GAMMA_BATCH = 40  # tokens per Gamma /markets request (repeated clob_token_ids; probe 2026-11-25: 5 asked, 5 returned)
GAMMA_KEEP = ("conditionId", "clobTokenIds", "outcomePrices", "outcomes", "closed", "closedTime", "umaEndDate", "endDate", "question")


def cache_path(cache: Path, kind: str, key: str) -> Path:
    return cache / kind / f"{key.replace(':', '_').replace('/', '_')}.json"


async def gamma_markets(rt: Any, tokens: list[str], extra: dict[str, str]) -> list[dict[str, Any]]:
    p: dict[str, Any] = {"clob_token_ids": tokens, "limit": 2 * len(tokens), **extra}
    data = (await rt.polymarket.gamma.get_json("/markets", params=p, ttl=0, tool="markets", args={"n": len(tokens), **extra})).data
    return [{k: m.get(k) for k in GAMMA_KEEP} for m in data if isinstance(m, dict)] if isinstance(data, list) else []


async def fetch_gamma(rt: Any, cache: Path, tokens: list[str], failed: Counter) -> None:
    """Gamma market objects (stripped to GAMMA_KEEP) of the H3 tokens: deterministic batches of GAMMA_BATCH sorted
    tokens, ``gamma/batch_<i>.json`` = {"tokens": [...], "markets": [...]}; tokens a batch did not return are asked
    again with ``closed=true`` in the same file. Market *outcomes* are read only by the full stage's H3 block."""
    (cache / "gamma").mkdir(parents=True, exist_ok=True)
    pacer, t0 = Pacer(300.0), time.time()  # 5 requests / s
    batches = [tokens[i : i + GAMMA_BATCH] for i in range(0, len(tokens), GAMMA_BATCH)]
    for i, b in enumerate(batches):
        path = cache / "gamma" / f"batch_{i:05d}.json"
        if path.exists():
            continue
        try:
            await pacer.spend(1)
            mk = await gamma_markets(rt, b, {})
            seen = {str(t) for m in mk for t in _ids(m)}
            rest = [t for t in b if t not in seen]
            if rest:
                await pacer.spend(1)
                mk += await gamma_markets(rt, rest, {"closed": "true"})
        except ProviderError as e:
            failed["gamma"] += 1
            if failed["gamma"] <= 3:
                emit(f"  gamma batch {i}: {e}")
            continue
        cmn.write_json(path, {"tokens": b, "markets": mk})
        if (i + 1) % 100 == 0 or i + 1 == len(batches):
            emit(
                f"  gamma: {i + 1}/{len(batches)} batches, {time.time() - t0:.0f} s, failed {failed['gamma']}, peak RSS {cmn.rss_mb():.0f} MB"
            )


def _ids(m: dict[str, Any]) -> list[str]:
    x = m.get("clobTokenIds")
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return []
    return [str(t) for t in x] if isinstance(x, list) else []


def gamma_map(cache: Path) -> dict[str, dict[str, Any]]:
    """token id -> stripped Gamma market, from the batch files (one file at a time)."""
    out: dict[str, dict[str, Any]] = {}
    for p in sorted((cache / "gamma").glob("batch_*.json")):
        try:
            body = cmn.read_json(p)
        except (OSError, ValueError):
            continue
        for m in body.get("markets") or []:
            for t in _ids(m):
                out.setdefault(t, m)
    return out


def load_cached(cache: Path, kind: str, key: str) -> Any:
    p = cache_path(cache, kind, key)
    try:
        return cmn.read_json(p) if p.exists() else None
    except (OSError, ValueError):
        return None


def daily_closes(raw: Any, as_of_s: int) -> dict[date, float]:
    """UTC daily closes from HL 1d candles (``t`` open ms, ``c`` close); only candles closed by the as-of cut-off."""
    out: dict[date, float] = {}
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        t, px = finite_num(c.get("t")), finite_num(c.get("c"))
        if t is None or px is None or t / 1000 + 86400 > as_of_s:
            continue
        out[datetime.fromtimestamp(t / 1000, UTC).date()] = px
    return out


def hour1_closes(raw: Any, as_of_s: int) -> dict[date, float]:
    """Price at d 01:00 UTC per day d: close of the HL 1h candle opened at d 00:00 (closed by the as-of cut-off)."""
    out: dict[date, float] = {}
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        t, px = finite_num(c.get("t")), finite_num(c.get("c"))
        if t is None or px is None or t / 1000 + 3600 > as_of_s or int(t / 1000) % 86400 != 0:
            continue
        out[datetime.fromtimestamp(t / 1000, UTC).date()] = px
    return out


def perm_p(sig: list[float], out: list[float], seed: int, n: int = N_PERM) -> float | None:
    if len(sig) < 10:
        return None
    obs = abs(spearman(sig, out))
    rng, o, hits = random.Random(seed), list(out), 0
    for _ in range(n):
        rng.shuffle(o)
        hits += abs(spearman(sig, o)) >= obs
    return (hits + 1) / (n + 1)


def h1_block(
    prep: Prep, cache: Path, name: str, periods: list[tuple[date, date]], field: str, seed: int, look: int | None = None
) -> dict[str, Any]:
    emit(
        f"### {name}: {'30' if name == 'H1' else '7'}-денні неперекривні періоди (сигнал {field} на d, наслідок {field} на кінці / account_value(d))"
    )
    emit(
        "| d → кінець | n | з portfolio | відсутні | IC | D | IC (відсутні = −1) | D (відсутні = −1) | IC без топ-1 % | D без топ-1 % | IC без hft_directional | p перестановки IC |"
    )
    emit("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    per: list[dict[str, Any]] = []
    for k, (d, e) in enumerate(periods):
        snap, end = prep.hl.get(d), prep.hl.get(e)
        if snap is None or end is None:
            emit(f"| {d} → {e} | знімка нема | | | | | | | | | | |")
            continue
        cls = prep.classes[d]
        uni = smf.h1_universe(snap, cls, field)
        t_end = statistics.median(r["ts"] for r in end.values()) * 1000  # the end day's snapshot time
        fb = {}
        for a in uni:
            if a in end and end[a].get(field) is not None:
                continue
            pf = load_cached(cache, "portfolio", a)
            if isinstance(pf, dict):  # window: this account's snapshot time on d -> the end day's snapshot time
                pf = {w: {"pnl": [tuple(x) for x in (b.get("pnl") or [])]} for w, b in pf.items() if isinstance(b, dict)}
                fb[a] = smf.portfolio_change(pf, int(snap[a]["ts"]) * 1000, int(t_end))
        roi, src = smf.h1_outcomes(uni, snap, end, field, fb)
        sig = {a: snap[a][field] for a in uni}
        m = smf.h1_period(uni, sig, roi)
        m1 = smf.h1_period(uni, sig, roi, missing_as=-1.0)
        mt = smf.h1_period(uni, sig, roi, drop_top_frac=smf.TOP_DROP_FRAC)
        uh = [a for a in uni if cls.get(a) != "hft_directional"]
        mh = smf.h1_period(uh, sig, roi)
        names = [a for a in uni if a in roi]
        p = perm_p([sig[a] for a in names], [roi[a] for a in names], seed + k)
        c = Counter(src.values())
        emit(
            f"| {d} → {e} | {m['n']} | {c['portfolio']} | {c['missing']} | {f4(m['ic'])} | {f4(m['d'], True)} | {f4(m1['ic'])} | {f4(m1['d'], True)} | "
            f"{f4(mt['ic'])} | {f4(mt['d'], True)} | {f4(mh['ic'])} | {p if p is None else f'{p:.4f}'} |"
        )
        row = {"d": d.isoformat(), "e": e.isoformat(), "n": m["n"], "portfolio": c["portfolio"], "missing": c["missing"], "ic": m["ic"], "d_": m["d"],
               "ic_m1": m1["ic"], "d_m1": m1["d"], "ic_top": mt["ic"], "d_top": mt["d"], "ic_nohft": mh["ic"], "perm_p": p}  # fmt: skip
        if name == "H1":  # sensitivity: outcome from the all-time PnL difference of the two snapshots
            roi2 = {a: (end[a]["pnl_all"] - snap[a]["pnl_all"]) / snap[a]["account_value"] for a in uni
                    if a in end and end[a].get("pnl_all") is not None and snap[a].get("pnl_all") is not None}  # fmt: skip
            m2 = smf.h1_period(uni, sig, roi2)
            row.update(ic_pnlall=m2["ic"], d_pnlall=m2["d"], n_pnlall=m2["n"])
            emit(f"- чутливість (наслідок pnl_all(кінець) − pnl_all(d)): n {m2['n']}, IC {f4(m2['ic'])}, D {f4(m2['d'], True)}")
        if d in prep.classes_stage0:  # M2 sensitivity: classes on 2026-10-16 from the stage-0 positions (17.10)
            uni0 = smf.h1_universe(snap, prep.classes_stage0[d], field)
            for a in uni0:
                if a not in fb and not (a in end and end[a].get(field) is not None):
                    pf = load_cached(cache, "portfolio", a)
                    if isinstance(pf, dict):
                        pf = {w: {"pnl": [tuple(x) for x in (b.get("pnl") or [])]} for w, b in pf.items() if isinstance(b, dict)}
                        fb[a] = smf.portfolio_change(pf, int(snap[a]["ts"]) * 1000, int(t_end))
            roi0, _ = smf.h1_outcomes(uni0, snap, end, field, fb)
            m0 = smf.h1_period(uni0, {a: snap[a][field] for a in uni0}, roi0)
            row.update(ic_stage0=m0["ic"], d_stage0=m0["d"], n_stage0=m0["n"])
            emit(f"- чутливість (класи на {d} з позицій стадії 0, 17.10): n {m0['n']}, IC {f4(m0['ic'])}, D {f4(m0['d'], True)}")
        per.append(row)
    key = "h1" if name == "H1" else "h1w"
    res: dict[str, Any] = {"periods": per}
    for met, col in (("ic", "ic"), ("d", "d_")):
        s = mean_t_ci([r[col] for r in per if r[col] is not None])
        sd = max(RETRO_SD[f"{key}_{met}"], s["sd"] or 0.0)
        mde_v = smf.Z80 * sd / math.sqrt(s["n"]) if s["n"] else None
        gate = mde_v is not None and mde_v <= GATES[f"{key}_{met}"]
        res[met] = {**s, "sd_used": sd, "mde": mde_v, "gate": gate}
        emit(
            f"- {met.upper()}: T = {s['n']}, середнє {f4(s['mean'], met == 'd')}, 95 % CI [{f4(s['lo'], met == 'd')}; {f4(s['hi'], met == 'd')}], "
            f"sd між періодами {f4(s['sd'], met == 'd')}, sd для воріт {f4(sd, met == 'd')}, MDE {f4(mde_v, met == 'd')} → ворота "
            f"({f4(GATES[f'{key}_{met}'], met == 'd')}) {'пройдено' if gate else 'НЕ пройдено'}"
        )
    # R1: no H1 / H1w verdict is coded; the gate lines above are read for a decision only on a decisive look
    res["decision"] = "descriptive" if look is None else "not_coded"
    emit(
        f"- рішення {name}: "
        + (
            "`descriptive` (as-of не вирішальний; рядки вище — лише опис)"
            if look is None
            else "у коді не ухвалюється (правило реєстрації 17.10 застосовується у звіті)"
        )
    )
    emit("")
    return res


def h2_block(prep: Prep, cache: Path, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    emit("### H2: потік сильних акаунтів за 24 год → дохідність монети мінус BTC (закриття d → закриття d + h)")
    closes = {
        c: daily_closes(load_cached(cache, "candles", c), prep.as_of_s)
        for c in sorted({c for r in prep.h2.values() for k in ("signal", "signal_early") for c in r.get(k) or {}} | {"BTC"})
    }
    btc = closes.get("BTC") or {}
    res: dict[str, Any] = {}
    for h in H2_HORIZONS:
        days = smf.matured_days(H2_FIRST, prep.h2_days[-1], h, prep.as_of_day)
        ics, spreads, nets, used = [], [], [], 0
        for d in days:
            rec = prep.h2[d]
            if rec.get("skip") is not None:
                continue
            out = {c: smf.relative_return(closes.get(c) or {}, btc, d, h) for c in rec["signal"]}
            m = smf.h2_day(rec["signal"], out)
            if m["ic"] is None:
                continue
            used += 1
            ics.append(m["ic"])
            spreads.append(m["spread"])
            nets.append(m["spread_net"])
        lag = h - 1
        s_ic, s_net, s_gross = smf.nw_summary(ics, lag), smf.nw_summary(nets, lag), smf.nw_summary(spreads, lag)
        gate = s_ic["mde"] is not None and s_ic["mde"] <= GATES["h2_ic"]
        res[f"h{h}"] = {"days": len(days), "used": used, "ic": s_ic, "spread_net": s_net, "spread": s_gross, "gate": gate}
        emit(
            f"- h = {h}{' (основний)' if h == 7 else ''}: днів {used} з {len(days)}; IC середнє {f4(s_ic['mean'])} [{f4(s_ic['lo'])}; {f4(s_ic['hi'])}] "
            f"(NW лаг {lag}), MDE {f4(s_ic['mde'])} → ворота 0.03 {'пройдено' if gate else 'НЕ пройдено'}; спред топ − низ {f4(s_gross['mean'], True)}, "
            f"після витрат 2 × 0.30 % {f4(s_net['mean'], True)} [{f4(s_net['lo'], True)}; {f4(s_net['hi'], True)}]"
        )
    # M3: the registered H2 rule, coded (h = 7 only; CI bounds at the corrected level, 2 routes x 3 looks)
    r7 = res["h7"]
    # R1: decided once, on the first decisive look with the gate passed; other looks and as-ofs are descriptive
    look = smf.decisive_look(prep.as_of_s)
    before = None if prior is None else prior.get("h2_decided_at_s")
    earlier = False if look == 1 else (None if prior is None else before is not None)
    dec = smf.h2_decision(r7["ic"], r7["spread_net"], r7["gate"], look=look, earlier_decided=earlier)
    dec["decided_at_s"] = prep.as_of_s if dec["verdict"] in ("confirmed", "rejected") else before
    res["decision"] = dec
    emit(
        f"- рішення H2 (h = 7, зареєстроване правило, CI {dec['level'] * 100:.2f} %): {dec['verdict']}; нижня межа IC {f4(dec['ic_lo'])}, "
        f"спреду після витрат {f4(dec['spread_lo'], True)}; `rejected` діє лише для прочитання «вхід від денного закриття d»"
        + ("" if before is None else f"; рішення вже ухвалено на зрізі {datetime.fromtimestamp(before, UTC):%Y-%m-%d}")
    )
    # M4 sensitivity (descriptive, no effect on the verdict): entry at d 01:00 (1h candle), exit d + h 01:00, strong
    # set from snapshot rows written before the day's run
    h1c = {c: hour1_closes(load_cached(cache, "candles1h", c), prep.as_of_s) for c in sorted(closes)}
    btc1 = h1c.get("BTC") or {}
    for h in H2_HORIZONS:
        days = smf.matured_days(H2_FIRST, prep.h2_days[-1], h, prep.as_of_day)
        ics, nets = [], []
        for d in days:
            rec = prep.h2[d]
            sig = rec.get("signal_early") or {}
            if rec.get("skip") is not None or len(sig) < smf.H2_MIN_COINS:
                continue
            m = smf.h2_day(sig, {c: smf.relative_return(h1c.get(c) or {}, btc1, d, h) for c in sig})
            if m["ic"] is not None:
                ics.append(m["ic"])
                nets.append(m["spread_net"])
        s_ic, s_net = smf.nw_summary(ics, h - 1), smf.nw_summary(nets, h - 1)
        res[f"h{h}_entry_0100"] = {"ic": s_ic, "spread_net": s_net}
        emit(
            f"- чутливість (описово), вхід d 01:00, h = {h}: днів {s_ic['n']}; IC {f4(s_ic['mean'])} [{f4(s_ic['lo'])}; {f4(s_ic['hi'])}], "
            f"спред після витрат {f4(s_net['mean'], True)} [{f4(s_net['lo'], True)}; {f4(s_net['hi'], True)}]"
        )
    no_px = sorted(c for c, v in closes.items() if not v)
    emit(f"- монет без денних закриттів HL: {len(no_px)}{' (' + ', '.join(no_px[:20]) + ')' if no_px else ''}")
    emit("")
    return res


def h3_block(prep: Prep, cache: Path, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """H3 by the registration plus the slice-1 fix round (2026-11-26, before outcomes): first appearance of each
    (wallet, token) pair only; ranking at the event's ts; main sample by the *scheduled* endDate <= as-of − 7 days;
    a Gamma data check (> 2 % missing -> stop); unresolved share (> 5 % -> no decision); CI at the corrected level;
    guards and concentration (``smf.h3_evaluate``); status by replication (``smf.h3_status``). ``prior``: the previous
    decisive slice ({"as_of_s", "overall", "crypto"} = criterion met there): then the main sample is the replication
    window (endDate after the previous slice's as-of − 7 days)."""
    emit("### H3: нові позиції топ-гаманців Polymarket (edge = outcome − p_obs − 0.01)")
    prior = accepted_prior(prior, prep.as_of_s)  # R1: before any market is read
    look = smf.decisive_look(prep.as_of_s)
    gm = gamma_map(cache)
    cand = [e for e in prep.pm_events if e["in_range"]]
    # m3: market data quality, before any outcome is read
    dq: Counter = Counter()
    bad_tok: set[str] = set()
    for e in cand:
        if not e["first_pair"]:
            continue
        mk = gm.get(e["market"])
        why = "no_market" if mk is None else None
        if mk is not None and e["market"] not in _ids(mk):
            why = "token_not_in_market"
        elif mk is not None and smf.gamma_end_ts(mk) is None:
            why = "no_end_date"
        dq[why or "ok"] += 1
        if why:
            bad_tok.add(e["market"])
    n_fp = sum(dq.values())
    toks = {e["market"] for e in cand if e["first_pair"]}
    share_pos = (n_fp - dq["ok"]) / n_fp if n_fp else 0.0
    share_tok = len(bad_tok) / len(toks) if toks else 0.0
    emit(
        "- якість даних Gamma (перша поява, p у діапазоні): "
        + ", ".join(f"{k} {v}" for k, v in sorted(dq.items()))
        + f"; частка без ринку / endDate: позицій {share_pos:.4f}, токенів {share_tok:.4f} (стоп > {smf.H3_GAMMA_MISSING_MAX})"
    )
    res: dict[str, Any] = {"data_quality": dict(dq), "missing_share": max(share_pos, share_tok)}
    if max(share_pos, share_tok) > smf.H3_GAMMA_MISSING_MAX:
        emit("- СТОП: частка відсутніх ринків > 2 %, перевірити пакетування Gamma; H3 не рахується")
        res["stopped"] = True
        return res
    end_after = prior["as_of_s"] - smf.H3_UMA_GRACE_S if prior else None
    rows, samples = [], Counter()
    for e in cand:
        mk = gm.get(e["market"])
        if mk is None:
            samples["no_market"] += 1
            continue
        close, end = smf.gamma_close_ts(mk), smf.gamma_end_ts(mk)
        s = smf.h3_sample(e["ts"], end, close, prep.as_of_s, end_after)
        samples[f"{'first' if e['first_pair'] else 'repeat'}:{s}"] += 1
        if s in ("no_end_date", "seen_at_or_after_end", "closed_before_observation", "earlier_slice"):
            continue
        y, why = smf.gamma_resolution(mk, e["market"], prep.as_of_s)
        rows.append(
            {
                **e,
                "sample": s,
                "y": y,
                "why": why,
                "close": close,
                "end": end,
                "cluster": str(mk.get("conditionId") or e["title"] or e["market"]),
                "edge": None if y is None else smf.h3_edge(y, e["p_obs"]),
            }
        )
    del gm
    emit("- вибірки (перша поява / повтор : клас за запланованим endDate): " + ", ".join(f"{k} {v}" for k, v in sorted(samples.items())))
    res["samples"] = dict(samples)
    level = smf.DECISION_LEVEL

    def summary(label: str, s: list[dict[str, Any]], lvl: float = 0.95) -> dict[str, Any]:
        c = smf.cluster_mean_ci([r["edge"] for r in s], [r["cluster"] for r in s], level=lvl)
        own = [(r["y"] - r["entry_px"], r["cluster"]) for r in s if r["entry_px"] is not None]
        co = smf.cluster_mean_ci([x for x, _ in own], [k for _, k in own])
        emit(
            f"- {label}: позицій {c['n']}, ринків {c['clusters']}, гаманців {len({r['account'] for r in s})}; edge {f4(c['mean'])} "
            f"[{f4(c['lo'])}; {f4(c['hi'])}] (CI {lvl * 100:.2f} %); трейдер (outcome − entry_px) {f4(co['mean'])} [{f4(co['lo'])}; {f4(co['hi'])}]"
        )
        return {"edge": c, "own": co}

    for rk, label in (("overall", "загальний рейтинг"), ("crypto", "крипто-рейтинг")):
        main_all = [r for r in rows if r[rk] and r["first_pair"] and r["sample"] == "main"]
        unres = [r for r in main_all if r["y"] is None]
        mk_all = {r["cluster"] for r in main_all}
        mk_unres = {r["cluster"] for r in unres}
        share = max(len(unres) / len(main_all), len(mk_unres) / len(mk_all)) if main_all else None
        emit(
            f"- {label}: основна вибірка (endDate ≤ as-of − 7 діб) позицій {len(main_all)}, ринків {len(mk_all)}; не вирішено позицій {len(unres)}, "
            f"ринків {len(mk_unres)} → частка {'—' if share is None else f'{share:.4f}'} (без рішення > {smf.H3_UNRESOLVED_MAX}); "
            "причини: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r["why"] for r in unres).items()))
        )
        # m7 (descriptive, enters no decision): main-sample markets that closed > 1 day before their current endDate
        early = {r["cluster"] for r in main_all if smf.early_close(r["end"], r["close"])}
        res[f"{rk}_early_close"] = {"markets": len(early), "of": len(mk_all)}
        emit(
            f"- {label}: ринків основної вибірки з closedTime < endDate − 1 доба (дострокові закриття, m7, описово): {len(early)} з {len(mk_all)}"
        )
        main = [r for r in main_all if r["y"] is not None]
        ev = smf.h3_evaluate(main, unresolved_share=share, level=level)
        prior_met = None if prior is None else bool(prior.get(rk))
        status = smf.h3_status(ev["criterion_met"], ev["decidable"], prior_met, look=look)
        res[rk] = {**ev, "status": status}
        m = ev["main"]
        emit(
            f"- {label} (основна, CI {level * 100:.2f} %): позицій {m['n']}, ринків {m['clusters']}, edge {f4(m['mean'])} [{f4(m['lo'])}; {f4(m['hi'])}]; "
            f"зареєстрований критерій: {'так' if ev['registered'] else 'ні'}"
        )
        for gk, g in ev["guards"].items():
            emit(
                f"  - {gk}: позицій {g['n']}, ринків {g['clusters']}, edge {f4(g['mean'])} [{f4(g['lo'])}; {f4(g['hi'])}] → {'так' if ev['guards_ok'][gk] else 'ні'}"
            )
        emit(
            f"  - частка найбільшого гаманця {f4(ev['max_wallet_share'])} (< 0.25: {'так' if ev['concentration_ok'] else 'ні'}); "
            f"критерій виконано: {'так' if ev['criterion_met'] else 'ні'} → статус **{status}**"
            + (
                " (as-of не вирішальний: лише опис)"
                if look is None
                else ""
                if prior is not None
                else " (один зріз: максимум `candidate`, `confirmed` лише реплікацією)"
            )
        )
        if status == "descriptive":  # R1: no interpretation off a decisive look
            res[rk]["excluded"] = False
        emit(
            f"  - верхня межа CI < +0.03: {'так' if res[rk]['excluded'] else 'ні'} (інтерпретація «зареєстрований ефект виключено на цьому вікні», не `rejected`; статус картки не змінюється)"
        )
        res[f"{rk}_main_95"] = summary(f"{label}, основна, CI 95 % (описово)", main)
        res[f"{rk}_repeats"] = summary(
            f"{label}, повтори пари (описово)",
            [r for r in rows if r[rk] and not r["first_pair"] and r["sample"] == "main" and r["y"] is not None],
        )
        for sk, sl in (
            ("end_after_as_of", "endDate після as-of, вирішені достроково"),
            ("end_in_grace", "endDate в останні 7 діб до as-of"),
        ):
            res[f"{rk}_{sk}"] = summary(
                f"{label}, {sl} (описово)", [r for r in rows if r[rk] and r["first_pair"] and r["sample"] == sk and r["y"] is not None]
            )
    rows = [r for r in rows if r["first_pair"] and r["sample"] == "main" and r["y"] is not None]
    for key, label, sel in (
        (
            "close_gt_24h",
            "загальний, закрився > 24 год після появи",
            lambda r: r["overall"] and r["close"] is not None and r["close"] - r["ts"] > 86400,
        ),
        (
            "close_le_24h",
            "загальний, закрився ≤ 24 год після появи",
            lambda r: r["overall"] and r["close"] is not None and r["close"] - r["ts"] <= 86400,
        ),
    ):
        res[key] = summary(label + " (описово)", [r for r in rows if sel(r)])
    # per-wallet check (5a, main sample): table, heterogeneity, persistence (first half -> second half by time)
    ov = [r for r in rows if r["overall"]]
    by_w: dict[str, list[dict[str, Any]]] = {}
    for r in ov:
        by_w.setdefault(r["account"], []).append(r)
    stats = {}
    for w, s in by_w.items():
        stats[w] = smf.cluster_mean_ci([smf.h3_edge(r["y"], r["p_obs"]) for r in s], [r["cluster"] for r in s])
    het = smf.cochran_q([stats[w]["mean"] for w in sorted(stats)], [stats[w]["se"] for w in sorted(stats)])
    emit(f"- за гаманцями (загальний): гаманців {len(stats)}, у Q {het['k']}; Q {f4(het['q'])}, df {het['df']}, I² {f4(het['i2'])}")
    emit("| гаманець | позицій | ринків | edge | 95 % CI |")
    emit("|---|---:|---:|---:|---|")
    for w in sorted(stats, key=lambda w: (-stats[w]["n"], w))[:10]:
        s = stats[w]
        emit(f"| {w[:10]}… | {s['n']} | {s['clusters']} | {f4(s['mean'])} | [{f4(s['lo'])}; {f4(s['hi'])}] |")
    mid = statistics.median(r["ts"] for r in ov) if ov else 0
    first = {w: [r for r in s if r["ts"] <= mid] for w, s in by_w.items()}
    ranked = sorted(
        (w for w, s in first.items() if len(s) >= 5),
        key=lambda w: (-statistics.fmean(smf.h3_edge(r["y"], r["p_obs"]) for r in first[w]), w),
    )
    top = set(ranked[:10])
    second = [r for r in ov if r["ts"] > mid and r["account"] in set(ranked)]
    for label, s in (
        ("топ-10 першої половини", [r for r in second if r["account"] in top]),
        ("решта", [r for r in second if r["account"] not in top]),
    ):
        c = smf.cluster_mean_ci([smf.h3_edge(r["y"], r["p_obs"]) for r in s], [r["cluster"] for r in s])
        emit(
            f"- персистентність, друга половина, {label}: позицій {c['n']}, ринків {c['clusters']}, edge {f4(c['mean'])} [{f4(c['lo'])}; {f4(c['hi'])}]"
        )
        res[f"persist_{'top' if label.startswith('топ') else 'rest'}"] = c
    res["heterogeneity"] = het
    emit("")
    return res


def fetch_plan(prep: Prep) -> dict[str, list[str]]:
    missing: set[str] = set()
    for periods, field in ((prep.h1, "pnl_month"), (prep.h1w, "pnl_week")):
        for d, e in periods:
            snap, end = prep.hl.get(d), prep.hl.get(e)
            if snap is None or end is None:
                continue
            unis = [smf.h1_universe(snap, prep.classes[d], field)]
            if d in prep.classes_stage0:
                unis.append(smf.h1_universe(snap, prep.classes_stage0[d], field))
            for a in (a for u in unis for a in u):
                if a not in end or end[a].get(field) is None:
                    missing.add(a)
    coins = sorted({c for r in prep.h2.values() for k in ("signal", "signal_early") for c in (r.get(k) or {})} | {"BTC"})
    tokens = sorted({e["market"] for e in prep.pm_events if e["in_range"] and e["first_pair"]})
    return {"portfolio": sorted(missing), "candles": coins, "candles1h": coins, "gamma": tokens}


def registration() -> dict[str, Any]:
    """R1: the registered parameters of this runner and the analytics module; a prior must carry the same."""
    return smf.registration_fingerprint(
        {"runner": {"start": START.isoformat(), "h2_first": H2_FIRST.isoformat(), "h2_horizons": list(H2_HORIZONS), "top_n": TOP_N,
                    "run_lag_s": RUN_LAG_S, "pm_v2_first_run": PM_V2_FIRST_RUN, "retro_sd": RETRO_SD, "gates": GATES}}
    )  # fmt: skip


def prior_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """The previous decisive slice's full-stage JSON -> the fields :func:`smf.check_prior` needs, plus the criterion
    per ranking (met there) and the look at which H2 was decided (None = not yet)."""
    h3 = body.get("h3") or {}
    as_of = body.get("as_of_s")
    return {
        "as_of_s": None if as_of is None else int(as_of),
        "prior_as_of_s": body.get("prior_as_of_s"),
        "stopped": bool(h3.get("stopped")) or not h3,
        "registration": body.get("registration"),
        "status": {rk: (h3.get(rk) or {}).get("status") for rk in ("overall", "crypto")},
        "overall": bool((h3.get("overall") or {}).get("criterion_met")),
        "crypto": bool((h3.get("crypto") or {}).get("criterion_met")),
        "h2_decided_at_s": ((body.get("h2") or {}).get("decision") or {}).get("decided_at_s"),
    }


def load_prior(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return prior_from_body(cmn.read_json(Path(path).expanduser()))


def accepted_prior(prior: dict[str, Any] | None, as_of_s: int) -> dict[str, Any] | None:
    """R1: refuse (ValueError) a prior that :func:`smf.check_prior` does not accept."""
    why = smf.check_prior(prior, as_of_s, registration())
    if why is not None:
        raise ValueError(f"--h3-prior-json refused: {why}")
    return prior


def full(prep: Prep, cache: Path, seed: int, no_fetch: bool, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    prior = accepted_prior(prior, prep.as_of_s)
    look = smf.decisive_look(prep.as_of_s)
    plan = fetch_plan(prep)
    emit(
        f"план запитів: portfolio {len(plan['portfolio'])}, candles {len(plan['candles'])}, candles1h {len(plan['candles1h'])}, gamma {len(plan['gamma'])}"
    )
    if not no_fetch:
        asyncio.run(fetch(prep, cache, plan))
    emit("")
    emit(f"## Зріз: метрики на {datetime.fromtimestamp(prep.as_of_s, UTC):%Y-%m-%d %H:%M} UTC")
    emit(look_line(look, prior))
    emit("")
    return {
        "as_of_s": prep.as_of_s,
        "look": look,
        "prior_as_of_s": None if prior is None else prior["as_of_s"],
        "registration": registration(),
        "h1": h1_block(prep, cache, "H1", prep.h1, "pnl_month", seed, look),
        "h1w": h1_block(prep, cache, "H1w", prep.h1w, "pnl_week", seed + 1000, look),
        "h2": h2_block(prep, cache, prior),
        "h3": h3_block(prep, cache, prior),
    }


def look_line(look: int | None, prior: dict[str, Any] | None) -> str:
    if look is None:
        return (
            "- **as-of не є вирішальним поглядом** (лише 2026-11-25, 2026-12-16, 2027-01-15 00:00 UTC): усі рядки описові, "
            "статуси H3 і рішення H2 — `descriptive`"
        )
    p = "без попереднього зрізу" if prior is None else f"попередній зріз {datetime.fromtimestamp(prior['as_of_s'], UTC):%Y-%m-%d}"
    tail = "" if look == 1 or prior is not None else "; без `--h3-prior-json` зрізу k − 1 статуси H3 і рішення H2 — `descriptive`"
    return f"- вирішальний погляд {look} з {smf.DECISIVE_LOOKS} ({p}){tail}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("coverage", "full"), required=True)
    ap.add_argument("--as-of", default="2026-11-25T00:00:00Z", help="fixed cut-off (UTC ISO)")
    ap.add_argument("--db", default=None, help="store copy (.sqlite); default: online backup of the live store into the cache dir")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--stage0-cache", default=str(STAGE0_CACHE))
    ap.add_argument("--out", default=None, help="text report path (default: <cache>/<stage>.md); JSON next to it")
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--no-fetch", action="store_true", help="full stage from the cache only")
    ap.add_argument(
        "--h3-prior-json",
        default=None,
        help="full-stage JSON of the previous decisive slice (required at looks 2 and 3, refused otherwise): H3 "
        "replication window and status, whether H2 was already decided (slice 1: none)",
    )
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    out = Path(a.out).expanduser() if a.out else cache / f"{a.stage}.md"
    as_of = datetime.fromisoformat(a.as_of.replace("Z", "+00:00")).astimezone(UTC)
    try:  # R1: refuse a wrong prior before any data is read
        prior = accepted_prior(load_prior(a.h3_prior_json), int(as_of.timestamp()))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    with open(out, "w") as fh:
        OUT["fh"] = fh
        emit(f"# smart-money slice 1, stage {a.stage}, as-of {as_of.isoformat()}, seed {a.seed}")
        conn = open_store(a.db, cache)
        prep = Prep(conn, as_of, Path(a.stage0_cache).expanduser())
        res = coverage(prep) if a.stage == "coverage" else full(prep, cache, a.seed, a.no_fetch, prior)
        emit(f"peak RSS {cmn.rss_mb():.0f} MB")
    cmn.write_json(out.with_suffix(".json"), json.loads(json.dumps(res, default=str)))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
