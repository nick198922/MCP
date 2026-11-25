"""Secondary ``need*`` coverage of check (a.2) of the forward paper carry (amendment after the interim run).

Card: docs/ideas/IDEA-20261017-forward-paper-carry.md, subsection "Поправка після проміжного прогону ... вторинне
покриття `need`" (written AFTER the interim run, before the final one; EXPLORATORY). The main result stays the
registered one; this module never changes it and never turns a '✗' into anything better.

``need*`` = the runner's ``need`` minus the symbols the engine recorded in ``excluded`` (any reason) in EVERY done
cycle up to ``as_of`` inclusive: ``replay_a2_cycle`` removes a cycle's recorded exclusions from the step's universe,
so data of a symbol excluded in every cycle enters no step. Coverage rule as registered (>= 0.95, symbols not listed
now outside the denominator). The secondary (a.2) differs from the main one only when the main one is incomplete for
the ``need`` coverage ALONE and ``need*`` coverage is >= 0.95; then it is judged by the registered rules unchanged.

Pure functions: no I/O; time (``as_of_ms``) is a parameter. The ``need`` rule and the coverage are copies of the
frozen runner code (``scripts/verify_carry_paper_replay.py``: ``_a2_book``, ``_coverage``); the script checks that
the copy reproduces the main output's numbers and otherwise reports the secondary result as the main one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tradedesk.analytics import carry_replay as cr
from tradedesk.analytics.carry import _Leg

MIN_COVERAGE = 0.95  # = the runner's frozen MIN_COVERAGE (a test pins the equality)
LABEL = "exploratory: amendment after the interim run (need*), not the main result"


def done_cycles(cycles: Iterable[Mapping[str, Any]], as_of_ms: int) -> list[Mapping[str, Any]]:
    """Cycles with status 'done' and ``cycle_ts <= as_of_ms``, in time order."""
    return sorted((c for c in cycles if c.get("status") == "done" and int(c["cycle_ts"]) <= as_of_ms), key=lambda c: int(c["cycle_ts"]))


def always_excluded(cycles: Iterable[Mapping[str, Any]], as_of_ms: int) -> set[str]:
    """Symbols in the recorded ``excluded`` list (any reason) of EVERY done cycle up to ``as_of_ms`` inclusive.

    No done cycle -> empty set (nothing removed). A cycle whose list the engine truncated counts with the symbols it
    recorded: a symbol cut off there is not removed (the conservative side)."""
    sets = [cr.excluded_symbols(c.get("payload") or {}) for c in done_cycles(cycles, as_of_ms)]
    return set.intersection(*sets) if sets else set()


def need_symbols(
    strategy: str,
    fund: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    times: Iterable[int],
    must: Iterable[str],
    *,
    entry_apr: float,
    lookback_ms: int,
    bar_ms: int,
) -> set[str]:
    """The runner's ``need`` (copy of the frozen rule): held / recorded candidate symbols, plus every symbol with
    downloaded funding on all legs whose downloaded APR (A) or |spread| (B) reaches ``entry_apr`` at a decision time."""
    venues = cr._venues(strategy)
    times = list(times)
    need = set(must)
    for s, legs in fund.items():
        lg = [_Leg({"funding": legs[v], "bars": []}, bar_ms) for v in venues]
        for t in times:
            a = [x.apr(t, lookback_ms) for x in lg]
            if None in a:
                continue
            if (strategy == "A" and a[0] >= entry_apr) or (strategy == "B" and abs(a[1] - a[0]) >= entry_apr):
                need.add(s)
                break
    return need


def coverage(group: Iterable[str], got: Iterable[str], listed: set[str] | None) -> dict[str, Any]:
    """Registered coverage (copy of the runner's ``_coverage``): symbols not listed now are outside the denominator;
    ``listed`` None (market list failed) -> nothing counts as delisted."""
    base = {s for s in group if listed is None or s in listed}
    have = base & set(got)
    return {"n": len(base), "have": len(have), "rate": len(have) / len(base) if base else None, "lacking": sorted(base - have)}


def listed_from_delisted(universe: Iterable[str], delisted_now: Iterable[str]) -> set[str]:
    """The listed set as the main run saw it, for the symbols that matter here: ``universe`` minus the run's printed
    ``delisted_now`` (a subset of ``need | must``, so exact for any subset of ``need``)."""
    return set(universe) - set(delisted_now)


def need_reason(cov_need: Mapping[str, Any]) -> str:
    """The runner's incomplete reason for the ``need`` coverage (same text, built from the main output's numbers)."""
    return f"funding + bars for {cov_need['have']}/{cov_need['n']} of the symbols that can matter"


def reconstruction_problems(main_cov: Mapping[str, Any], mine_by_name: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Differences between the main output's ``coverage`` and the reconstruction (e.g. ``need``, ``must``,
    ``universe_funding``): any difference -> the secondary result is reported as the main one."""
    out = []
    for name, mine in mine_by_name.items():
        theirs = main_cov.get(name) or {}
        for k in ("n", "have", "lacking"):
            if theirs.get(k) != mine.get(k):
                a, b = theirs.get(k), mine.get(k)
                if k == "lacking":
                    a, b = sorted(set(a or []) - set(b or [])), sorted(set(b or []) - set(a or []))
                out.append(f"{name}.{k}: main {a} vs reconstructed {b}")
    return out


