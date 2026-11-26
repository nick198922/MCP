"""Secondary need* coverage of check (a.2) of the forward paper carry (EXPLORATORY, amendment after the interim run).

Card: docs/ideas/IDEA-20261017-forward-paper-carry.md, "Поправка після проміжного прогону (квант, 2026-11-25, до
фінального прогону): вторинне покриття `need`". The MAIN result is the registered one, printed by
``scripts/verify_carry_paper_replay.py --stage a2``; this script never changes it and never improves a '✗'.

Offline: no network. Reads the main run's ``a2-<as-of>.json`` (``coverage``, ``summary`` incl. ``incomplete_reasons``,
``verdict``, the (a.1) status), the collector DB opened READ-ONLY (``mode=ro``: cp_cycle, cp_position symbols / open
times, cp_log open sides of A, cp_funding) and the per-file cache of that run's window. It rebuilds the runner's
``need`` / ``must`` / universe coverage from the cache, checks them against the main output (any difference -> the
secondary result is the main one), then removes from ``need`` the symbols the engine recorded in ``excluded`` in EVERY
done cycle up to as-of (``analytics.carry_replay_need``). No P&L is read or printed (no ``trades``, no cp_position
``state`` / ``trade``).

  uv run python scripts/verify_carry_paper_replay_need.py --as-of-ms 1795968000000      # final (after the main a2)
  uv run python scripts/verify_carry_paper_replay_need.py --as-of-ms 1795564800000 --legacy-cache-names  # interim smoke
``--legacy-cache-names``: cache files written before the cache-key fix (docs/changes/2026-11-25-carry-replay-cache-key.md),
i.e. the interim run only; never for the final run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_carry_paper_replay as v  # noqa: E402  (frozen runner: only its helpers are used, nothing is changed)

from tradedesk.analytics import carry_replay as cr  # noqa: E402
from tradedesk.analytics import carry_replay_need as nd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OWN_FILES = (Path(__file__).resolve(), ROOT / "src/tradedesk/analytics/carry_replay_need.py")


def cache_path(cache: Path, venue: str, sym: str, kind: str, since: int, until: int, *, legacy: bool = False) -> Path:
    """The file ``fetch_funding`` / ``fetch_bars`` of the runner write (after the cache-key fix; ``legacy``: before)."""
    p = v._cache_file(cache, venue, sym, kind, since, until)
    return p if legacy else p.with_name(f"{p.stem}_{hashlib.sha1(sym.encode()).hexdigest()[:10]}.json")


def read_cached(p: Path) -> Any:
    return json.loads(p.read_text()) if p.exists() else None


def load_book(db: str, S: str) -> dict[str, Any]:
    """cp_cycle (parsed payload), cp_position symbols and open times, A's open side rows; never ``state`` / ``trade``."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        q = "SELECT cycle_ts, status, payload FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts"
        cycles = [
            {"cycle_ts": int(r["cycle_ts"]), "status": r["status"], "payload": json.loads(r["payload"] or "{}")}
            for r in con.execute(q, (S,))
        ]
        pos = [
            (r["symbol"], int(r["opened_ts"])) for r in con.execute("SELECT symbol, opened_ts FROM cp_position WHERE strategy = ?", (S,))
        ]
        q = "SELECT cycle_ts, trade_id, kind, payload FROM cp_log WHERE strategy = ? AND kind = 'side' ORDER BY id"
        sides = [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in con.execute(q, (S,))]
    finally:
        con.close()
    return {"cycles": cycles, "positions": pos, "sides": sides}


