"""Tests of analytics/condor_forward.py (item 8j registration of IDEA-20261031-structural-edges-depth)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tradedesk.analytics.condor_forward import (
    H1_QTY,
    REPORT_KEYS,
    condor_cost,
    cost_fridays,
    cost_report,
    cost_verdict,
    foreign_keys,
    format_cost_report,
    friday_entry,
    json_safe,
    leg_cost,
    mde,
    power,
    ratio_ci,
    ratio_of_sums,
    thursday_exit,
    weeks_for_mde,
)
from tradedesk.analytics.option_legs import expiry_surface, leg_row, plan_legs
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import inverse_price

FIX = Path(__file__).resolve().parent / "fixtures" / "deribit_option_legs_2026-11-01.json"

FRI = date(2026, 11, 6)
F = 100_000.0
TAU = 6.96
EXP = int(datetime(2026, 11, 13, 8, tzinfo=UTC).timestamp())
TS = int(datetime(2026, 11, 6, 9, 0, 5, tzinfo=UTC).timestamp())


def row(role: str, K: float, opt: str, *, iv: float = 0.5, half: float = 0.0002, **kw):
    T = TAU / 365.0
    mark = inverse_price(F, K, T, iv, opt == "C")
    r = {
        "currency": "BTC",
        "day": FRI.isoformat(),
        "role": role,
        "ts": TS,
        "expiry_ts": EXP,
        "tau_days": TAU,
        "spot": F,
        "forward": F,
        "opt": opt,
        "strike": K,
        "dist": 0.0,
        "src": "ticker",
        "bid": mark - half,
        "ask": mark + half,
        "mark": mark,
        "mark_iv": iv,
    }
    r.update(kw)
    return r


def condor(**kw):
    return [row("lp", 85_000, "P", **kw), row("sp10", 92_000, "P", **kw), row("sc10", 108_000, "C", **kw), row("lc", 115_000, "C", **kw)]


def test_model_cost_is_vega_times_shift():
    r = row("sp10", 92_000, "P")
    T = TAU / 365.0
    vega_coin = black76_greeks(F, 92_000, T, 0.5, False)["vega"] / F  # USD per 1 vol point / F
    actual, model = leg_cost(r, -1, "open", hs=0.0001)
    assert actual == pytest.approx(0.0002, abs=1e-12)  # sold at the bid: mark - bid
    assert model == pytest.approx(vega_coin * 0.01, rel=1e-3)  # 0.0001 = 0.01 vol points


def test_close_swaps_the_sides():
    r = row("sp10", 92_000, "P", bid=None)
    assert leg_cost(r, -1, "open") is None  # selling needs the bid
    a, _ = leg_cost(r, -1, "close")  # buying back uses the ask
    assert a == pytest.approx(0.0002, abs=1e-12)
    assert leg_cost(row("lp", 85_000, "P", src="summary"), 1) is None


def test_condor_cost_sums_four_legs():
    legs, why = friday_entry(condor(), FRI, "BTC")
    assert why == "ok"
    c = condor_cost(legs)
    assert c["actual"] == pytest.approx(4 * 0.0002, abs=1e-12)
    assert c["model"] > 0 and c["ratio"] == pytest.approx(c["actual"] / c["model"])


def test_reading_rule_cutoff_band_and_order():
    late = int(datetime(2026, 11, 6, 10, 0, 1, tzinfo=UTC).timestamp())
    rows = condor()
    rows[1] = {**rows[1], "ts": late}
    assert friday_entry(rows, FRI, "BTC") == (None, "no row: sp10")
    rows = condor()
    rows[3] = {**rows[3], "dist": 0.016}
    assert friday_entry(rows, FRI, "BTC")[1] == "outside +-1.5 %: lc"
    rows = condor()
    rows[2] = {**rows[2], "strike": 99_000.0}  # short call below the forward
    assert friday_entry(rows, FRI, "BTC")[1] == "strike order"
    assert friday_entry(condor(), date(2026, 11, 7), "BTC")[1] == "not a Friday"
    assert friday_entry(condor(), FRI, "ETH")[0] is None


def test_rows_after_the_cutoff_do_not_change_the_entry():
    base, _ = friday_entry(condor(), FRI, "BTC")
    later = [{**r, "day": "2026-11-07", "role": "hold_" + r["role"], "ts": TS + 86_400, "bid": 0.5} for r in condor()] + [
        {**r, "ts": TS + 7_200, "bid": 0.9} for r in condor()
    ]  # same Friday, 11:00 UTC: after the cutoff
    after, why = friday_entry(condor() + later, FRI, "BTC")
    assert why == "ok" and after == base
    assert condor_cost(after) == condor_cost(base)


def test_entry_ignores_another_expiry_and_keeps_the_earliest_row():
    rows = condor()
    rows[0] = {**rows[0], "expiry_ts": EXP + 7 * 86_400}  # lp of the 2-week expiry
    assert friday_entry(rows, FRI, "BTC") == (None, "no row: lp")
    assert friday_entry(condor() + [rows[0]], FRI, "BTC")[0] == friday_entry(condor(), FRI, "BTC")[0]
    early = {**condor()[1], "ts": TS - 600, "bid": 0.001}  # sp10 seen twice by 10:00 UTC
    for order in ([early, *condor()], [*condor(), early]):
        legs, why = friday_entry(order, FRI, "BTC")
        assert why == "ok" and legs["sp10"]["ts"] == TS - 600  # independent of the iteration order


def test_thursday_exit_takes_the_entry_instruments_by_the_cutoff():
    ent = [{**r, "instrument": f"BTC-{r['strike']:.0f}-{r['opt']}"} for r in condor()]
    legs, _ = friday_entry(ent, FRI, "BTC")
    thu_ts = int(datetime(2026, 11, 12, 9, 0, 5, tzinfo=UTC).timestamp())
    hold = [{**r, "role": "hold_" + r["role"], "day": "2026-11-12", "ts": thu_ts, "tau_days": 0.96} for r in ent]
    ex, why = thursday_exit(hold, legs, FRI, "BTC")
    assert why == "ok" and ex["sp10"]["tau_days"] == 0.96
    other = [{**hold[0], "instrument": "BTC-80000-P"}] + hold[1:]
    assert thursday_exit(other, legs, FRI, "BTC")[1] == "no hold row: lp"
    late = [{**hold[3], "ts": thu_ts + 3_600}] + hold[:3]
    assert thursday_exit(late, legs, FRI, "BTC")[1] == "no hold row: lc"
    dup = [{**hold[1], "ts": thu_ts - 600, "tau_days": 0.97}]
    for order in (dup + hold, hold + dup):
        assert thursday_exit(order, legs, FRI, "BTC")[0]["sp10"]["tau_days"] == 0.97  # earliest ts wins
    assert condor_cost(ex, "close")["actual"] == pytest.approx(4 * 0.0002, abs=1e-12)


def test_ratio_ci_and_verdict():
    assert ratio_of_sums([1, 2, 3], [2, 2, 2]) == pytest.approx(1.0)
    assert math.isnan(ratio_of_sums([1], [0]))
    lo, hi = ratio_ci([0.5] * 8, [1.0] * 8)
    assert lo == hi == pytest.approx(0.5)
    assert cost_verdict(0.4, 0.9, 8) == "conservative"
    assert cost_verdict(1.1, 1.6, 8) == "optimistic"
    assert cost_verdict(0.8, 1.2, 8) == "consistent"
    assert cost_verdict(0.1, 0.2, 5) == "◯"


def test_rows_without_ts_are_skipped_not_read_as_time_zero():
    rows = condor()
    no_ts = {k: v for k, v in rows[1].items() if k != "ts"}
    assert friday_entry([*rows[:1], no_ts, *rows[2:]], FRI, "BTC") == (None, "no row: sp10")
    assert friday_entry([*rows[:1], {**rows[1], "ts": None}, *rows[2:]], FRI, "BTC") == (None, "no row: sp10")
    poisoned = {**no_ts, "bid": 0.001}  # would be "the earliest" if a missing ts were 0
    for order in ([poisoned, *condor()], [*condor(), poisoned]):
        legs, why = friday_entry(order, FRI, "BTC")
        assert why == "ok" and legs["sp10"]["ts"] == TS and legs["sp10"]["bid"] != 0.001
    ent = [{**r, "instrument": f"BTC-{r['strike']:.0f}-{r['opt']}"} for r in condor()]
    legs, _ = friday_entry(ent, FRI, "BTC")
    hold = [{**r, "role": "hold_" + r["role"], "day": "2026-11-12", "ts": TS + 6 * 86_400} for r in ent]
    assert thursday_exit([{**hold[0], "ts": None}, *hold[1:]], legs, FRI, "BTC")[1] == "no hold row: lp"
    for order in ([{**hold[0], "ts": None, "tau_days": 9.9}, *hold], [*hold, {**hold[0], "ts": None, "tau_days": 9.9}]):
        assert thursday_exit(order, legs, FRI, "BTC")[0]["lp"]["tau_days"] == TAU


# ---- part (a) report (item 8m): costs only -------------------------------------------------------------------------
T0 = date(2026, 11, 6)
# distinctive Thursday values that must never reach the report (prices, spot, delivery, P&L of part (b))
POISON = {
    "lp": 0.00731597,
    "sp10": 0.01913577,
    "sc10": 0.02468013,
    "lc": 0.00357911,
    "spot": 86421.7531,
    "delivery_price": 86013.4219,
    "pnl": 0.0424242,
}
HOLD_HALF = 0.0003


def week_rows(friday: date, *, with_hold: bool = True) -> list[dict]:
    exp = int(datetime(friday.year, friday.month, friday.day, 8, tzinfo=UTC).timestamp()) + 7 * 86_400
    ts = int(datetime(friday.year, friday.month, friday.day, 9, 0, 5, tzinfo=UTC).timestamp())
    out = []
    for r in condor():
        e = {**r, "day": friday.isoformat(), "ts": ts, "expiry_ts": exp, "instrument": f"BTC-{friday:%d%b}-{r['strike']:.0f}-{r['opt']}"}
        e.update(bid_iv=0.49, ask_iv=0.51, bid_size=0.5 if r["role"] == "sc10" else 3.0, ask_size=2.0)
        out.append(e)
        if with_hold:
            p = POISON[r["role"]]
            out.append(
                {
                    **e,
                    "role": "hold_" + r["role"],
                    "day": (friday + timedelta(days=6)).isoformat(),
                    "ts": ts + 6 * 86_400,
                    "tau_days": 0.96,
                    "spot": POISON["spot"],
                    "mark": p,
                    "bid": p - HOLD_HALF,
                    "ask": p + HOLD_HALF,
                    "delivery_price": POISON["delivery_price"],
                    "pnl": POISON["pnl"],
                    "target": POISON["spot"],
                }
            )
    return out


def _walk(x, keys: set, values: list):
    if isinstance(x, dict):
        for k, v in x.items():
            keys.add(k)
            _walk(v, keys, values)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _walk(v, keys, values)
    elif isinstance(x, float):
        values.append(x)


def test_report_never_carries_thursday_prices_spot_delivery_or_pnl():
    fr = cost_fridays(T0)
    rows = [r for f in fr for r in week_rows(f)]
    rep = cost_report(rows, fr, "BTC", n_boot=200)
    assert rep["entry"]["n_valid"] == rep["exit"]["n_valid"] == 8
    assert rep["exit"]["actual_mean"] == pytest.approx(4 * HOLD_HALF)  # the buy-back cost is still measured
    keys: set = set()
    values: list = []
    _walk(rep, keys, values)
    assert keys <= REPORT_KEYS, keys - REPORT_KEYS
    assert not keys & {"mark", "spot", "bid", "ask", "credit", "delivery_price", "pnl", "price"}
    assert "weeks" not in rep["exit"] and "credit_share" not in rep["exit"]  # no per-week buy-back rows
    bad = [v for p in POISON.values() for v in (p, p - HOLD_HALF, p + HOLD_HALF)]
    for v in values:
        assert all(not math.isclose(v, b, rel_tol=1e-9) for b in bad), v
    text = "\n".join(format_cost_report(rep, "primary"))
    for b in bad:
        for s in (repr(b), f"{b:.4f}", f"{b:.6f}", f"{100 * b:.3f}", f"{100 * b:.4f}", f"{b * POISON['spot']:.0f}"):
            assert s not in text, s
    for word in ("delivery", "P&L", "pnl", "86421", "86013"):
        assert word not in text


def test_script_output_has_no_forbidden_field():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import condor_forward_costs as cfc

    fr = cost_fridays(T0)
    rows = [r for f in fr for r in week_rows(f)]
    lines, reps = cfc.build(rows, fr, interim=False)
    text = "\n".join(lines)
    for p in POISON.values():
        assert repr(p) not in text and f"{p:.4f}" not in text and f"{100 * p:.3f}" not in text
    assert set(reps) == {"BTC", "ETH"} and reps["ETH"]["entry"]["n_valid"] == 0
    assert cfc.registered_end(T0) == datetime(2026, 12, 31, 10, tzinfo=UTC)


def _script():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import condor_forward_costs as cfc

    return cfc


def test_interim_computes_and_prints_nothing_of_the_buyback():
    fr = cost_fridays(T0)
    rows = [r for f in fr[:2] for r in week_rows(f)]  # two weeks done, Thursday rows present in the input
    full = cost_report(rows, fr, "BTC", n_boot=200)
    leak = [full["exit"]["actual_mean"], full["exit"]["model_mean"]]
    rep = cost_report(rows, fr, "BTC", n_boot=200, interim=True)
    assert "exit" not in rep and rep["interim"] is True
    assert rep["entry"]["n_valid"] == 2 and rep["entry"]["verdict"] == "—"
    keys: set = set()
    values: list = []
    _walk(rep, keys, values)
    assert keys <= REPORT_KEYS
    for v in values:
        assert all(not math.isclose(v, b, rel_tol=1e-9) for b in leak), v
    lines, reps = _script().build(rows, fr, interim=True)
    text = "\n".join(lines)
    assert "| (a2) Thursday buy-back |" not in text and "(a2) Thursday buy-back (median)" not in text
    assert "INTERIM" in text and "not computed in an interim look" in text
    for b in leak:
        for s in (repr(b), f"{100 * b:.3f}", f"{100 * b:.4f}"):
            assert s not in text, s
    assert all("exit" not in r for r in reps.values())
    dumped = json.dumps(json_safe(reps), allow_nan=False)
    assert "exit" not in json.loads(dumped)["BTC"]


def test_json_safe_replaces_nan():
    x = {"a": math.nan, "b": [1.0, math.inf, {"c": math.nan}], "d": "x", "e": 2}
    assert json_safe(x) == {"a": None, "b": [1.0, None, {"c": None}], "d": "x", "e": 2}
    rep = cost_report([], cost_fridays(T0), "ETH", n_boot=50)  # no rows: NaN means everywhere
    json.dumps(json_safe(rep), allow_nan=False)


def test_runtime_whitelist_guard_exits_before_writing(monkeypatch, tmp_path):
    cfc = _script()
    fr = cost_fridays(T0)
    rows = [r for f in fr for r in week_rows(f)]
    real = cfc.cost_report

    def mutant(*a, **k):
        rep = real(*a, **k)
        rep["exit"]["mark"] = POISON["lp"]  # a forbidden key slipped in
        return rep

    monkeypatch.setattr(cfc, "cost_report", mutant)
    assert foreign_keys(real(rows, fr, "BTC", n_boot=50)) == set()
    with pytest.raises(SystemExit, match="outside REPORT_KEYS"):
        cfc.build(rows, fr, interim=False)
    monkeypatch.setattr(cfc, "_now", lambda: datetime(2027, 1, 4, tzinfo=UTC))
    monkeypatch.setattr(cfc, "detect_t0", lambda db, now: T0)
    monkeypatch.setattr(cfc, "copy_db", lambda src, dst: None)
    monkeypatch.setattr(cfc, "load_rows", lambda db, f, *, with_hold=True: rows)
    out = tmp_path / "a.md"
    with pytest.raises(SystemExit, match="outside REPORT_KEYS"):
        cfc.main(["--out", str(out)])
    assert not out.exists() and not out.with_suffix(".json").exists()


@pytest.mark.parametrize(
    ("now", "argv"),
    [
        (datetime(2026, 11, 1, 12, tzinfo=UTC), ["--interim"]),  # before T0
        (datetime(2026, 11, 1, 12, tzinfo=UTC), ["--interim", "--t0", "2026-10-30"]),
        (datetime(2026, 11, 6, 9, 59, tzinfo=UTC), ["--interim"]),  # T0 day, before the 10:00 cutoff
        (datetime(2026, 11, 20, 12, tzinfo=UTC), ["--interim", "--t0", "2026-10-30"]),  # --t0 before EARLIEST_T0
        (datetime(2027, 3, 1, tzinfo=UTC), ["--t0", "2026-10-30"]),
    ],
)
def test_main_refuses_without_touching_the_store(monkeypatch, tmp_path, now, argv):
    cfc = _script()

    def boom(*a, **k):
        raise AssertionError("the store was touched")

    monkeypatch.setattr(cfc, "_now", lambda: now)
    monkeypatch.setattr(cfc, "copy_db", boom)
    monkeypatch.setattr(cfc, "load_rows", boom)
    out = tmp_path / "a.md"
    with pytest.raises(SystemExit):
        cfc.main([*argv, "--db", str(tmp_path / "none.sqlite"), "--out", str(out)])
    assert not out.exists()
    assert cfc.EARLIEST_T0 == date(2026, 11, 6)


def _entry_db(path, days_roles):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE option_leg_snapshot (day TEXT, currency TEXT, role TEXT, ts INTEGER)")
    for day, role, ts in days_roles:
        con.execute("INSERT INTO option_leg_snapshot VALUES (?, 'BTC', ?, ?)", (day, role, ts))
    con.commit()
    con.close()


def _ts_of(d: date, h: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, tzinfo=UTC).timestamp())


def test_detect_t0_reads_only_complete_entry_fridays(tmp_path):
    cfc = _script()
    f1, f2 = T0, T0 + timedelta(days=7)
    roles = ("lp", "sp10", "sc10", "lc")
    rows = [(f1.isoformat(), r, _ts_of(f1, 9)) for r in roles[:3]]  # T0 incomplete: one role missing
    rows += [(f2.isoformat(), r, _ts_of(f2, 9)) for r in roles]
    rows += [((f1 - timedelta(days=7)).isoformat(), r, _ts_of(f1, 9)) for r in roles]  # before EARLIEST_T0
    p = tmp_path / "s.sqlite"
    _entry_db(p, rows)
    now = datetime(2027, 1, 8, tzinfo=UTC)
    assert cfc.detect_t0(p, now) == f2
    _entry_db(tmp_path / "t.sqlite", [(f1.isoformat(), r, _ts_of(f1, 9)) for r in roles])
    assert cfc.detect_t0(tmp_path / "t.sqlite", now) == f1
    late = tmp_path / "u.sqlite"
    _entry_db(late, [(f1.isoformat(), r, _ts_of(f1, 11)) for r in roles])  # taken after the 10:00 cutoff
    assert cfc.detect_t0(late, now) is None


@pytest.mark.parametrize(("detected", "argv"), [(T0 + timedelta(days=7), ["--t0", "2026-11-06"]), (T0, ["--t0", "2026-11-13"]), (None, [])])
def test_main_refuses_a_shifted_or_missing_t0(monkeypatch, tmp_path, detected, argv):
    cfc = _script()
    fr = cost_fridays(T0)
    rows = [r for f in fr for r in week_rows(f)]

    def no_hold(*a, **k):
        raise AssertionError("rows were loaded")

    monkeypatch.setattr(cfc, "_now", lambda: datetime(2027, 1, 8, tzinfo=UTC))
    monkeypatch.setattr(cfc, "copy_db", lambda src, dst: None)
    monkeypatch.setattr(cfc, "detect_t0", lambda db, now: detected)
    monkeypatch.setattr(cfc, "load_rows", no_hold)
    out = tmp_path / "a.md"
    with pytest.raises(SystemExit):
        cfc.main([*argv, "--out", str(out)])
    assert not out.exists() and not out.with_suffix(".json").exists()
    assert rows  # sample is non-empty: refusal is by T0, not by data


@pytest.mark.parametrize(
    ("now", "argv"),
    [
        (datetime(2026, 11, 20, 12, tzinfo=UTC), []),  # registered run before 2026-12-31 10:00 UTC
        (datetime(2026, 12, 31, 9, 59, tzinfo=UTC), []),
    ],
)
def test_main_refuses_before_the_registered_end(monkeypatch, tmp_path, now, argv):
    cfc = _script()

    def boom(*a, **k):
        raise AssertionError("rows were loaded")

    monkeypatch.setattr(cfc, "_now", lambda: now)
    monkeypatch.setattr(cfc, "copy_db", lambda src, dst: None)
    monkeypatch.setattr(cfc, "detect_t0", lambda db, n: T0)
    monkeypatch.setattr(cfc, "load_rows", boom)
    out = tmp_path / "a.md"
    with pytest.raises(SystemExit):
        cfc.main([*argv, "--out", str(out)])
    assert not out.exists()


@pytest.mark.parametrize("interim", [True, False])
def test_main_writes_standard_json(monkeypatch, tmp_path, interim):
    cfc = _script()
    monkeypatch.setattr(cfc, "detect_t0", lambda db, now: T0)
    fr = cost_fridays(T0)
    seen = {}

    def load(db, fridays, *, with_hold=True):
        seen["with_hold"] = with_hold
        return [r for f in fridays for r in week_rows(f, with_hold=with_hold)]

    now = datetime(2026, 11, 20, 12, tzinfo=UTC) if interim else datetime(2026, 12, 31, 10, tzinfo=UTC)
    monkeypatch.setattr(cfc, "_now", lambda: now)
    monkeypatch.setattr(cfc, "copy_db", lambda src, dst: None)
    monkeypatch.setattr(cfc, "load_rows", load)
    out = tmp_path / "a.md"
    cfc.main(["--out", str(out), *(["--interim"] if interim else [])])
    assert seen["with_hold"] is (not interim)  # the interim look does not even read the Thursday rows
    raw = out.with_suffix(".json").read_text()
    reps = json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-standard JSON constant {c}"))
    assert reps["ETH"]["entry"]["R"] is None  # NaN -> null
    assert ("exit" in reps["BTC"]) is (not interim)
    text = out.read_text()
    assert f"--t0 {fr[0]}" in text and ("INTERIM" in text) is interim


def test_fewer_than_six_valid_weeks_is_inconclusive():
    fr = cost_fridays(T0)
    rows = [r for f in fr[:5] for r in week_rows(f)]  # 3 Fridays without any snapshot
    rep = cost_report(rows, fr, "BTC", n_boot=200)
    e = rep["entry"]
    assert e["n_valid"] == 5 and e["n_weeks"] == 8 and e["verdict"] == "◯"
    assert e["skip_counts"] == [["no row: lp sp10 sc10 lc", 3]]
    assert rep["exit"]["verdict"] == "◯" and ["no valid entry", 3] in rep["exit"]["skip_counts"]
    six = cost_report([r for f in fr[:6] for r in week_rows(f)], fr, "BTC", n_boot=200)
    assert six["entry"]["verdict"] != "◯"  # 6 of 8 pass the gate
    no_side = [{**r, "bid": None} if r["role"] == "sp10" and r["day"] == fr[0].isoformat() else r for r in rows]
    rep = cost_report(no_side, fr, "BTC", n_boot=200)
    assert rep["entry"]["n_valid"] == 4 and ["no quote: sp10(no side)", 1] in rep["entry"]["skip_counts"]


def test_report_descriptives_on_synthetic_weeks():
    fr = cost_fridays(T0)
    rep = cost_report([r for f in fr for r in week_rows(f)], fr, "BTC", n_boot=200)
    e = rep["entry"]
    assert e["actual_mean"] == pytest.approx(4 * 0.0002)
    assert e["half_spread_vp"]["sp10"]["median"] == pytest.approx(1.0)
    assert e["small_size"] == {"n_legs": 32, "n_known": 32, "n_small": 8, "share": 0.25}  # sc10 sold into a 0.5 bid
    assert rep["exit"]["small_size"]["n_small"] == 0  # buy-back of sc10 trades the ask (size 2)
    legs, _ = friday_entry(week_rows(T0), T0, "BTC")
    credit = sum(-q * legs[k]["mark"] for k, q in H1_QTY.items())
    assert e["credit_share"]["actual"] == pytest.approx(4 * 0.0002 / credit)
    assert len(e["weeks"]) == 8 and e["R"] == pytest.approx(e["weeks"][0]["ratio"])


def fixture_rows() -> list[dict]:
    """The saved live answer of 2026-11-01 (expiry 2026-11-06) relabelled as the Friday 2026-10-30 09:00 entry."""
    live = json.loads(FIX.read_text())
    now = datetime.fromisoformat(live["fetched"])
    exp_dt = datetime(2026, 11, 6, 8, tzinfo=UTC)
    ts = int(datetime(2026, 10, 30, 9, tzinfo=UTC).timestamp())
    out = []
    for cur in ("BTC", "ETH"):
        exp = expiry_surface(live["summary"][cur]["result"], cur, exp_dt, now)
        legs, _ = plan_legs(exp)
        for leg in legs:
            if leg["role"] in H1_QTY:
                t = (live["ticker"].get(leg["instrument"]) or {}).get("result")
                out.append({**leg_row(leg, exp, t, currency=cur, expiry=exp_dt, now=now), "day": "2026-10-30", "ts": ts})
    return out


def test_fixture_entry_cost_matches_the_registered_prior():
    rows = fixture_rows()
    fri = [date(2026, 10, 30)]
    raw = cost_report(rows, fri, "BTC", n_boot=200)  # the Sunday grid: BTC lp 65000 is 1.51 % of spot from its target
    assert raw["entry"]["n_valid"] == 0 and raw["entry"]["skips"] == [["2026-10-30", "outside +-1.5 %: lp"]]
    # the cost prior of part (a) of the 8j registration (not stage 0 of part (b)) was taken without the band:
    # lift it only to reach the cost code
    btc = cost_report([{**r, "dist": 0.0} for r in rows], fri, "BTC", n_boot=200)
    eth = cost_report(rows, fri, "ETH", n_boot=200)
    assert btc["entry"]["n_valid"] == 1 and btc["entry"]["verdict"] == "◯"
    assert btc["entry"]["R"] == pytest.approx(0.73, abs=0.01)  # registration 8j: BTC 0.050 / 0.069
    assert btc["entry"]["actual_mean"] == pytest.approx(0.00050, abs=0.00001)
    assert eth["entry"]["R"] == pytest.approx(0.84, abs=0.01)  # ETH 0.080 / 0.096
    assert btc["entry"]["half_spread_vp"]["sp10"]["median"] == pytest.approx(0.77, abs=0.01)
    assert btc["exit"]["skip_counts"] == [["no hold row: lp sp10 sc10 lc", 1]]
    assert btc["entry"]["small_size"]["n_small"] >= 1  # the sc10 best bid was 0.1 BTC


def test_mde_arithmetic():
    assert mde(1.0, 49) == pytest.approx(0.4)
    assert weeks_for_mde(1.0, 0.28) == 100  # (2.8 / 0.28)^2 exactly
    assert power(1.0, 49, 0.4) == pytest.approx(0.80, abs=0.003)  # MDE is the 80 %-power effect (2.8 = 1.96 + 0.84)
    assert power(1.0, 49, 1.959964 / 7) == pytest.approx(0.5)
    n = weeks_for_mde(0.42, 0.13)
    assert mde(0.42, n) <= 0.13 < mde(0.42, n - 1)
