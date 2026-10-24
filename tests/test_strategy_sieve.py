from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, resample
from tradedesk.analytics.stop_management import risk_unit, simulate_stop_rule
from tradedesk.analytics.strategy_sieve import (
    FAIL,
    FAIL_OOS,
    FUND_8H,
    HIDDEN_MS,
    IS_END_MS,
    N_PLAN,
    PASS,
    START_MS,
    UNDERPOWERED,
    Signal,
    Summary,
    causality_violations,
    holm_with_registry,
    mde_z,
    n_effective,
    p_one_sided,
    p_threshold,
    period_of,
    placebo_pool,
    revealed_before,
    simulate_signals,
    stamp_violations,
    stop_rule,
    summarize,
    verdict,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path.home() / ".cache/tradedesk-agents"
S1_CACHE = CACHE / "course_s14_ema"
DATA_DIR = CACHE / "funding_crowding"
STUDY_TRADES = CACHE / "demo_stop_management" / "trades"
T0 = START_MS + 10 * DAY_MS  # inside IS, aligned to 00:00 UTC
SPEC_DIR = ROOT / "src" / "tradedesk" / "analytics" / "sieve_specs"


def h1_bars(rows: list[tuple[float, float, float, float]], t0: int = T0) -> Bars:
    a = np.array(rows, dtype=float)
    t = t0 + np.arange(len(rows), dtype=np.int64) * HOUR_MS
    return Bars(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], HOUR_MS)


def random_walk(n: int, seed: int, t0: int = T0, vol: float = 0.006, drift: float = 0.0) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    t = t0 + np.arange(n, dtype=np.int64) * HOUR_MS
    return Bars(t, o, h, lo, c, HOUR_MS)