def book_need(db: str, S: str, as_of_ms: int, data: Path, main: dict[str, Any], *, legacy: bool) -> dict[str, Any]:
    from tradedesk.collector.carry_paper_feed import MULT

    rules = cr.rules_from(v._params(S))
    venues = v._venues(S)
    jb = load_book(db, S)
    done = nd.done_cycles(jb["cycles"], as_of_ms)
    first_t, last_t = int(done[0]["cycle_ts"]), int(done[-1]["cycle_ts"]) + cr.DECISION_DELAY_MS
    since = (first_t - rules.lookback_ms - 2 * v.DAY) // v.H8 * v.H8
    until = last_t + 3_600_000
    cands = {x["symbol"] for c in done for x in c["payload"].get("candidates") or []}
    # the runner's ``held`` = symbols in the engine's book before some done cycle, i.e. positions opened in a done cycle
    # before as-of; a position opened in the as-of cycle itself is a recorded candidate of it (checked below via must.n)
    held = {s for s, o in jb["positions"] if o <= as_of_ms}
    must = cands | held
    rows = v.load_funding(db, venues)
    syms = sorted({s for (_, s) in rows if all((w, s) in rows for w in venues)} | must)
    fund: dict[str, dict[str, Any]] = {}
    for s in syms:
        legs = {w: read_cached(cache_path(data, w, s, "funding", since, until, legacy=legacy)) for w in venues}
        if all(x is not None for x in legs.values()):
            fund[s] = legs
    times = [int(c["cycle_ts"]) + cr.DECISION_DELAY_MS for c in done]
    need = nd.need_symbols(S, fund, times, must, entry_apr=rules.entry_apr, lookback_ms=rules.lookback_ms, bar_ms=rules.bar_ms)
    journal_spot = v.spot_map([r for r in jb["sides"] if int(r["cycle_ts"]) <= as_of_ms]) if S == "A" else {}
    markets: set[str] = set()
    for s in sorted(need & set(fund)):
        if S == "A":
            if s in journal_spot:
                spot_sym = journal_spot[s][0]
            else:
                base = s.split("/")[0]
                m = MULT.match(base)
                spot_sym = f"{m.group(2)}/USDT" if m else f"{base}/USDT"
            legs2 = [(cr.PERP_VENUE, s), (cr.SPOT_VENUE, spot_sym)]
        else:
            legs2 = [(cr.PERP_VENUE, s), (cr.BYBIT, s)]
        if all(read_cached(cache_path(data, w, x, "bars4h", since, until, legacy=legacy)) for w, x in legs2):
            markets.add(s)
    cov_main = main["coverage"]
    listed = nd.listed_from_delisted(set(syms) | need | must, cov_main.get("delisted_now") or [])
    mine = {
        "must": nd.coverage(must, markets, listed),
        "need": nd.coverage(need, markets, listed),
        "universe_funding": nd.coverage(syms, fund, listed),
    }
    problems = nd.reconstruction_problems(cov_main, mine)
    ae = nd.always_excluded(jb["cycles"], as_of_ms)
    star = nd.coverage(need - ae, markets, listed)
    removed = need & ae & listed
    return {
        "window": [since, until],
        "done_cycles": len(done),
        "always_excluded": len(ae),
        "reconstructed": mine,
        "problems": problems,
        "need_star": star,
        "removed_from_need": {"n": len(removed), "with_bars": sorted(removed & markets), "without_bars": len(removed - markets)},
        "not_removed_lacking": star["lacking"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=v.DB, help="collector DB or a copy; opened read-only")
    ap.add_argument("--as-of-ms", type=int, default=v.FINAL_AS_OF_MS)
    ap.add_argument("--cache-dir", default=str(v.CACHE), help="the main run's --cache-dir (files under <cache-dir>/data)")
    ap.add_argument("--a2", default=None, help="main (a.2) JSON (default: <cache-dir>/a2-<as-of>.json)")
    ap.add_argument("--out", default=None, help="JSON result (default: <cache-dir>/need_star-<as-of>.json)")
    ap.add_argument("--legacy-cache-names", action="store_true", help="interim smoke only: cache names before the key fix")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    a2_path = Path(a.a2) if a.a2 else cache / f"a2-{a.as_of_ms}.json"
    out = Path(a.out) if a.out else cache / f"need_star-{a.as_of_ms}.json"
    res = json.loads(a2_path.read_text())
    sha = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:12] for p in OWN_FILES}
    print(
        f"secondary need* ({nd.LABEL}); main a2 {a2_path.name}; as-of {v._t(a.as_of_ms)}; legacy names {a.legacy_cache_names}; sha256 {sha}"
    )
    print(f"main code_sha256 {res.get('code_sha256')}; timing sample {res.get('timing_sample')}; a2 stopped {res.get('a2_stopped')}")
    result: dict[str, Any] = {"label": nd.LABEL, "as_of_ms": a.as_of_ms, "a2_file": str(a2_path), "sha256": sha, "books": {}}
    print(
        "| book | main (a.2) | main (a) | need (main) | need (rebuilt) | always excluded | need* | removed with bars | secondary (a.2) | secondary (a) |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    notes = []
    for S in ("A", "B"):
        a1 = (res.get("a1") or {}).get(S) or {}
        if a1.get("as_of_ms") not in (None, a.as_of_ms):
            raise SystemExit(f"{S}: the main JSON is for as-of {a1.get('as_of_ms')}, not {a.as_of_ms}")
        book = (res.get("a2") or {}).get(S)
        a1_status = {"status": a1.get("status"), "passed": a1.get("passed")} if a1 else None
        mv = (res.get("verdict") or {}).get(S, "◯")
        if book is None:
            sec = nd.secondary(None, a1_status, None, None, mv)
            result["books"][S] = {"secondary": sec}
            print(f"| {S} | not run | {mv} | — | — | — | — | — | {sec['state']} | {sec['verdict']} |")
            continue
        r = book_need(a.db, S, a.as_of_ms, cache / "data", book, legacy=a.legacy_cache_names)
        sec = nd.secondary(book["summary"], a1_status, book["coverage"]["need"], r["need_star"], mv, r["problems"])
        r["secondary"] = sec
        r["main"] = {
            "state": nd.primary_state(book["summary"]),
            "verdict": mv,
            "incomplete_reasons": book["summary"].get("incomplete_reasons"),
        }
        result["books"][S] = r
        cn, rn, st = book["coverage"]["need"], r["reconstructed"]["need"], r["need_star"]
        print(f"| {S} | {r['main']['state']} | {mv} | {cn['have']}/{cn['n']} | {rn['have']}/{rn['n']} | {r['always_excluded']} | "
              f"{st['have']}/{st['n']} ({st['rate']}) | {len(r['removed_from_need']['with_bars'])} | {sec['state']} | {sec['verdict']} |")  # fmt: skip
        notes.append(f"{S}: main incomplete reasons {r['main']['incomplete_reasons']}; reconstruction problems {r['problems'] or 'none'}; "
                     f"removed from need {r['removed_from_need']['n']} (with bars {r['removed_from_need']['with_bars']}, without "
                     f"{r['removed_from_need']['without_bars']}); need* lacking {st['lacking'][:20]}; secondary: {sec['why']}")  # fmt: skip
    for n in notes:
        print(n)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(f"full result: {out} (rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB)")


if __name__ == "__main__":
    main()
