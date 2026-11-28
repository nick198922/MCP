import math
import random

import pytest

from tradedesk.analytics import level_break as lb


def synth(n: int, split: int, jump: float, seed: int = 1) -> tuple[dict, dict, dict, dict]:
    """Two venues sharing a heavy-tailed daily market shock; venue b's level is multiplied by exp(jump) from split."""
    rng = random.Random(seed)
    liq_a, liq_b, oi_a, oi_b = {}, {}, {}, {}
    for d in range(n):
        shock = math.exp(rng.gauss(0, 1.0)) * (20 if rng.random() < 0.03 else 1)
        oi_a[d], oi_b[d] = 1e9, 5e8
        liq_a[d] = 1e6 * shock * math.exp(rng.gauss(0, 0.2))
        liq_b[d] = 5e5 * shock * math.exp(rng.gauss(0, 0.2)) * (math.exp(jump) if d >= split else 1.0)
    return liq_a, oi_a, liq_b, oi_b


def test_gaps():
    assert lb.gaps([1, 2, 5, 6, 9]) == [(3, 4), (7, 8)]
    assert lb.gaps([3, 3, 4]) == []


def test_log_ratio_uses_previous_day_oi_and_drops_zeros():
    liq_a = {1: 10.0, 2: 0.0, 3: 10.0}
    liq_b = {1: 20.0, 2: 5.0, 3: 20.0}
    oi_a = {0: 100.0, 1: 100.0, 2: 100.0}
    oi_b = {0: 100.0, 1: 100.0, 2: 50.0}
    y, dropped = lb.log_ratio_series(liq_a, oi_a, liq_b, oi_b)
    assert y[1] == pytest.approx(math.log(2))
    assert y[3] == pytest.approx(math.log(4))  # OI of day 2 (50) for venue b
    assert 2 not in y and dropped["zero_liq"] == 1


def test_windows_skip_transition():
    before, after = lb.windows(100, 10, 3, 5)
    assert (before.start, before.stop) == (87, 97)
    assert (after.start, after.stop) == (105, 115)


def test_split_stat_recovers_jump_and_placebo_excludes_truth():
    la, oa, lbb, ob = synth(800, 400, math.log(3))
    y, _ = lb.log_ratio_series(la, oa, lbb, ob)
    s = lb.split_stat(y, 400, 90, 7, 15, 0.8)
    assert s.stat == pytest.approx(math.log(3), abs=0.1)
    null = lb.placebo([y], 400, 90, 7, 15, 0.8)
    assert null and all(abs(v) < 0.2 for _, v in null)
    for sp, _ in null:  # no placebo span touches the transition [393, 415)
        assert sp + 15 + 90 <= 393 or sp - 7 - 90 >= 415
    up, two = lb.placebo_rank(s.stat, [v for _, v in null])
    assert up == pytest.approx(1 / (1 + len(null)))


def test_stat_ignores_days_outside_windows():
    la, oa, lbb, ob = synth(600, 300, 0.0)
    y, _ = lb.log_ratio_series(la, oa, lbb, ob)
    base = lb.split_stat(y, 300, 90, 7, 15, 0.8).stat
    y2 = dict(y)
    for d in range(405, 600):  # after the after-window [315, 405): changing later data changes nothing
        y2[d] = 99.0
    for d in range(0, 203):  # before the before-window [203, 293)
        y2[d] = -99.0
    assert lb.split_stat(y2, 300, 90, 7, 15, 0.8).stat == base


def test_log_ratio_day_depends_only_on_itself_and_previous_day():
    la, oa, lbb, ob = synth(50, 25, 0.5)
    y1, _ = lb.log_ratio_series(la, oa, lbb, ob)
    for d in range(30, 50):
        la[d] *= 7
        oa[d] *= 3
    y2, _ = lb.log_ratio_series(la, oa, lbb, ob)
    assert all(y1[d] == y2[d] for d in range(1, 30))