def summ(mean: float, lo: float, hi: float, mde: float = 0.04, n: int = 1000) -> Summary:
    se = (hi - lo) / 3.92
    return Summary(n, n // 2, mean, 1.0, lo, hi, se, p_one_sided(mean, se), mde)


GOOD_D = summ(0.06, 0.01, 0.11)  # a signal - placebo difference that passes


def every_k_bars(b: Bars, k: int, entry_mult: float = 1.0) -> list[Signal]:
    """Long every k bars, stop 2 % and target 3 % from the (possibly wrong) entry, hold 5 days."""
    out = []
    for i in range(20, len(b), k):
        e = float(b.c[i]) * entry_mult
        out.append(Signal(b.close_time(i), "long", e, e * 0.98, e * 1.03, 5 * DAY_MS))
    return out


# ---------------------------------------------------------------- signals and trades


def test_signal_validity_and_long_space():
    assert Signal(T0, "long", 100, 98, 104, DAY_MS).valid()
    assert Signal(T0, "short", 100, 102, 96, DAY_MS).valid()
    assert Signal(T0, "long", 100, 98, None, DAY_MS).valid()
    assert not Signal(T0, "long", 100, 101, 104, DAY_MS).valid()  # stop above a long entry
    assert not Signal(T0, "short", 100, 102, 103, DAY_MS).valid()  # target above a short entry
    assert not Signal(T0, "long", 100, 98, 104, 0).valid()
    assert not Signal(T0, "flat", 100, 98, 104, DAY_MS).valid()
    assert Signal(T0, "short", 100, 102, 96, DAY_MS).long_space() == (-100, -102, -96)
    assert Signal(T0, "long", 100, 98, None, DAY_MS).long_space() == (100, 98, math.inf)


def test_periods():
    assert period_of(IS_END_MS - 1) == "is"
    assert period_of(IS_END_MS) == "oos"
    assert period_of(HIDDEN_MS - 1) == "oos"
    assert period_of(HIDDEN_MS) == "hidden"


def test_trades_use_stop_management_costs_and_one_position_per_symbol():
    # the first bar closes at T0 at 100: the entry of signal a
    path = h1_bars(
        [(99.5, 100.2, 99.4, 100), (100, 101, 99.5, 100.5), (100.5, 102.5, 100, 102), (102, 106, 101.5, 105), (105, 105.5, 104, 104.5)],
        t0=T0 - HOUR_MS,
    )
    a = Signal(T0, "long", 100, 98, 104, 10 * HOUR_MS)
    b = Signal(T0 + HOUR_MS, "long", 100.5, 99, 103, 10 * HOUR_MS)  # while a is open: skipped
    c = Signal(T0 + 3 * HOUR_MS, "long", 105, 104.1, 110, 10 * HOUR_MS)  # after a's exit
    tr = simulate_signals(path, [(HOUR_MS, c), (HOUR_MS, b), (HOUR_MS, a)], "B")
    assert [t.t_ms for t in tr] == [a.t_ms, c.t_ms]
    ref = simulate_stop_rule(path, T0, 100, 98, 104, 10 * HOUR_MS, stop_rule("B"))
    assert tr[0].r_net == pytest.approx(ref.r_net) and tr[0].exit_kind == "target"
    ref_a = simulate_stop_rule(path, T0, 100, 98, 104, 10 * HOUR_MS, stop_rule("strategy"))
    assert tr[0].r_alt == pytest.approx(ref_a.r_net)
    assert math.isnan(tr[0].placebo_r) and tr[0].placebo_n == 0  # no rng, no placebo
    assert tr[0].period == "is" and tr[0].day == T0 // DAY_MS
    assert tr[0].fund_r == 0.0 and tr[0].fund_src == "none" and tr[0].entry_gap == 0.0


def test_short_is_simulated_on_negated_prices():
    path = h1_bars([(99.8, 100.1, 99.7, 100), (100, 100.5, 99, 99.2), (99.2, 99.5, 95, 95.5)], t0=T0 - HOUR_MS)
    tr = simulate_signals(path, [(HOUR_MS, Signal(T0, "short", 100, 102, 96, 10 * HOUR_MS))], "strategy")
    assert len(tr) == 1 and tr[0].exit_kind == "target" and tr[0].r_net > 1.5
    assert tr[0].stop_frac == pytest.approx(0.02) and tr[0].target_frac == pytest.approx(0.04)


def test_allowed_filters_entries_and_a_signal_off_the_hour_grid_is_skipped():
    path = h1_bars([(100, 101, 99.5, 100.5)] * 5, t0=T0 - HOUR_MS)
    sig = Signal(T0, "long", 100.5, 98, 104, 3 * HOUR_MS)
    assert simulate_signals(path, [(HOUR_MS, sig)], "B", allowed=lambda t: False) == []
    assert len(simulate_signals(path, [(HOUR_MS, sig)], "B")) == 1
    off = Signal(T0 + 60_000, "long", 100.5, 98, 104, 3 * HOUR_MS)  # not the close of a 1h bar
    assert simulate_signals(path, [(HOUR_MS, off)], "B") == []


# ---------------------------------------------------------------- B1: the entry is the bar close


def test_cheap_entry_spec_is_refused_and_earns_nothing():
    """Critic's probe: a long every 10 bars of 4h entered 1 % below the close (with the stop and target hung from that
    price) gave +0.357R on a driftless walk. The runner refuses it; the simulation enters at the close anyway."""
    h1 = random_walk(24 * 900, seed=21)
    b4 = resample(h1, 4)
    cheap = every_k_bars(b4, 10, entry_mult=0.99)
    assert len(stamp_violations(b4, cheap)) == len(cheap)
    assert causality_violations(lambda b: every_k_bars(b, 10, 0.99), b4, {})
    honest = every_k_bars(b4, 10)
    assert stamp_violations(b4, honest) == []
    tc = simulate_signals(h1, [(b4.bar_ms, s) for s in cheap], "strategy")
    th = simulate_signals(h1, [(b4.bar_ms, s) for s in honest], "strategy")
    assert len(tc) == len(th) > 300
    assert all(t.entry_gap == pytest.approx(0.01, rel=1e-6) for t in tc)
    # the same trades as entering at the close with the same stop / target fractions
    assert [t.r_net for t in tc] == pytest.approx([t.r_net for t in th], abs=1e-9)
    r = np.array([t.r_net for t in tc])
    cost = np.mean([t.cost_r for t in tc])
    assert abs(r.mean() + cost) < 3 * r.std() / math.sqrt(r.size)  # about minus costs, not +0.36R


# ---------------------------------------------------------------- causality


def backdated(b: Bars, jump: float = 0.02) -> list[Signal]:
    """Critic's probe: bar i rose by ``jump`` -> a long stamped with the close (time and price) of bar i - 1."""
    out = []
    for i in range(1, len(b)):
        if b.c[i] / b.c[i - 1] - 1 > jump:
            e = float(b.c[i - 1])
            out.append(Signal(b.close_time(i - 1), "long", e, e * 0.97, e * 1.03, DAY_MS))
    return out


def test_backdated_spec_is_refused_and_would_have_earned():
    h1 = random_walk(24 * 900, seed=13)
    b4 = resample(h1, 4)
    sigs = backdated(b4)
    assert 30 < len(sigs) < 0.2 * len(b4)
    assert stamp_violations(b4, sigs) == []  # stamps and prices look right ...
    bad = causality_violations(backdated, b4, {})
    assert len(bad) == len({s.t_ms for s in sigs}) or len(bad) >= 200  # ... but every sampled signal fails
    tr = simulate_signals(h1, [(b4.bar_ms, s) for s in sigs], "strategy")
    assert np.mean([t.r_net for t in tr]) > 0.3  # what the hole was worth


@pytest.mark.parametrize("name", ["s3_range_sweep", "donchian_breakout"])
def test_specs_are_causal(name):
    spec = sieve_specs.load(name)
    h1 = random_walk(24 * 400, seed=7)
    for hours in spec.TIMEFRAMES:
        b = resample(h1, hours)
        sigs = spec.signals(b, **spec.PARAMS)
        assert sigs, f"{name} {hours}h: no signals on the sample, the test would prove nothing"
        assert all(s.valid() for s in sigs)
        assert stamp_violations(b, sigs) == []
        m = len(b)
        cut = m // 2
        assert causality_violations(spec.signals, b, spec.PARAMS, [m // 3, cut, m - 5], n_sample=60, n_last=10) == []
        # changing the bars after t does not change the signals decided by t
        edge = int(b.t[cut - 1]) + b.bar_ms
        k = 1.0 + np.linspace(0.0, 0.5, m - cut)
        changed = Bars(
            b.t,
            np.concatenate([b.o[:cut], b.o[cut:] * k]),
            np.concatenate([b.h[:cut], b.h[cut:] * k * 1.01]),
            np.concatenate([b.low[:cut], b.low[cut:] * k * 0.99]),
            np.concatenate([b.c[:cut], b.c[cut:] * k]),
            b.bar_ms,
        )
        before = [s for s in sigs if s.t_ms <= edge]
        assert before == [s for s in spec.signals(changed, **spec.PARAMS) if s.t_ms <= edge]


def test_causality_check_flags_a_lookahead_spec():
    def peeking(b: Bars) -> list[Signal]:
        top = float(b.h.max())  # the whole series, future included
        return [Signal(b.close_time(i), "long", float(b.c[i]), float(b.c[i]) * 0.98, None, DAY_MS) for i in range(len(b)) if b.h[i] == top]

    b = random_walk(2000, seed=3)
    assert causality_violations(peeking, b, {}, [500, 1000, 1500])


def test_stamp_check_flags_a_time_off_the_bar_grid():
    b = random_walk(100, seed=1)
    s = Signal(int(b.t[10]) + 30 * 60_000, "long", float(b.c[10]), float(b.c[10]) * 0.98, None, DAY_MS)
    assert "not the close time" in stamp_violations(b, [s])[0]


# ---------------------------------------------------------------- B7: spec allow-list


def test_every_package_spec_passes_the_allow_list_and_names_itself():
    names = sorted(p.stem for p in SPEC_DIR.glob("*.py") if p.stem != "__init__")
    assert names, "no specs found"
    for n in names:
        assert sieve_specs.check_source((SPEC_DIR / f"{n}.py").read_text()) == [], n
        assert sieve_specs.load(n).NAME == n


@pytest.mark.parametrize(
    "src",
    [
        "import os",
        "import subprocess as s",
        "from os import path",
        "from . import s3_range_sweep",
        "import tradedesk.analytics.course_s14_ema",  # binds the top package
        "from tradedesk.core import http",
        "x = open('/etc/passwd')",
        "x = eval('1')",
        "exec('x = 1')",
        "m = __import__('os')",
        "import numpy as np\nf = getattr(np, 'load')",
        "x = ().__class__",
        "import numpy as np\nx = np.load('f.npy')",
        "import numpy as np\nnp.save('f.npy', [1])",
        "from numpy import load",
        "from tradedesk.analytics.sieve_specs import util",
        "from tradedesk.analytics.strategy_sieve import zlib",  # a stdlib module re-exported by our module
        "g = (x for x in [])\nf = g.gi_frame",
        "x = {}['__builtins__']",
        "x = globals()",
        "def f():\n    global y\n    y = 1",
    ],
)
def test_allow_list_refuses_unsafe_spec_code(src):
    assert sieve_specs.check_source(src), src


def test_allow_list_accepts_plain_spec_code():
    src = (
        "from __future__ import annotations\nimport math\nimport numpy as np\n"
        "from tradedesk.analytics.course_s14_ema import Bars, atr\nimport tradedesk.analytics.course_basics as cb\n"
        "def f(b):\n    return float(np.mean(b.c)) + math.sqrt(2)\n"
    )
    assert sieve_specs.check_source(src) == []


def test_spec_loader_only_imports_package_modules():
    for bad in ("../scripts/sieve", "os.path", "S3", ""):
        with pytest.raises(ValueError):
            sieve_specs.load(bad)
    with pytest.raises(ImportError):
        sieve_specs.load("no_such_spec")


def test_spec_hash_ignores_name_and_docs_and_reveal_follows_it():
    src = (SPEC_DIR / "donchian_breakout.py").read_text()
    copy = src.replace('NAME = "donchian_breakout"', 'NAME = "turtle_copy"').replace("classic", "famous")
    assert copy != src and sieve_specs.source_hash(copy) == sieve_specs.source_hash(src)
    assert sieve_specs.source_hash(src.replace('"n": 20', '"n": 21')) != sieve_specs.source_hash(src)
    assert sieve_specs.load("donchian_breakout").SIEVE_HASH == sieve_specs.source_hash(src)
    rows = [{"strategy": "a", "revealed": "0"}, {"strategy": "b", "revealed": "1", "run_id": "r2", "spec_hash": "h1"}]
    assert revealed_before(rows, "a") is None
    assert revealed_before(rows, "b")["run_id"] == "r2"
    assert revealed_before(rows, "renamed_b", "h1")["run_id"] == "r2"  # a renamed copy gets no second reveal
    assert revealed_before(rows, "renamed_b", "") is None


# ---------------------------------------------------------------- placebo


def test_placebo_pool_respects_allowed_days_and_periods():
    h1 = random_walk(24 * 10, seed=1, t0=IS_END_MS - 5 * DAY_MS)
    bad_day = (IS_END_MS - 3 * DAY_MS) // DAY_MS
    pool = placebo_pool(h1, lambda t: t // DAY_MS != bad_day)
    close = h1.t + h1.bar_ms
    assert all(close[k] < IS_END_MS and close[k] // DAY_MS != bad_day for k in pool["is"])
    assert all(IS_END_MS <= close[k] < HIDDEN_MS for k in pool["oos"])
    assert pool["hidden"].size == 0
    assert pool["is"].size + pool["oos"].size == close.size - 24


def test_placebo_on_random_walk_is_about_minus_costs():
    h1 = random_walk(24 * 900, seed=11)
    rng = np.random.default_rng(5)
    sigs = []
    for k in range(48, len(h1) - 200, 24):
        j = k + int(rng.integers(0, 24))
        e = float(h1.c[j])
        side = "long" if rng.random() < 0.5 else "short"
        s = 1 if side == "long" else -1
        sigs.append((HOUR_MS, Signal(int(h1.t[j]) + HOUR_MS, side, e, e * (1 - s * 0.03), e * (1 + s * 0.045), 3 * DAY_MS)))
    tr = simulate_signals(h1, sigs, "B", rng=np.random.default_rng(9))
    assert len(tr) > 300
    pr = np.array([t.placebo_r for t in tr])
    assert np.isfinite(pr).mean() > 0.95 and np.mean([t.placebo_n for t in tr]) > 4.5
    cost = np.mean([t.cost_r for t in tr])
    se = np.nanstd(pr) / math.sqrt(np.isfinite(pr).sum())
    # a random entry on a driftless walk earns nothing before costs
    assert abs(np.nanmean(pr) + cost) < 3 * se
    # random signals do not beat the random placebo either
    d = np.array([t.r_net - t.placebo_r for t in tr if math.isfinite(t.placebo_r)])
    assert abs(d.mean()) < 3 * d.std() / math.sqrt(d.size)


def test_side_beta_on_a_drifting_market_does_not_pass():
    """Donchian-like 'beta': long, no target, on a market that drifts up. The absolute E[R] clears the bar, the
    difference with the same-side placebo does not, so the verdict is not a pass."""
    h1 = random_walk(24 * 900, seed=17, vol=0.002, drift=1e-4)
    sigs = [
        (HOUR_MS, Signal(int(h1.t[j]) + HOUR_MS, "long", float(h1.c[j]), float(h1.c[j]) * 0.97, None, 2 * DAY_MS))
        for j in range(47, len(h1) - 60, 48)
    ]
    tr = simulate_signals(h1, sigs, "strategy", rng=np.random.default_rng(3))
    assert len(tr) > 400
    s_is = summarize([t.r_net for t in tr], [t.day for t in tr], n_boot=2000)
    d_is = summarize([t.r_net - t.placebo_r for t in tr], [t.day for t in tr], n_boot=2000)
    assert s_is.mean >= 0.05 and s_is.lo > 0  # the absolute rule alone would pass it
    assert d_is.lo <= 0 < d_is.hi
    good_oos = summ(0.1, 0.02, 0.18)
    assert verdict(s_is, good_oos, d_is, GOOD_D, 0.0) != PASS
    assert verdict(s_is, good_oos, GOOD_D, GOOD_D, 0.0) == PASS  # it is the placebo condition that stops it


# ---------------------------------------------------------------- B6: funding


def flat_path(hours: int) -> Bars:
    return h1_bars([(100, 100.1, 99.9, 100)] * (hours + 1), t0=T0 - HOUR_MS)


def test_funding_is_charged_over_the_hold():
    path = flat_path(80)
    hold = 72 * HOUR_MS
    ft = T0 + np.arange(13, dtype=np.int64) * 8 * HOUR_MS  # settlements at T0, T0 + 8h, ... T0 + 96h
    fr = np.full(13, 0.0001)
    long_ = Signal(T0, "long", 100, 98, None, hold)
    base = simulate_signals(path, [(HOUR_MS, long_)], "strategy")[0]
    t = simulate_signals(path, [(HOUR_MS, long_)], "strategy", funding=(ft, fr))[0]
    assert t.exit_kind == "time" and t.exit_ms == T0 + hold
    # 8 settlements strictly inside (T0, T0 + 72h): T0 + 8h ... T0 + 64h
    want = 8 * 0.0001 * 100 / risk_unit(100, 98)
    assert t.fund_src == "actual" and t.fund_r == pytest.approx(want)
    assert t.r_net == pytest.approx(base.r_net - want)
    assert t.r_alt == pytest.approx(base.r_alt - want)
    # a short receives the same positive rate
    short = Signal(T0, "short", 100, 102, None, hold)
    ts = simulate_signals(path, [(HOUR_MS, short)], "strategy", funding=(ft, fr))[0]
    assert ts.fund_r == pytest.approx(-8 * 0.0001 * 100 / risk_unit(-100, -102))
    # history starting after the entry: the stress rate against the position
    tf = simulate_signals(path, [(HOUR_MS, long_)], "strategy", funding=(ft + DAY_MS, fr))[0]
    assert tf.fund_src == "flat" and tf.fund_r == pytest.approx(9 * FUND_8H * 100 / risk_unit(100, 98))
    tn = simulate_signals(path, [(HOUR_MS, long_)], "strategy", funding=(np.zeros(0, dtype=np.int64), np.zeros(0)))[0]
    assert tn.fund_src == "flat" and tn.fund_r == pytest.approx(tf.fund_r)


def test_stress_row_is_taker_entry_and_double_costs():
    path = flat_path(80)
    t = simulate_signals(path, [(HOUR_MS, Signal(T0, "long", 100, 98, None, 24 * HOUR_MS))], "strategy")[0]
    from tradedesk.analytics.stop_management import Fees

    f2 = Fees().scaled(2)
    ru = risk_unit(100, 98)
    # time exit at 100: the stressed cost is a taker entry and a taker exit, both doubled
    want = t.r_net + t.cost_r - (f2.taker_exit * 100 + f2.taker_exit * 100) / ru
    assert t.r_stress == pytest.approx(want)


# ---------------------------------------------------------------- statistics, registry, verdict


def test_summarize_and_p():
    rng = np.random.default_rng(2)
    v = rng.normal(0.3, 1.0, 400)
    day = np.repeat(np.arange(200), 2)
    s = summarize(v, day, n_boot=2000)
    assert s.n == 400 and s.dates == 200 and s.lo < s.mean < s.hi
    assert s.mde == pytest.approx(mde_z(N_PLAN) * s.se) and s.p < 0.01
    assert summarize([], []).n == 0 and summarize([], []).mde == math.inf
    one = summarize([0.5, 0.7], [1, 1])
    assert one.n == 2 and math.isnan(one.lo) and one.mde == math.inf
    assert p_one_sided(0.0, 0.1) == pytest.approx(0.5)
    assert p_one_sided(0.196, 0.1) == pytest.approx(0.025, abs=1e-3)
    assert mde_z(100) == pytest.approx(3.291 + 0.8416, abs=2e-3)


def test_holm_counts_at_least_the_planned_hundred_tests():
    p = 0.003
    # the first run faces N = 100: p = 0.003 is not enough (it would pass at N = 1)
    assert holm_with_registry(p, []) == pytest.approx(0.3)
    good_is, good_oos = summ(0.17, 0.05, 0.30), summ(0.09, 0.01, 0.17)
    assert verdict(good_is, good_oos, GOOD_D, GOOD_D, holm_with_registry(p, [])) != PASS
    assert verdict(good_is, good_oos, GOOD_D, GOOD_D, holm_with_registry(1e-5, [])) == PASS
    # past 100 rows the bar keeps rising
    assert holm_with_registry(p, [0.5] * 149) == pytest.approx(min(1.0, 150 * p))
    assert holm_with_registry(1e-5, [math.nan] * 199) == pytest.approx(200 * 1e-5)  # missing p count as runs
    assert n_effective(1) == 100 and n_effective(150) == 150
    assert p_threshold(1) == pytest.approx(0.0005) and p_threshold(200) == pytest.approx(0.00025)


def test_pass_needs_effect_ci_placebo_and_oos_sign():
    # a tight IS (MDE at N = 100 below 0.05R), so a miss is "ні", not "замало угод"
    good_is, good_oos = summ(0.08, 0.065, 0.095), summ(0.02, -0.05, 0.09)
    assert verdict(good_is, good_oos, GOOD_D, GOOD_D, 0.01) == PASS
    assert verdict(good_is, summ(-0.01, -0.08, 0.06), GOOD_D, GOOD_D, 0.01) == FAIL_OOS  # OOS sign
    assert verdict(good_is, good_oos, GOOD_D, summ(-0.01, -0.08, 0.06), 0.01) == FAIL_OOS  # OOS vs placebo
    assert verdict(summ(0.04, 0.025, 0.055), good_oos, GOOD_D, GOOD_D, 0.01) == FAIL  # below +0.05R
    assert verdict(good_is, good_oos, summ(0.03, -0.02, 0.08), GOOD_D, 0.01) == FAIL  # IS vs placebo CI
    assert verdict(good_is, good_oos, GOOD_D, GOOD_D, 0.2) == FAIL  # Holm


def test_power_flag_uses_the_corrected_level():
    oos = summ(0.02, -0.05, 0.09)
    # MDE above 0.05R and the CI still reaches +0.05R: too few trades, not failed
    assert verdict(summ(0.01, -0.09, 0.11, n=300), oos, GOOD_D, GOOD_D, 0.4) == UNDERPOWERED
    # the same power, but the CI rules +0.05R out: failed
    assert verdict(summ(-0.10, -0.2, 0.0, n=300), oos, GOOD_D, GOOD_D, 1.0) == FAIL
    # enough power, no effect: failed
    assert verdict(summ(0.0, -0.01, 0.01, n=50000), oos, GOOD_D, GOOD_D, 0.5) == FAIL
    # 2.8 x SE = 0.04 (enough at an uncorrected 5 %), 4.13 x SE = 0.059: at N = 100 it is "too few trades"
    mid = summ(0.03, 0.002, 0.058)
    assert 2.8 * mid.se < 0.05 < mde_z(100) * mid.se
    assert verdict(mid, oos, GOOD_D, GOOD_D, 1.0) == UNDERPOWERED
    # small samples come out with a large MDE
    rng = np.random.default_rng(4)
    small = summarize(rng.normal(0, 1.1, 120), np.arange(120), n_boot=2000)
    assert small.mde > 0.05 and verdict(small, oos, GOOD_D, GOOD_D, 1.0) == UNDERPOWERED


# ---------------------------------------------------------------- runner


def _runner():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import sieve

    return sieve


def test_runner_registry_round_trip_and_hidden_cut(tmp_path):
    sieve = _runner()
    reg = tmp_path / "registry.csv"
    assert sieve.read_registry(reg) == []
    sieve.append_registry(reg, {"run_id": "x", "strategy": "s", "p_is": 0.01, "mean_is": math.nan, "revealed": 0})
    sieve.append_registry(reg, {"run_id": "y", "strategy": "s", "p_is": 0.2, "revealed": 1})
    rows = sieve.read_registry(reg)
    assert [r["run_id"] for r in rows] == ["x", "y"] and rows[0]["mean_is"] == "" and rows[1]["revealed"] == "1"
    assert list(rows[0]) == sieve.COLUMNS
    h1 = random_walk(24 * 20, seed=2, t0=HIDDEN_MS - 10 * DAY_MS)
    tb = sieve.tf_bars(h1, 4, include_hidden=False)
    assert int(tb.t[-1]) + tb.bar_ms <= HIDDEN_MS and len(sieve.tf_bars(h1, 4, include_hidden=True)) > len(tb)
    cut = sieve.cut_hidden(h1)
    assert int(cut.t[-1]) + HOUR_MS == HIDDEN_MS
    assert sieve.parse_sets(["w=20", "width_atr=5.5"], {"w": 30, "width_atr": 6.0}) == {"w": 20, "width_atr": 5.5}
    with pytest.raises(SystemExit):
        sieve.parse_sets(["nope=1"], {"w": 30})
    assert sieve.read_registry(sieve.DEFAULT_REGISTRY) is not None  # the shipped header matches
    assert (reg.read_bytes().count(b"\r")) == 0
    assert len(sieve.code_hash()) == 12


def test_runner_funding_loader(tmp_path):
    sieve = _runner()
    d = tmp_path / "funding" / "AAAUSDT"
    d.mkdir(parents=True)
    (d / "2024-02.json").write_text(json.dumps([[T0 + 8 * HOUR_MS, 8, 0.0002]]))
    (d / "2024-01.json").write_text(json.dumps([[T0, 8, 0.0001]]))
    t, r = sieve.load_funding(tmp_path, "AAAUSDT")
    assert t.tolist() == [T0, T0 + 8 * HOUR_MS] and r.tolist() == [0.0001, 0.0002]
    t, r = sieve.load_funding(tmp_path, "BBBUSDT")
    assert t.size == 0 and r.size == 0


def _fake_rows(include_hidden: bool) -> list[dict]:
    rng = np.random.default_rng(1)
    rows = []
    spans = [("is", START_MS, IS_END_MS, 3000), ("oos", IS_END_MS, HIDDEN_MS, 800)]
    if include_hidden:
        spans.append(("hidden", HIDDEN_MS, HIDDEN_MS + 150 * DAY_MS, 300))
    for period, a, b, n in spans:
        for t in np.sort(rng.integers(a, b, n)):
            t = int(t) // HOUR_MS * HOUR_MS
            r = float(rng.normal(0.3, 1.0))
            rows.append(
                {
                    "sym": "AAAUSDT",
                    "t_ms": t,
                    "day": t // DAY_MS,
                    "period": period,
                    "side": "long",
                    "tf_ms": 4 * HOUR_MS,
                    "r_net": r,
                    "cost_r": 0.02,
                    "fund_r": 0.01,
                    "fund_src": "actual",
                    "exit_kind": "target",
                    "exit_ms": t + HOUR_MS,
                    "r_alt": r,
                    "r_stress": r - 0.05,
                    "stop_frac": 0.03,
                    "target_frac": 0.05,
                    "entry_gap": 0.0,
                    "placebo_r": float(rng.normal(0, 0.5)),
                    "placebo_n": 5,
                }
            )
    return rows


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    """The runner without market data: a passing fake strategy, the registry in tmp (as if canonical)."""
    sieve = _runner()
    calls: list[bool] = []
    state = {"n_bad": 0}

    def fake_run_all(syms, spec_name, params, management, a, include_hidden):
        calls.append(include_hidden)
        rows = _fake_rows(include_hidden)
        return {"rows": rows, "signals": len(rows), "bad": ["AAAUSDT: bad stamp"] * min(3, state["n_bad"]), "n_bad": state["n_bad"]}

    monkeypatch.setattr(sieve, "run_all", fake_run_all)
    monkeypatch.setattr(sieve.s1, "load_1h", lambda c, d, s: random_walk(24 * 300, seed=3))
    monkeypatch.setattr(sieve.s1, "members", lambda c: {"2020-02": ["AAAUSDT"]})
    monkeypatch.setattr(sieve, "DEFAULT_REGISTRY", tmp_path / "registry.csv")
    out = tmp_path / "report.md"
    base = ["sieve.py", "s3_range_sweep", "--n-boot", "500", "--out", str(out)]
    return sieve, calls, state, base, out


def test_runner_reveals_the_hidden_period_once_and_records_it(fake_runner, monkeypatch):
    """First pass opens the hidden period and records it; the next run of the same strategy (another variant) passes
    again but does not simulate the hidden period."""
    sieve, calls, _, base, out = fake_runner
    monkeypatch.setattr(sys, "argv", base)
    assert sieve.main() == 0
    rows = sieve.read_registry(sieve.DEFAULT_REGISTRY)
    assert calls == [False, True]
    assert len(rows) == 1 and rows[0]["outcome"] == PASS and rows[0]["revealed"] == "1" and int(rows[0]["n_hidden"]) == 300
    assert rows[0]["n_effective"] == "100" and rows[0]["spec_hash"] == sieve_specs.load("s3_range_sweep").SIEVE_HASH
    assert "Приховане" in out.read_text()
    monkeypatch.setattr(sys, "argv", [*base, "--set", "w=20"])
    assert sieve.main() == 0
    rows = sieve.read_registry(sieve.DEFAULT_REGISTRY)
    assert calls == [False, True, False]  # no second reveal
    assert len(rows) == 2 and rows[1]["revealed"] == "0" and rows[1]["n_hidden"] == "" and rows[1]["n_registry"] == "2"
    assert "вже відкрито" in out.read_text()


@pytest.mark.parametrize("extra", [["--no-register"], ["--registry", "/tmp/empty.csv"], ["--limit", "11"], ["--limit", "0"]])
def test_runner_has_no_way_around_the_registry(fake_runner, monkeypatch, extra):
    sieve, calls, _, base, _ = fake_runner
    monkeypatch.setattr(sys, "argv", [*base, *extra])
    with pytest.raises(SystemExit):
        sieve.main()
    assert calls == [] and not sieve.DEFAULT_REGISTRY.exists()


def test_limited_run_shows_counts_only_and_is_not_registered(fake_runner, monkeypatch):
    sieve, calls, _, base, out = fake_runner
    monkeypatch.setattr(sys, "argv", [*base, "--limit", "1"])
    assert sieve.main() == 0
    text = out.read_text()
    assert calls == [False] and not sieve.DEFAULT_REGISTRY.exists()
    assert "угод" in text and "E[R] net" not in text and "Вердикт" not in text and PASS not in text


def test_runner_refuses_a_spec_with_bad_stamps(fake_runner, monkeypatch):
    sieve, calls, state, base, out = fake_runner
    state["n_bad"] = 7
    monkeypatch.setattr(sys, "argv", base)
    assert sieve.main() == 2
    assert calls == [False] and not sieve.DEFAULT_REGISTRY.exists()
    assert "ВІДХИЛЕНО" in out.read_text() and "E[R] net" not in out.read_text()


# ---------------------------------------------------------------- known result (local caches only)

needs_cache = pytest.mark.skipif(
    not ((S1_CACHE / "universe.json").exists() and (DATA_DIR / "listing").exists()),
    reason="point-in-time 1h cache of the stop-management study not on this machine",
)


@needs_cache
@pytest.mark.skipif(not (STUDY_TRADES / "BTCUSDT.json").exists(), reason="stop-management trade cache not on this machine")
def test_s3_trades_equal_the_stop_management_study(monkeypatch):
    """Same entry -> the same R under B and A as the study (its S3 rows; a few differ only by the shared position slot).
    The study has no funding, so this comparison runs without it."""
    sieve = _runner()
    monkeypatch.setattr(sieve, "load_funding", lambda dd, s: None)
    out = sieve.run_symbol(
        ("BTCUSDT", "s3_range_sweep", dict(sieve_specs.load("s3_range_sweep").PARAMS), "B", str(S1_CACHE), str(DATA_DIR), True)
    )
    assert out["n_bad"] == 0
    got = {(t["t_ms"], t["side"], t["tf_ms"] // HOUR_MS): t for t in out["trades"]}
    study = [r for r in json.loads((STUDY_TRADES / "BTCUSDT.json").read_text())["rows"] if r["setup"] == "S3"]
    hit = [
        (r, got[(r["entry_ms"], r["side"], 4 if r["tf"] == "4h" else 24)])
        for r in study
        if (r["entry_ms"], r["side"], 4 if r["tf"] == "4h" else 24) in got
    ]
    assert len(study) > 300 and len(hit) >= 0.9 * len(study)
    for r, t in hit:
        assert t["entry_gap"] == 0.0 and t["fund_r"] == 0.0
        assert t["r_net"] == pytest.approx(r["B_r"], abs=1e-9)
        assert t["r_alt"] == pytest.approx(r["A_r"], abs=1e-9)


@needs_cache
def test_s3_known_result_in_sample():
    """The study: E[R] net IS under B about -0.046R (all setups; S3 alone -0.049R) with a day-clustered SE of ~0.016R,
    without funding. The sieve trades S3 alone (no slot shared with S14 / S16), so the number differs within that
    error; funding only makes it worse."""
    sieve = _runner()
    a = argparse.Namespace(s1_cache=S1_CACHE, data_dir=DATA_DIR, workers=4)
    syms = sorted({x for v in sieve.s1.members(S1_CACHE).values() for x in v})
    res = sieve.run_all(syms, "s3_range_sweep", dict(sieve_specs.load("s3_range_sweep").PARAMS), "B", a, include_hidden=False)
    rows = res["rows"]
    assert res["n_bad"] == 0 and not any(r["period"] == "hidden" for r in rows)
    is_rows = [{**r, "r_nofund": r["r_net"] + r["fund_r"]} for r in rows if r["period"] == "is"]
    s = sieve.summ(is_rows, "r_nofund", n_boot=2000)
    assert s.n > 9000
    assert abs(s.mean - (-0.046)) < 0.03, s
    assert sieve.summ(is_rows, n_boot=2000).hi < 0
