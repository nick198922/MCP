"""Secondary need* coverage of check (a.2) (analytics/carry_replay_need.py, scripts/verify_carry_paper_replay_need.py).

Amendment after the interim run (card IDEA-20261017-forward-paper-carry.md): exploratory, never the main result."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tradedesk.analytics import carry_replay as cr
from tradedesk.analytics import carry_replay_need as nd
from tradedesk.core.envelope import Evidence, Fetched

H8, DAY = 8 * 3_600_000, 86_400_000
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _scripts() -> tuple[Any, Any]:
    sys.path.insert(0, str(SCRIPTS))
    import verify_carry_paper_replay as v
    import verify_carry_paper_replay_need as s

    return v, s


def _cyc(ts: int, excluded: list[str], status: str = "done") -> dict[str, Any]:
    return {"cycle_ts": ts, "status": status, "payload": {"excluded": [{"symbol": x, "reason": "no_spot"} for x in excluded]}}


# ---------- always_excluded ----------


def test_always_excluded_is_the_intersection_over_done_cycles():
    cycles = [_cyc(1 * H8, ["X", "Y", "Z"]), _cyc(2 * H8, ["X", "Y"]), _cyc(3 * H8, ["X", "Y", "W"])]
    assert nd.always_excluded(cycles, 3 * H8) == {"X", "Y"}
    assert nd.always_excluded(cycles, 1 * H8) == {"X", "Y", "Z"}  # as-of inclusive
    assert nd.always_excluded(cycles, 0) == set()  # no done cycle: nothing is removed


def test_always_excluded_ignores_failed_cycles_and_counts_any_reason():
    cycles = [_cyc(H8, ["X", "Y"]), _cyc(2 * H8, [], status="failed"), _cyc(3 * H8, ["X"])]
    cycles[2]["payload"]["excluded"][0]["reason"] = "no_second_venue"
    assert nd.always_excluded(cycles, 3 * H8) == {"X"}


def test_always_excluded_before_t_does_not_depend_on_cycles_after_t():
    base = [_cyc(H8, ["X", "Y"]), _cyc(2 * H8, ["X", "Y"])]
    later = [*base, _cyc(3 * H8, []), _cyc(4 * H8, ["Q"])]
    assert nd.always_excluded(base, 2 * H8) == nd.always_excluded(later, 2 * H8) == {"X", "Y"}
    assert nd.always_excluded(later, 3 * H8) == set()


def test_truncated_excluded_list_removes_only_what_it_recorded():
    """A symbol the engine cut off a truncated list is not removed (the conservative side)."""
    c2 = _cyc(2 * H8, ["X"])
    c2["payload"]["excluded_counts"] = {"no_spot": 2}  # two exclusions, one written
    assert cr.excluded_truncated(c2["payload"])
    assert nd.always_excluded([_cyc(H8, ["X", "Y"]), c2], 2 * H8) == {"X"}


# ---------- coverage / need rule: copies of the frozen runner ----------


def test_min_coverage_and_coverage_equal_the_runner():
    v, _ = _scripts()
    assert nd.MIN_COVERAGE == v.MIN_COVERAGE == 0.95
    for group, got, listed in [
        ({"A", "B", "C", "D"}, {"A", "C", "E"}, {"A", "B", "C"}),
        ({"A", "B"}, {"A"}, None),
        (set(), {"A"}, None),
        ({"A"}, set(), set()),
    ]:
        assert nd.coverage(group, got, listed) == v._coverage(group, got, listed)


def test_need_reason_is_the_runner_text():
    src = (SCRIPTS / "verify_carry_paper_replay.py").read_text()
    assert "funding + bars for {cov['need']['have']}/{cov['need']['n']} of the symbols that can matter" in src
    assert nd.need_reason({"have": 6, "n": 92, "rate": 6 / 92}) == "funding + bars for 6/92 of the symbols that can matter"


def _rows(rate: float, start: int, end: int) -> list[dict[str, Any]]:
    return [{"ts_ms": t, "rate": rate} for t in range(start, end + 1, H8)]


def test_need_symbols_a_threshold_on_downloaded_apr():
    """APR = sum of rates over 7 d x 365/7: 21 x 2e-4 x 365/7 = 0.219 >= 0.20; 1e-4 -> 0.1095 < 0.20."""
    t0, t = 0, 30 * DAY
    fund = {
        "X": {cr.PERP_VENUE: _rows(2e-4, t0, t)},
        "Y": {cr.PERP_VENUE: _rows(1e-4, t0, t)},
        "Z": {cr.PERP_VENUE: _rows(2e-4, t - 3 * DAY, t)},  # history shorter than the lookback: no APR
    }
    kw = {"entry_apr": 0.20, "lookback_ms": 7 * DAY, "bar_ms": 4 * 3_600_000}
    assert nd.need_symbols("A", fund, [t], {"M"}, **kw) == {"M", "X"}
    assert nd.need_symbols("A", fund, [], set(), **kw) == set()


def test_need_symbols_b_uses_the_absolute_spread():
    t = 30 * DAY
    two = lambda a, b: {cr.PERP_VENUE: _rows(a, 0, t), cr.BYBIT: _rows(b, 0, t)}  # noqa: E731
    fund = {"X": two(3e-4, 1e-4), "Y": two(1e-4, 3e-4), "W": two(1e-4, 1e-4)}
    kw = {"entry_apr": 0.20, "lookback_ms": 7 * DAY, "bar_ms": 4 * 3_600_000}
    assert nd.need_symbols("B", fund, [t], set(), **kw) == {"X", "Y"}


def test_reconstruction_problems():
    main = {"need": {"n": 3, "have": 1, "lacking": ["B", "C"]}, "must": {"n": 1, "have": 1, "lacking": []}}
    ok = {"need": nd.coverage({"A", "B", "C"}, {"A"}, None), "must": nd.coverage({"A"}, {"A"}, None)}
    assert nd.reconstruction_problems(main, ok) == []
    bad = {"need": nd.coverage({"A", "B", "D"}, {"A"}, None)}
    assert nd.reconstruction_problems(main, bad) == ["need.lacking: main ['C'] vs reconstructed ['D']"]


# ---------- secondary result ----------

A1_PASS = {"status": "pass", "passed": True}


def _summary(*, rate=1.0, oc=1.0, by_class=None, reasons=("funding + bars for 6/92 of the symbols that can matter",)):
    s = {
        "decisions": 314,
        "matched": 314,
        "rate": rate,
        "open_close": 11,
        "open_close_rate": oc,
        "divergences_by_class": by_class or {},
        "passed": False,
    }
    if reasons:
        s["incomplete"], s["incomplete_reasons"] = True, list(reasons)
    return s


NEED = {"n": 92, "have": 6, "rate": 6 / 92}


def _star(have: int, n: int) -> dict[str, Any]:
    return {"n": n, "have": have, "rate": have / n if n else None, "lacking": []}


def test_secondary_applies_only_for_the_need_reason_alone():
    s = _summary()
    main_v = cr.verdict(A1_PASS, s)
    assert main_v == "◯"
    got = nd.secondary(s, A1_PASS, NEED, _star(6, 6), main_v)
    assert got["applied"] and got["state"] == "pass" and got["verdict"] == "✓" and got["label"] == nd.LABEL
    assert s["incomplete"] and s["passed"] is False  # the main summary is not changed


def test_secondary_boundary_and_below_threshold():
    s = _summary()
    assert nd.secondary(s, A1_PASS, NEED, _star(19, 20), "◯")["applied"]  # 0.95 exactly passes (>=)
    low = nd.secondary(s, A1_PASS, NEED, _star(18, 20), "◯")
    assert not low["applied"] and (low["state"], low["verdict"]) == ("incomplete", "◯")
    empty = nd.secondary(s, A1_PASS, NEED, _star(0, 0), "◯")
    assert not empty["applied"] and empty["verdict"] == "◯"


@pytest.mark.parametrize(
    "reasons",
    [
        ("funding + bars for 6/92 of the symbols that can matter", "funding for 400/491 of the universe"),
        ("1 divergences on symbols our download lacks", "funding + bars for 6/92 of the symbols that can matter"),
        ("funding + bars for 7/92 of the symbols that can matter",),  # text does not match the main coverage
        ("held / candidate symbols not downloaded: ['X']",),
    ],
)
def test_secondary_is_the_main_result_when_another_reason_exists(reasons):
    s = _summary(reasons=reasons)
    got = nd.secondary(s, A1_PASS, NEED, _star(6, 6), "◯")
    assert not got["applied"] and (got["state"], got["verdict"]) == ("incomplete", "◯")


def test_secondary_never_improves_a_cross():
    s = _summary(by_class={"unexplained": 1})
    assert cr.verdict(A1_PASS, s) == "✗"
    got = nd.secondary(s, A1_PASS, NEED, _star(6, 6), "✗")
    assert not got["applied"] and got["verdict"] == "✗"
    a1_fail = {"status": "fail", "passed": False}
    got = nd.secondary(_summary(), a1_fail, NEED, _star(6, 6), cr.verdict(a1_fail, _summary()))
    assert not got["applied"] and got["verdict"] == "✗"


def test_secondary_is_judged_by_the_registered_rules_once_applied():
    """Applied, but under 90 % matched: the registered mapping gives FAIL / ✗, not a pass."""
    s = _summary(rate=0.85, by_class={"funding_data": 47})
    got = nd.secondary(s, A1_PASS, NEED, _star(6, 6), cr.verdict(A1_PASS, s))
    assert got["applied"] and (got["state"], got["verdict"]) == ("FAIL", "✗")
    s = _summary(oc=0.5)
    assert nd.secondary(s, A1_PASS, NEED, _star(6, 6), "◯")["verdict"] == "≈"


def test_secondary_with_reconstruction_problems_or_missing_main():
    got = nd.secondary(_summary(), A1_PASS, NEED, _star(6, 6), "◯", ["need.n: main 92 vs reconstructed 91"])
    assert not got["applied"] and got["verdict"] == "◯"
    assert nd.secondary(None, A1_PASS, None, None, "◯")["state"] == "not run"
    passed = {k: v for k, v in _summary(reasons=()).items()} | {"passed": True}
    got = nd.secondary(passed, A1_PASS, NEED, _star(6, 6), "✓")
    assert not got["applied"] and (got["state"], got["verdict"]) == ("pass", "✓")


# ---------- the script reads the files the runner writes ----------

_SYMS = ("币安人生/USDT:USDT", "牛来/USDT:USDT")


class _Fake:
    async def funding_rate_history_range(self, venue: str, sym: str, since: int, until: int, **_: Any) -> Fetched:
        rows = [{"timestamp": t, "fundingRate": 1e-4} for t in range(since, until + 1, H8)]
        return Fetched(
            {"rows": rows, "complete": True, "truncated": False, "dropped_rows": 0}, Evidence(provider="f", tool="t", args={"pages": 1})
        )

    async def ohlcv(self, venue: str, sym: str, tf: str, limit: int, since: int, until: int) -> Fetched:
        return Fetched(
            [[t, 1.0, 1.0, 1.0, 1.0, 1.0] for t in range(since, until + 1, 4 * 3_600_000)][:limit],
            Evidence(provider="f", tool="t", args={}),
        )


@pytest.mark.parametrize("kind", ["funding", "bars4h"])
def test_cache_path_is_the_file_the_runner_writes(tmp_path, kind):
    v, s = _scripts()

    async def sleep(_: float) -> None:
        return None

    since = 2_000 * H8
    until = since + 3 * DAY
    pace = v.Pace(None, sleep=sleep, now_ms=lambda: until + H8 + 3_600_000, mono=lambda: 0.0)
    rt = SimpleNamespace(exchanges=_Fake())
    fetch = v.fetch_funding if kind == "funding" else v.fetch_bars
    for sym in _SYMS:
        before = set(tmp_path.iterdir())
        assert asyncio.run(fetch(rt, pace, tmp_path, cr.PERP_VENUE, sym, since, until))
        (new,) = set(tmp_path.iterdir()) - before
        assert new == s.cache_path(tmp_path, cr.PERP_VENUE, sym, kind, since, until)
        assert s.cache_path(tmp_path, cr.PERP_VENUE, sym, kind, since, until, legacy=True) == v._cache_file(
            tmp_path, cr.PERP_VENUE, sym, kind, since, until
        )
