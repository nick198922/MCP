"""Criterion (a) of the forward paper carry: engine decisions vs the backtest code's recalculation.

Registration: docs/ideas/IDEA-20261017-forward-paper-carry.md, "Реєстрація перевірки (а) (квант, 2026-11-25, до прогону)".

Stages (``--stage``):
  counts  structure of the engine's records only (cycles, statuses, git SHAs, params hashes, payload fields, decision
          and log counts, gaps); prints no match rate, no funding amount, no P&L;
  a1      (a.1) replay of the journal (cp_cycle / cp_log / cp_position, plus cp_funding) through the backtest step code;
  a2      (a.2) the backtest step per cycle from the engine's book on freshly downloaded history (funding + 4h bars,
          network only through the runtime providers, cached per file under --cache-dir, paced for Binance, waits
          while the engine runs, stops on a second rate-limit answer).

Frozen after the interim run (registration, review R2): ``_a2_book`` except its download calls, ``_coverage``,
``listed_now``, ``MIN_COVERAGE``, the universe / ``must`` / ``need`` rules, ``spot_map``, ``run_a2``'s verdict (incl. the
forced ◯ of a timing sample), the guard window, and inside ``fetch_funding`` the rule "an incomplete download is a
failed one" (``complete is False`` or ``dropped_rows > 0`` -> ``None``, not cached, the symbol is not checked). May
change for download reliability only: ``Pace``, ``fetch_funding`` (except that rule), ``fetch_bars``, ``PACE_S``,
``QUIET_MIN``, ``RATE_LIMIT_PAUSE_S``, ``MAX_CYCLE_WAIT_S``. ``code_sha256`` hashes both files of the check and the
backtest reference code (analytics/carry.py, analytics/carry_rules.py, collector/market_data.py, review R3): a change
of any of the five after the registration commit -> the report shows both versions, the registration commit's one is
the main; engine fixes only in ``collector/carry_paper_*``.

The DB is opened READ-ONLY (``mode=ro``): the live collector DB or a copy. Output: a compact table on stdout, the full
result (every mismatch / divergence) as JSON in --out.
  uv run python scripts/verify_carry_paper_replay.py --stage counts
  uv run python scripts/verify_carry_paper_replay.py --stage a1 --as-of-ms 1795968000000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import resource
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import carry_replay as cr
from tradedesk.analytics.carry import _Leg, _Market

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
CACHE = Path.home() / ".cache/tradedesk-agents/carry_replay"
FINAL_AS_OF_MS = 1795968000000  # 2026-11-29 16:00 UTC: last cycle whose decision (16:05) is inside the registered period
INTERIM_AS_OF_MS = 1795564800000  # 2026-11-25 00:00 UTC: interim cut-off, fixed in the registration before any run
H8, DAY = 8 * 3_600_000, 86_400_000
BOOKS = {"A": "cc-paper", "B": "pp-paper"}
# Binance shares the collector's IP: fundingRate (and fundingInfo) have their own 500 / 5 min budget, the engine uses up
# to ~400 of it per cycle. The run takes at most 100 PAGES / 5 min (one page per 3 s: a 2-page symbol waits 6 s),
# klines one request per 0.5 s, and waits out the first 30 min after each funding time (00 / 08 / 16 UTC) and then
# until both books' current cycle is 'done'. A rate-limit answer (or a read cut short) pauses >= 5 min; the second
# one stops (a.2) with ◯ (the per-file cache stays).
PACE_S = {
    ("binanceusdm", "funding"): 3.0,
    ("binanceusdm", "bars"): 0.5,
    ("binance", "bars"): 0.5,
    ("bybit", "funding"): 0.2,
    ("bybit", "bars"): 0.2,
}
QUIET_MIN = 30
RATE_LIMIT_PAUSE_S = 300
MAX_CYCLE_WAIT_S = 7200  # the engine's current cycle still not done 2 h after the funding time: stop, do not guess
MIN_COVERAGE = 0.95  # (a.2) is ◯ when funding history downloads for fewer of the universe symbols


def _t(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def _rss() -> str:
    return f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _con(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def load_journal(db: str, strategy: str) -> dict[str, Any]:
    con = _con(db)
    try:
        cyc = [dict(r) for r in con.execute("SELECT * FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts", (strategy,))]
        logs = [dict(r) for r in con.execute("SELECT * FROM cp_log WHERE strategy = ? ORDER BY id", (strategy,))]
        pos = [dict(r) for r in con.execute("SELECT * FROM cp_position WHERE strategy = ? ORDER BY opened_ts, trade_id", (strategy,))]
    finally:
        con.close()
    return cr.journal_from_rows(cyc, logs, pos)


def load_funding(db: str, venues: tuple[str, ...]) -> dict[tuple[str, str], list[tuple[int, float]]]:
    con = _con(db)
    out: dict[tuple[str, str], list[tuple[int, float]]] = {}
    try:
        for v in venues:
            for r in con.execute("SELECT symbol, ts, rate FROM cp_funding WHERE venue = ? ORDER BY symbol, ts", (v,)):
                out.setdefault((v, r["symbol"]), []).append((int(r["ts"]), float(r["rate"])))
    finally:
        con.close()
    return out


def cycle_done_from_db(db: str) -> Callable[[int], bool]:
    """``f(cycle_ts)``: both books have that cycle recorded as 'done' (the engine is no longer fetching for it)."""

    def done(cycle_ts: int) -> bool:
        con = _con(db)
        try:
            q = "SELECT COUNT(DISTINCT strategy) FROM cp_cycle WHERE cycle_ts = ? AND status = 'done' AND strategy IN ('A', 'B')"
            return int(con.execute(q, (cycle_ts,)).fetchone()[0]) == 2
        finally:
            con.close()

    return done


def _venues(strategy: str) -> tuple[str, ...]:
    return (cr.PERP_VENUE,) if strategy == "A" else (cr.PERP_VENUE, cr.BYBIT)


def _params(strategy: str) -> Any:
    from tradedesk.collector.carry_paper_engine import PARAMS

    return PARAMS[strategy]


CODE_FILES = ("carry_replay.py", "verify_carry_paper_replay.py", "carry.py", "carry_rules.py", "market_data.py")


def code_sha256() -> dict[str, str]:
    """Hashes of both files of the check (carry_replay.py and this runner, whose universe / coverage rules are frozen
    too) AND of the backtest reference code they call (review R3): analytics/carry.py (``_Leg``, ``_Market``,
    observers, steps), analytics/carry_rules.py (rules) and collector/market_data.py (``funding_entries``, ``bars_4h``
    parsing). The final report compares them with the registration commit's versions (or shows both results). Paths
    are taken next to carry_replay.py, so nothing is imported for this."""
    pkg = Path(cr.__file__).resolve().parent  # src/tradedesk/analytics
    files = {
        "carry_replay.py": pkg / "carry_replay.py",
        "verify_carry_paper_replay.py": Path(__file__),
        "carry.py": pkg / "carry.py",
        "carry_rules.py": pkg / "carry_rules.py",
        "market_data.py": pkg.parent / "collector" / "market_data.py",
    }
    return {k: hashlib.sha256(files[k].read_bytes()).hexdigest()[:16] for k in CODE_FILES}


def _sha_line(h: dict[str, str]) -> str:
    return "sha256 " + ", ".join(f"{k} {v}" for k, v in h.items())


def _venue_shares(ig: dict[str, Any]) -> str:
    """Per-venue funding guard (registered: trips if ANY venue's share of differing legs exceeds the limit)."""
    return "by venue " + ", ".join(
        f"{v} {c['legs_with_difference']}/{c['compared']} = {c['share']} (not compared {c['not_compared']}) -> {c['state']}"
        for v, c in ig.get("by_venue", {}).items()
    )


# ---------- counts ----------


def counts(db: str, as_of_ms: int) -> dict[str, Any]:
    """Structure only: nothing here is a match rate, a funding amount or a P&L."""
    con = _con(db)
    out: dict[str, Any] = {"db": db, "as_of_ms": as_of_ms}
    try:
        for S in ("A", "B"):
            j = load_journal(db, S)
            cyc = [c for c in j["cycles"] if int(c["cycle_ts"]) <= as_of_ms]
            done = [c for c in cyc if c["status"] == "done"]
            ts = [int(c["cycle_ts"]) for c in cyc]
            holes = (ts[-1] - ts[0]) // H8 + 1 - len(ts) if ts else 0
            shas: dict[str, list[int]] = {}
            for c in cyc:
                shas.setdefault(str(c.get("git_sha")), []).append(int(c["cycle_ts"]))
            reasons: dict[str, int] = {}
            for c in cyc:
                if c["status"] != "done":
                    k = f"{c['status']}: {str(c.get('reason') or '').split(':')[0]}"
                    reasons[k] = reasons.get(k, 0) + 1
            keys: dict[str, int] = {}
            actions: dict[str, int] = {}
            issues: dict[str, int] = {}
            trunc = stopped = n_cand = mism = 0
            first_stop = None
            for c in done:
                pl = c["payload"]
                for k in pl:
                    keys[k] = keys.get(k, 0) + 1
                for d in pl.get("decisions") or []:
                    actions[d["action"]] = actions.get(d["action"], 0) + 1
                for f in pl.get("flags") or []:
                    issues[str(f.get("issue"))] = issues.get(str(f.get("issue")), 0) + 1
                n_cand += len(pl.get("candidates") or [])
                trunc += cr.excluded_truncated(pl)
                if pl.get("entries_stopped"):
                    stopped += 1
                    first_stop = first_stop or int(c["cycle_ts"])
                mism += bool(pl.get("interval_mismatches"))
            logs = [r for r in j["logs"] if int(r["cycle_ts"]) <= as_of_ms]
            kinds: dict[str, int] = {}
            ev: dict[str, int] = {}
            fields: dict[str, set[str]] = {}
            for r in logs:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                fields.setdefault(r["kind"], set()).update(r["payload"])
                if r["kind"] == "event":
                    ev[str(r["payload"].get("type"))] = ev.get(str(r["payload"].get("type")), 0) + 1
            pos = j["positions"]
            out[S] = {
                "cycles": len(cyc), "done": len(done), "not_done": reasons, "grid_holes": holes,
                "first": _t(ts[0]) if ts else None, "last": _t(ts[-1]) if ts else None,
                "git_sha": {k: {"n": len(v), "first": _t(min(v)), "last": _t(max(v))} for k, v in shas.items()},
                "params_hash": sorted({str(c.get("params_hash")) for c in cyc}), "registered_hash": _params(S).hash(),
                "payload_keys": keys, "decisions_by_action": actions, "candidates": n_cand, "flags_by_issue": issues,
                "cycles_excluded_truncated": trunc, "cycles_entries_stopped": stopped, "first_entries_stopped": _t(first_stop),
                "cycles_with_interval_mismatches": mism, "log_rows_by_kind": kinds, "events_by_type": ev,
                "log_fields": {k: sorted(v) for k, v in fields.items()},
                "positions": {"total": len(pos), "open_now": sum(p["status"] == "open" for p in pos),
                              "closed_now": sum(p["status"] == "closed" for p in pos),
                              "forced_or_manual": sum(p.get("exit_reason") in ("forced", "manual") for p in pos)},
            }  # fmt: skip
        fr = {}
        for r in con.execute("SELECT venue, COUNT(*) n, COUNT(DISTINCT symbol) s, MIN(ts) a, MAX(ts) b FROM cp_funding GROUP BY venue"):
            fr[r["venue"]] = {"rows": r["n"], "symbols": r["s"], "first": _t(r["a"]), "last": _t(r["b"])}
        out["cp_funding"] = fr
    finally:
        con.close()
    return out


def show_counts(c: dict[str, Any]) -> None:
    print(f"DB {c['db']} as of {_t(c['as_of_ms'])} UTC, {_rss()}")
    for S in ("A", "B"):
        x = c[S]
        print(f"\n== {S} ({BOOKS[S]}): cycles {x['cycles']} ({x['first']} .. {x['last']}), done {x['done']}, grid holes {x['grid_holes']}")
        print(f"  not done: {x['not_done']}")
        print(f"  params_hash {x['params_hash']} (registered {x['registered_hash']})")
        multi = {k: v["n"] for k, v in x["git_sha"].items() if v["n"] > 1}
        print(f"  git SHAs: {len(x['git_sha'])} distinct over {x['cycles']} cycles (more than one cycle: {multi}); per SHA in --out")
        print(f"  decisions by action {x['decisions_by_action']}, candidates {x['candidates']}")
        print(f"  entries_stopped cycles {x['cycles_entries_stopped']} (from {x['first_entries_stopped']}); excluded truncated in "
              f"{x['cycles_excluded_truncated']} cycles; interval_mismatches in {x['cycles_with_interval_mismatches']}")  # fmt: skip
        print(f"  flags by issue {x['flags_by_issue']}")
        print(f"  log rows {x['log_rows_by_kind']}; events {x['events_by_type']}")
        print(f"  positions {x['positions']}")
        print(f"  payload keys {sorted(x['payload_keys'])}")
        for k, v in x["log_fields"].items():
            print(f"  {k} fields: {', '.join(v)}")
    for v, r in c["cp_funding"].items():
        print(f"cp_funding {v}: {r['rows']} rows, {r['symbols']} symbols, {r['first']} .. {r['last']}")


# ---------- a1 ----------


def run_a1(db: str, as_of_ms: int, *, keep_books: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for S in ("A", "B"):
        t0 = time.time()
        j = load_journal(db, S)
        rows = load_funding(db, _venues(S))
        res = cr.replay_a1(cr.rules_from(_params(S)), j, as_of_ms=as_of_ms, funding_rows=rows, keep_books=keep_books)
        res["seconds"] = round(time.time() - t0, 1)
        out[S] = res
        print(f"{S}: replayed {res['cycles']} in {res['seconds']} s, {_rss()}", flush=True)
    return out


def show_a1(res: dict[str, Any]) -> None:
    print("\n| book | cycles | decisions matched | accounting matched | APR vs cp_funding | payments vs cp_funding | (a.1) |")
    print("|---|---|---|---|---|---|---|")
    for S, r in res.items():
        a, f, p = r["decisions"], r["apr_vs_cp_funding"], r["payments_vs_cp_funding"]
        print(
            f"| {S} {BOOKS[S]} | {r['cycles']} | {a['matched']}/{a['total']} | {r['accounting']['matched']}/{r['accounting']['total']} | "
            f"{f['matched']}/{f['total']} (exempt {f['exempt_by_reason']}) | {p['matched']}/{p['total']} (exempt {p['exempt_by_reason']}) | "
            f"{r['status']} |"
        )
    for S, r in res.items():
        print(f"\n{S} by action: {r['decisions']['by_action']}; unverifiable: {r['unverifiable']}")
        print(f"  grid holes {[_t(x) for x in r['grid_holes']]}; aside {len(r['aside'])} (list in --out); "
              f"cycles after the stop date without the flag: {len(r['cycles_after_stop_without_flag'])}")  # fmt: skip
        for x in r["aside"][:20]:
            print(f"    aside {_t(x['cycle_ts'])} {x['symbol']} {x['issue']} detail={x['detail']} gap_cycles={x['gap_cycles']}")
        bad = {
            sha: v
            for sha, v in r["per_git_sha"].items()
            if v["decisions"][0] != v["decisions"][1] or v["accounting"][0] != v["accounting"][1]
        }
        print(f"  git SHAs with a mismatch: {len(bad)} of {len(r['per_git_sha'])}")
        for sha, v in bad.items():
            print(
                f"  git {sha}: cycles {v['cycles'][0]}, decisions {v['decisions'][1]}/{v['decisions'][0]}, accounting {v['accounting'][1]}/{v['accounting'][0]}"
            )
        kinds: dict[str, int] = {}
        for m in r["mismatches"]:
            kinds[f"{m['kind']}:{m['field']}"] = kinds.get(f"{m['kind']}:{m['field']}", 0) + 1
        print(f"  mismatches by kind {kinds}")
        for m in r["mismatches"][:15]:
            print(f"    {_t(m['cycle_ts'])} {m['kind']} {m['symbol']} {m['field']}: engine {m['engine']} replay {m['replay']}")


# ---------- a2 ----------


class RateLimitStop(Exception):
    """(a.2) stops: a second rate-limit answer, or the engine's cycle not done in time. The cache stays."""


def _retry_after_s(detail: str) -> float | None:
    m = re.search(r"retry[- _]?after\D{0,5}(\d+)", detail or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


class Pace:
    """Request pacing per (venue, kind) by PAGES, the engine's quiet window, and the rate-limit back-off.

    ``cycle_done(cycle_ts)``: both books have the cycle 'done' (None: only the 30-minute window). ``sleep`` /
    ``now_ms`` / ``mono`` are injectable for tests."""

    def __init__(
        self,
        cycle_done: Callable[[int], bool] | None = None,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now_ms: Callable[[], int] = _now_ms,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cycle_done, self.sleep, self.now_ms, self.mono = cycle_done, sleep, now_ms, mono
        self.last: dict[tuple[str, str], float] = {}
        self.done: set[int] = set()
        self.strikes = 0

    async def quiet(self) -> None:
        waited = 0.0
        while True:
            now = self.now_ms()
            boundary = now // H8 * H8
            into = now - boundary
            if into < QUIET_MIN * 60_000:  # the engine's fetch window after 00 / 08 / 16 UTC
                s = (QUIET_MIN * 60_000 - into) / 1000 + 5
                print(f"  quiet window: waiting {s:.0f} s", flush=True)
                await self.sleep(s)
                waited += s
                continue
            if self.cycle_done is None or boundary in self.done:
                return
            if self.cycle_done(boundary):
                self.done.add(boundary)
                return
            if waited >= MAX_CYCLE_WAIT_S:
                raise RateLimitStop(f"cycle {_t(boundary)} of the engine not done after {waited:.0f} s of waiting")
            print(f"  engine cycle {_t(boundary)} not done yet: waiting 60 s", flush=True)
            await self.sleep(60)
            waited += 60

    async def wait(self, venue: str, kind: str) -> None:
        await self.quiet()
        gap = PACE_S.get((venue, kind), 0.5)
        last = self.last.get((venue, kind))
        if last is not None and self.mono() - last < gap:
            await self.sleep(gap - (self.mono() - last))
        self.last[(venue, kind)] = self.mono()

    def note_pages(self, venue: str, kind: str, pages: int) -> None:
        """A request that walked ``pages`` pages: the next one waits one gap per page."""
        if pages > 1 and (venue, kind) in self.last:
            self.last[(venue, kind)] += (pages - 1) * PACE_S.get((venue, kind), 0.5)

    async def rate_limited(self, venue: str, detail: str) -> None:
        self.strikes += 1
        if self.strikes >= 2:
            raise RateLimitStop(f"second rate-limit answer ({venue}: {detail[:160]})")
        s = max(RATE_LIMIT_PAUSE_S, _retry_after_s(detail) or 0.0)
        print(f"  rate limit ({venue}: {detail[:160]}): pausing {s:.0f} s, the next one stops (a.2)", flush=True)
        await self.sleep(s)


def _cache_file(cache: Path, venue: str, sym: str, kind: str, since: int, until: int) -> Path:
    return cache / f"{venue}_{re.sub(r'[^A-Za-z0-9]+', '_', sym)}_{kind}_{since}_{until}.json"


async def fetch_funding(rt: Any, pace: Pace, cache: Path, venue: str, sym: str, since: int, until: int) -> list[dict[str, Any]] | None:
    """Funding history of one leg (cached per file), None when the download failed (counted as not checked)."""
    from tradedesk.collector.market_data import funding_entries
    from tradedesk.core.errors import ProviderError

    p = _cache_file(cache, venue, sym, "funding", since, until)
    if p.exists():
        return json.loads(p.read_text())
    for _ in range(2):
        await pace.wait(venue, "funding")
        try:
            f = await rt.exchanges.funding_rate_history_range(venue, sym, since, until, trust_short_page=venue == cr.PERP_VENUE)
        except ProviderError as e:
            print(f"  funding {venue} {sym}: {e.reason}: {str(e.detail)[:160]}", flush=True)
            if e.reason == "rate_limited":
                await pace.rate_limited(venue, str(e.detail))
                continue
            return None
        except Exception as e:  # noqa: BLE001 - a failed symbol is recorded as not downloaded, the run goes on
            print(f"  funding {venue} {sym}: {type(e).__name__}: {str(e)[:160]}", flush=True)
            return None
        pace.note_pages(venue, "funding", int((getattr(f.evidence, "args", None) or {}).get("pages", 1)))
        data = f.data or {}
        if data.get("truncated"):  # a later page failed (on Binance most likely 429 / 418): back off like a rate limit
            print(f"  funding {venue} {sym}: truncated (a later page failed), not cached", flush=True)
            await pace.rate_limited(venue, "truncated read: a later page failed")
            continue
        if data.get("complete") is False or int(data.get("dropped_rows") or 0) > 0:  # lost rows would look like feed gaps
            print(f"  funding {venue} {sym}: incomplete (complete={data.get('complete')}, dropped_rows="
                  f"{data.get('dropped_rows')}), not cached: not checked", flush=True)  # fmt: skip
            return None
        rows = funding_entries(data.get("rows") or [])
        p.write_text(json.dumps(rows))
        return rows
    return None


async def fetch_bars(rt: Any, pace: Pace, cache: Path, venue: str, sym: str, since: int, until: int) -> list[dict[str, Any]] | None:
    from tradedesk.collector.market_data import bars_4h
    from tradedesk.core.envelope import Builder

    p = _cache_file(cache, venue, sym, "bars4h", since, until)
    if p.exists():
        return json.loads(p.read_text())
    for _ in range(2):
        await pace.wait(venue, "bars")
        b = Builder()
        bars = await bars_4h(rt, b, venue, sym, since, until)
        limited = [m for m in b.missing if m.reason == "rate_limited"]
        if limited:
            print(f"  bars {venue} {sym}: rate_limited: {str(limited[0].detail)[:160]}", flush=True)
            await pace.rate_limited(venue, str(limited[0].detail))
            continue
        if bars is None or b.missing or b.warnings:
            why = [f"{m.reason}: {str(m.detail)[:80]}" for m in b.missing] + [str(w)[:80] for w in b.warnings]
            print(f"  bars {venue} {sym}: unavailable or incomplete ({why}), not cached", flush=True)
            return None
        p.write_text(json.dumps(bars))
        return bars
    return None


async def listed_now(rt: Any, strategy: str) -> set[str] | None:
    """Symbols listed (active) now on every leg venue; None when a market list fails (then nothing counts as delisted)."""
    sets = []
    for v in _venues(strategy):
        try:
            f = await rt.exchanges.markets(v)
        except Exception as e:  # noqa: BLE001
            print(f"  markets {v}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            return None
        sets.append({s for s, m in (f.data or {}).items() if not (isinstance(m, dict) and m.get("active") is False)})
    return set.intersection(*sets) if sets else None


def spot_map(logs: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """A: perp symbol -> (spot symbol, quantity multiplier) as the engine journaled them on its open side rows."""
    by: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for r in logs:
        if r["kind"] == "side" and r["payload"].get("event") == "open":
            by.setdefault((int(r["cycle_ts"]), str(r.get("trade_id"))), {})[str(r["payload"].get("leg"))] = r["payload"]
    out = {}
    for legs in by.values():
        if "perp" in legs and "spot" in legs and legs["spot"].get("symbol"):
            mult = legs["spot"].get("contract_multiplier", legs["spot"].get("multiplier"))  # the engine writes contract_multiplier
            out[legs["perp"]["symbol"]] = (legs["spot"]["symbol"], int(mult or 1))
    return out


def _coverage(group: set[str], got: set[str], listed: set[str] | None) -> dict[str, Any]:
    base = {s for s in group if listed is None or s in listed}
    have = base & got
    return {"n": len(base), "have": len(have), "rate": len(have) / len(base) if base else None, "lacking": sorted(base - have)}


async def _a2_book(rt: Any, pace: Pace, db: str, S: str, as_of_ms: int, cache: Path, max_symbols: int | None, a1: dict[str, Any]) -> Any:
    from tradedesk.collector.carry_paper_feed import MULT

    rules = cr.rules_from(_params(S))
    j = load_journal(db, S)
    done = [c for c in j["cycles"] if int(c["cycle_ts"]) <= as_of_ms and c["status"] == "done"]
    skipped = sum(1 for c in j["cycles"] if int(c["cycle_ts"]) <= as_of_ms and c["status"] != "done")
    if not done:
        return None
    rows = load_funding(db, _venues(S))
    first_t, last_t = int(done[0]["cycle_ts"]), int(done[-1]["cycle_ts"]) + cr.DECISION_DELAY_MS
    since = (first_t - rules.lookback_ms - 2 * DAY) // H8 * H8
    until = last_t + 3_600_000
    cands = {x["symbol"] for c in done for x in c["payload"].get("candidates") or []}
    held = {s for b in a1["books"].values() for s in b["book"]}
    must = cands | held  # registered: 100 % coverage (funding and bars) of held symbols and recorded candidates
    # Universe: every symbol the engine stored funding for on all legs (its own universe history, no survivorship),
    # plus every recorded candidate and held symbol; capped by --max-symbols only for a timing sample.
    syms = sorted({s for (v, s) in rows if all((w, s) in rows for w in _venues(S))} | must)
    if max_symbols:
        syms = syms[:max_symbols]
    listed = await listed_now(rt, S)
    print(
        f"{S}: {len(syms)} symbols, window {_t(since)} .. {_t(until)}, listed now {None if listed is None else len(listed)}, {_rss()}",
        flush=True,
    )
    t0 = time.time()
    fund: dict[str, dict[str, list[dict[str, Any]]]] = {}
    dl_rows: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for i, s in enumerate(syms):
        legs: dict[str, Any] = {}
        for v in _venues(S):
            legs[v] = await fetch_funding(rt, pace, cache, v, s, since, until)
            if legs[v] is not None:
                dl_rows[(v, s)] = [(int(e["ts_ms"]), float(e["rate"])) for e in legs[v]]
        if all(x is not None for x in legs.values()):
            fund[s] = legs
        if i % 25 == 0:
            print(f"  funding {i + 1}/{len(syms)} {time.time() - t0:.0f} s {_rss()}", flush=True)
    # bars only where they can matter: held / recorded candidates, or a downloaded APR (spread) at the entry level
    times = [int(c["cycle_ts"]) + cr.DECISION_DELAY_MS for c in done]
    need = set(must)
    for s, legs in fund.items():
        lg = [_Leg({"funding": legs[v], "bars": []}, rules.bar_ms) for v in _venues(S)]
        for t in times:
            a = [x.apr(t, rules.lookback_ms) for x in lg]
            if None in a:
                continue
            if (S == "A" and a[0] >= rules.entry_apr) or (S == "B" and abs(a[1] - a[0]) >= rules.entry_apr):
                need.add(s)
                break
    journal_spot = spot_map(j["logs"]) if S == "A" else {}
    markets: dict[str, Any] = {}
    spot_used: dict[str, str] = {}
    todo = sorted(need & set(fund))
    for i, s in enumerate(todo):
        if S == "A":
            if s in journal_spot:
                spot_sym, mult = journal_spot[s]
            else:
                base = s.split("/")[0]
                m = MULT.match(base)
                spot_sym, mult = (f"{m.group(2)}/USDT", int(m.group(1))) if m else (f"{base}/USDT", 1)
            spot_used[s] = spot_sym
            perp = await fetch_bars(rt, pace, cache, cr.PERP_VENUE, s, since, until)
            spot = await fetch_bars(rt, pace, cache, cr.SPOT_VENUE, spot_sym, since, until)
            if perp and spot:
                markets[s] = _Market(
                    {"funding": fund[s][cr.PERP_VENUE], "perp": perp, "spot": [dict(x, close=x["close"] * mult) for x in spot]},
                    rules.bar_ms,
                )
        else:
            ba = await fetch_bars(rt, pace, cache, cr.PERP_VENUE, s, since, until)
            bb = await fetch_bars(rt, pace, cache, cr.BYBIT, s, since, until)
            if ba and bb:
                markets[s] = (
                    _Leg({"funding": fund[s][cr.PERP_VENUE], "bars": ba}, rules.bar_ms),
                    _Leg({"funding": fund[s][cr.BYBIT], "bars": bb}, rules.bar_ms),
                )
        if i % 25 == 0:
            print(f"  bars {i + 1}/{len(todo)} {time.time() - t0:.0f} s {_rss()}", flush=True)
    ix = cr.index_logs(j["logs"])
    # registered cross-symbol guards over the whole window [first cycle - lookback, last decision]: funding legs of the
    # universe (cp_funding on every leg venue), and every price the engine recorded in the done cycles
    in_syms = set(syms)
    integ = cr.funding_integrity(
        {k: v for k, v in rows.items() if k[1] in in_syms}, dl_rows, lo=first_t - rules.lookback_ms, hi=last_t, listed=listed
    )
    prices = cr.price_integrity(S, done, a1["books"], ix, markets, listed=listed)
    guards = {"funding": integ["state"], "prices": prices["state"]}
    print(f"  guards: funding legs {integ['legs_with_difference']}/{integ['compared']} differ (not compared "
          f"{integ['not_compared']}), {_venue_shares(integ)} -> {guards['funding']}; prices {prices['differ']}/{prices['compared']} differ "
          f"(not compared {prices['not_compared']}) -> {guards['prices']}", flush=True)  # fmt: skip
    data = cr.a2_data(markets, dl_rows, rows, listed=listed, bar_ms=rules.bar_ms, guards=guards, spot=spot_used)
    res_rows: list[dict[str, Any]] = []
    truncated: list[int] = []
    for c in done:
        c_ts = int(c["cycle_ts"])
        if cr.excluded_truncated(c["payload"]):
            truncated.append(c_ts)
            continue
        for r in cr.replay_a2_cycle(rules, c, a1["books"][c_ts], ix, data):
            res_rows.append({**r, "cycle_ts": c_ts})
    summary = cr.summarize_a2(res_rows, skipped)
    reasons = summary.setdefault("incomplete_reasons", [])
    cov = {
        "must": _coverage(must, set(markets), listed),
        "need": _coverage(need, set(markets), listed),
        "universe_funding": _coverage(set(syms), set(fund), listed),
        "delisted_now": sorted(s for s in need | must if listed is not None and s not in listed),
    }
    # registered: held and candidate symbols 100 % (funding and bars), the rest of ``need`` and the universe's funding
    # >= 95 %; else (a.2) is ◯ (fetch again), never a pass. Symbols not listed now are outside these denominators.
    if cov["must"]["rate"] is not None and cov["must"]["rate"] < 1.0:
        reasons.append(f"held / candidate symbols not downloaded: {cov['must']['lacking'][:20]}")
    if cov["need"]["rate"] is not None and cov["need"]["rate"] < MIN_COVERAGE:
        reasons.append(f"funding + bars for {cov['need']['have']}/{cov['need']['n']} of the symbols that can matter")
    if cov["universe_funding"]["rate"] is not None and cov["universe_funding"]["rate"] < MIN_COVERAGE:
        reasons.append(f"funding for {cov['universe_funding']['have']}/{cov['universe_funding']['n']} of the universe")
    if truncated:
        reasons.append(f"{len(truncated)} cycles with a truncated 'excluded' list not checked")
    if max_symbols:
        reasons.append(f"timing sample (--max-symbols {max_symbols}): not a result")
    if reasons:
        summary["incomplete"], summary["passed"] = True, False
    else:
        summary.pop("incomplete_reasons")
    return {
        "summary": summary,
        "coverage": cov,
        "truncated_excluded_cycles": truncated,
        "guards": guards,
        "cp_funding_integrity": integ,
        "price_integrity": prices,
        "symbols": len(syms), "with_funding": len(fund), "with_bars": len(markets), "seconds": round(time.time() - t0),
        "divergences": [r for r in res_rows if not r["match"]],
    }  # fmt: skip


async def run_a2(db: str, as_of_ms: int, cache: Path, max_symbols: int | None, *, pace: Pace | None = None) -> dict[str, Any]:
    from tradedesk.server import build_server

    a1 = run_a1(db, as_of_ms, keep_books=True)
    _, rt = build_server()
    pace = pace or Pace(cycle_done_from_db(db))  # the caller creates ``cache``
    out: dict[str, Any] = {}
    stopped = None
    try:
        for S in ("A", "B"):
            if stopped:
                out[S] = None
                continue
            try:
                out[S] = await _a2_book(rt, pace, db, S, as_of_ms, cache, max_symbols, a1[S])
            except RateLimitStop as e:
                stopped = str(e)
                print(f"(a.2) stopped: {stopped}; the cache is kept, run again later", flush=True)
                out[S] = None
    finally:
        close = getattr(rt, "aclose", None)
        if close is not None:
            await close()
    for S in ("A", "B"):
        a1[S].pop("books", None)
    verdict = {S: cr.verdict(a1[S], (out.get(S) or {}).get("summary")) for S in ("A", "B")}
    if max_symbols:  # a timing sample is never a result, whatever it shows
        verdict = dict.fromkeys(verdict, "◯")
    return {
        "a1": a1,
        "a2": out,
        "a2_stopped": stopped,
        "timing_sample": bool(max_symbols),
        "code_sha256": code_sha256(),
        "verdict": verdict,
    }


CAVEATS = (
    "(a.2) 'funding' variant: a symbol without a recorded candidate gets its APR from cp_funding as stored NOW "
    "(INSERT OR REPLACE, no write time), not as the engine saw it at t.",
)


def show_a2(res: dict[str, Any]) -> None:
    if res.get("timing_sample"):
        print("TIMING SAMPLE (--max-symbols): not a result, the verdict is forced to ◯")
    show_a1(res["a1"])
    print(f"\n{_sha_line(res['code_sha256'])}; (a.2) stopped: {res['a2_stopped']}")
    for c in CAVEATS:
        print(f"caveat: {c}")
    print("| book | decisions matched (rate) | open/close matched (rate) | divergences by class | skipped cycles | (a.2) | (a) |")
    print("|---|---|---|---|---|---|---|")
    for S, r in res["a2"].items():
        if r is None:
            print(f"| {S} {BOOKS[S]} | — | — | — | — | ◯ | {res['verdict'][S]} |")
            continue
        s = r["summary"]
        state = "incomplete" if s.get("incomplete") else "pass" if s["passed"] else "FAIL"
        print(
            f"| {S} {BOOKS[S]} | {s['matched']}/{s['decisions']} ({s['rate']}) | {s['open_close_matched']}/{s['open_close']} "
            f"({s['open_close_rate']}) | {s['divergences_by_class']} | {s['skipped_cycles']} | {state} | {res['verdict'][S]} |"
        )
    for S, r in res["a2"].items():
        if r is None:
            continue
        s, cov, ig = r["summary"], r["coverage"], r["cp_funding_integrity"]
        print(f"\n{S}: by action [n, matched] {s['by_action']}; incomplete: {s.get('incomplete_reasons')}")
        print(f"  symbols {r['symbols']}, with funding {r['with_funding']}, with bars {r['with_bars']}, {r['seconds']} s")
        print(f"  coverage must {cov['must']['have']}/{cov['must']['n']}, need {cov['need']['have']}/{cov['need']['n']}, "
              f"universe funding {cov['universe_funding']['have']}/{cov['universe_funding']['n']}; delisted now {cov['delisted_now']}")  # fmt: skip
        pi = r["price_integrity"]
        print(f"  cp_funding vs download: {ig['legs']} legs in the window, compared {ig['compared']}, not compared "
              f"{ig['not_compared']}, not listed {ig['not_listed']}; missing in engine {ig['missing_in_engine']}, extra "
              f"{ig['extra_in_engine']}, rate differs {ig['rate_diff']}; worst {ig['worst'][:5]}")  # fmt: skip
        print(f"  GUARD funding: legs with a difference {ig['legs_with_difference']}/{ig['compared']} = {ig['share']} "
              f"(max {ig['max_share']}) pooled -> {ig['pooled_state']}; {_venue_shares(ig)} -> {ig['state']}")  # fmt: skip
        print(f"  GUARD prices: recorded {pi['recorded']}, compared {pi['compared']}, differ {pi['differ']} (stale "
              f"{pi['stale']}), not compared {pi['not_compared']} (no bars {pi['not_downloaded']}, no bar at t "
              f"{pi['download_gap']}), not listed {pi['not_listed']}: share {pi['share']} (max {pi['max_share']}) -> "
              f"{pi['state']}; worst {pi['worst'][:5]}")  # fmt: skip
        print(f"  slot competition {s['slot_competition']}; data_hold where the backtest would have STOPPED the set-aside "
              f"position: {len(s['data_hold_stops'])} {[[_t(c), x] for c, x in s['data_hold_stops'][:20]]}")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("counts", "a1", "a2"), required=True)
    ap.add_argument("--db", default=DB, help="collector DB or a copy; opened read-only")
    ap.add_argument("--as-of-ms", type=int, default=FINAL_AS_OF_MS, help="last cycle_ts included (UTC ms)")
    ap.add_argument("--cache-dir", default=str(CACHE))
    ap.add_argument("--out", default=None, help="JSON result (default: <cache-dir>/<stage>-<as-of>.json)")
    ap.add_argument("--max-symbols", type=int, default=None, help="a2 timing sample only: first N symbols per book")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    out = Path(a.out) if a.out else cache / f"{a.stage}-{a.as_of_ms}.json"
    if a.stage == "counts":
        res = counts(a.db, a.as_of_ms)
        show_counts(res)
    elif a.stage == "a1":
        res = run_a1(a.db, a.as_of_ms)
        res = {"a1": res, "code_sha256": code_sha256()}
        show_a1(res["a1"])
        print(f"\n{_sha_line(res['code_sha256'])}")
    else:
        (cache / "data").mkdir(parents=True, exist_ok=True)
        res = asyncio.run(run_a2(a.db, a.as_of_ms, cache / "data", a.max_symbols))
        show_a2(res)
    out.write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    print(f"\nfull result: {out} ({_rss()})")


if __name__ == "__main__":
    main()