def primary_state(summary: Mapping[str, Any] | None) -> str:
    """(a.2) state as the runner prints it."""
    if summary is None:
        return "not run"
    return "incomplete" if summary.get("incomplete") else "pass" if summary.get("passed") else "FAIL"


def secondary(
    summary: Mapping[str, Any] | None,
    a1_status: Mapping[str, Any] | None,
    main_cov_need: Mapping[str, Any] | None,
    cov_need_star: Mapping[str, Any] | None,
    main_verdict: str,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    """Secondary (a.2) state and (a) verdict of one book.

    Applied only when (1) the reconstruction reproduces the main output (``problems`` empty), (2) the main verdict is
    not '✗', (3) the main (a.2) is incomplete with EXACTLY one reason, the ``need`` coverage one, and (4) ``need*``
    coverage is >= 0.95. Then the incomplete flag is dropped and (a.2) / (a) are judged by the registered rules
    unchanged (``summarize_a2``'s pass rule, ``carry_replay.verdict``): that may give 'FAIL' / '✗' as well as
    'pass'. Otherwise the secondary result is the main one. Never better than '✗' when the main one is '✗'."""
    same = {"state": primary_state(summary), "verdict": main_verdict, "applied": False, "label": LABEL}
    if problems:
        return {**same, "why": "the reconstruction does not reproduce the main output: " + "; ".join(problems[:5])}
    if main_verdict == "✗":
        return {**same, "why": "the main verdict is ✗: the amendment never improves a ✗"}
    if summary is None or a1_status is None or main_cov_need is None:
        return {**same, "why": "the main (a.1) / (a.2) result is missing"}
    if not summary.get("incomplete"):
        return {**same, "why": "the main (a.2) is not incomplete"}
    reasons = list(summary.get("incomplete_reasons") or [])
    if reasons != [need_reason(main_cov_need)]:
        return {**same, "why": f"the need coverage is not the only reason of 'incomplete': {reasons}"}
    rate = None if cov_need_star is None else cov_need_star.get("rate")
    if rate is None or rate < MIN_COVERAGE:
        return {**same, "why": f"need* coverage {rate} < {MIN_COVERAGE} (or empty)"}
    s2 = {k: v for k, v in summary.items() if k not in ("incomplete", "incomplete_reasons")}
    by_class = s2.get("divergences_by_class") or {}
    s2["passed"] = s2.get("rate") is not None and s2["rate"] >= 0.90 and by_class.get("unexplained", 0) == 0  # summarize_a2
    v2 = cr.verdict(dict(a1_status), s2)
    if main_verdict == "✗":  # unreachable after the check above; kept as the hard rule of the amendment
        v2 = "✗"
    return {
        "state": "pass" if s2["passed"] else "FAIL",
        "verdict": v2,
        "applied": True,
        "label": LABEL,
        "why": "need coverage was the only reason; need* >= 0.95",
    }