def test_bootstrap_ci_covers_jump_and_no_jump():
    for jump in (0.0, 1.0):
        series = []
        for seed in (1, 2):
            la, oa, lbb, ob = synth(400, 200, jump, seed)
            series.append(lb.log_ratio_series(la, oa, lbb, ob)[0])
        before, after = lb.windows(200, 90, 7, 15)
        lo, hi, sd = lb.block_bootstrap(series, before, after, 7, 400, 3)
        assert lo < jump < hi and sd > 0
        assert lb.pooled_stat(series, 200, 90, 7, 15, 0.8) == pytest.approx(jump, abs=0.12)


def test_sum_ratio_change():
    la, oa, lbb, ob = synth(400, 200, math.log(2))
    before, after = lb.windows(200, 90, 7, 15)
    assert lb.sum_ratio_change(la, oa, lbb, ob, before, after) == pytest.approx(math.log(2), abs=0.15)


def _script():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "verify_coinalyze_bybit_break.py"
    spec = importlib.util.spec_from_file_location("verify_coinalyze_bybit_break", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(tmp_path, last_liq_day: int) -> str:
    import sqlite3

    path = str(tmp_path / "t.sqlite")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE coinalyze_history (symbol TEXT, metric TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
        " close REAL, liq_long_usd REAL, liq_short_usd REAL)"
    )
    mod = _script()
    for coin, (a, b) in mod.COINS.items():
        la, oa, lbb, ob = synth(900, 500, math.log(3), seed=len(coin) + ord(coin[0]))
        for sym, liq, oi in ((a, la, oa), (b, lbb, ob)):
            for i in range(900):
                ts = (mod.SPLIT - 500 + i) * lb.DAY
                con.execute("INSERT INTO coinalyze_history VALUES (?, 'oi_usd', ?, 0, 0, 0, ?, NULL, NULL)", (sym, ts, oi[i]))
                if mod.SPLIT - 500 + i <= last_liq_day:
                    v = liq[i] / 2
                    con.execute("INSERT INTO coinalyze_history VALUES (?, 'liq_usd', ?, NULL, NULL, NULL, NULL, ?, ?)", (sym, ts, v, v))
    con.commit()
    con.close()
    return path


def test_script_refuses_without_coverage_and_runs_on_synthetic(tmp_path, monkeypatch, capsys):
    mod = _script()
    out = tmp_path / "r.json"
    monkeypatch.setattr("sys.argv", ["x", "--stage", "run", "--db", _db(tmp_path, mod.SPLIT), "--out", str(out)])
    assert mod.main() == 2 and not out.exists()
    assert "REFUSED" in capsys.readouterr().out
    (tmp_path / "t.sqlite").unlink()
    monkeypatch.setattr("sys.argv", ["x", "--stage", "run", "--db", _db(tmp_path, 10**9), "--out", str(out)])
    assert mod.main() == 0 and out.exists()
    text = capsys.readouterr().out
    assert "VERDICT: break" in text


def test_verdict_rules():
    v = lb.verdict
    assert v(0.8, (0.5, 1.1), 0.01, 0.02, {"BTC": 0.7, "ETH": 0.9}, True, 0.0, False)[0] == "break"
    assert v(0.8, (0.5, 1.1), 0.01, 0.02, {"BTC": 0.7, "ETH": 0.9}, False, 0.0, False)[0] == "inconclusive"
    assert v(0.3, (0.1, 0.5), 0.01, 0.02, {"BTC": 0.3, "ETH": 0.3}, True, 0.0, False)[0] == "inconclusive"
    assert v(0.02, (-0.1, 0.15), 0.4, 0.8, {"BTC": 0.0, "ETH": 0.04}, True, 0.0, False)[0] == "no_break"
    assert v(0.02, (-0.3, 0.35), 0.4, 0.8, {"BTC": 0.0, "ETH": 0.04}, True, 0.0, False)[0] == "inconclusive"
    assert v(-0.8, (-1.1, -0.5), 0.99, 0.02, {"BTC": -0.7, "ETH": -0.9}, True, 0.0, False)[0] == "other_break"
    assert v(0.8, (0.5, 1.1), 0.01, 0.02, {"BTC": 0.7, "ETH": 0.9}, True, 0.2, False)[0] == "inconclusive"
    assert v(0.8, (0.5, 1.1), 0.01, 0.02, {"BTC": 0.7, "ETH": 0.9}, True, 0.0, True)[0] == "inconclusive"
