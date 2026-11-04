from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics import regime_side as rs

D = rs.DAY_MS
T0 = 1_600_000_000_000 // D * D


def _closes(vals: list[float], start: int = T0) -> dict[int, float]:
    return {start + i * D: v for i, v in enumerate(vals)}


def test_sma_sign_rising_falling_and_warmup() -> None:
    up = rs.sma_sign(_closes([float(x) for x in range(1, 11)]), 5)
    assert [up[T0 + i * D] for i in range(10)] == [0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
    dn = rs.sma_sign(_closes([float(x) for x in range(10, 0, -1)]), 5)
    assert dn[T0 + 9 * D] == -1
    flat = rs.sma_sign(_closes([3.0] * 6), 5)
    assert flat[T0 + 5 * D] == 1  # close == SMA counts as above (as cycle_regime's price_to_sma >= 0)


def test_sma_sign_needs_consecutive_days() -> None:
    c = _closes([1.0, 2.0, 3.0])
    c[T0 + 4 * D] = 4.0  # a missing day breaks the window
    c[T0 + 5 * D] = 5.0
    s = rs.sma_sign(c, 3)
    assert s[T0 + 2 * D] == 1
    assert s[T0 + 4 * D] == 0 and s[T0 + 5 * D] == 0


def test_sma_sign_no_lookahead() -> None:
    rng = np.random.default_rng(1)
    vals = list(100 + np.cumsum(rng.normal(size=60)))
    a = rs.sma_sign(_closes(vals), 20)
    vals2 = vals[:40] + [v * 3 for v in vals[40:]]
    b = rs.sma_sign(_closes(vals2), 20)
    assert all(a[T0 + i * D] == b[T0 + i * D] for i in range(40))


def test_sign_before_uses_last_closed_day() -> None:
    signs = {T0: 1, T0 + D: -1}
    assert rs.sign_before(T0 + D, signs) == 1  # day T0 closes exactly at T0 + D
    assert rs.sign_before(T0 + D + 5 * 3_600_000, signs) == 1
    assert rs.sign_before(T0 + 2 * D, signs) == -1
    assert rs.sign_before(T0 + 2 * D - 1, signs) == 1
    assert rs.sign_before(T0, signs) == 0


def test_cycle_sign_and_split() -> None:
    assert [rs.cycle_sign(s) for s in ("growth", "accumulation", "decline", "bottom", "overheat", None, "none")] == [1, 1, -1, 0, 0, 0, 0]
    k, d = rs.split(["long", "long", "short", "short", "long"], [1, -1, 1, -1, 0])
    assert k.tolist() == [True, False, False, True, False]
    assert d.tolist() == [False, True, True, False, False]


def test_delta_boot_point_pooling_and_centering() -> None:
    n = 40
    g = np.array([0] * n + [1] * n)
    cl = np.tile(np.arange(n), 2)
    kept = np.tile(np.arange(n) % 2 == 0, 2)
    dropped = ~kept
    r = np.where(kept, 1.0, 0.0)
    r[n:] = np.where(kept[n:], 0.5, 0.5)  # group 1: no difference
    r[:n] += np.linspace(-0.1, 0.1, n)
    res = rs.delta_boot(g, cl, kept, dropped, r, n_boot=500)
    d0 = r[:n][kept[:n]].mean() - r[:n][dropped[:n]].mean()
    assert math.isclose(res["per"][0]["m"], d0, abs_tol=1e-12)
    assert math.isclose(res["per"][1]["m"], 0.0, abs_tol=1e-12)
    assert math.isclose(res["pool"]["m"], d0 / 2, abs_tol=1e-12)
    assert res["pool"]["lo"] < res["pool"]["m"] < res["pool"]["hi"]
    tr = rs.delta_boot(g, cl, kept, dropped, r, pooled="trades", n_boot=200)
    assert math.isclose(tr["pool"]["m"], r[kept].mean() - r[dropped].mean(), abs_tol=1e-12)
    c = rs.delta_boot(g, cl, kept, dropped, r, n_boot=500, center=True)
    assert abs(c["pool"]["m"]) < 1e-12 and c["pool"]["se"] > 0
    assert c["pool"]["n_k"] == float(kept.sum()) and c["pool"]["n_d"] == float(dropped.sum())


def test_delta_boot_past_unchanged_by_future_rows() -> None:
    rng = np.random.default_rng(3)
    n = 200
    g = np.zeros(n, dtype=np.int64)
    t = np.arange(n)
    kept = rng.random(n) < 0.5
    r = rng.normal(size=n)
    past = t < 120
    a = rs.delta_boot(g[past], t[past] // 10, kept[past], ~kept[past], r[past], n_boot=300)
    r2 = r.copy()
    r2[~past] += 5.0
    b = rs.delta_boot(g[past], t[past] // 10, kept[past], ~kept[past], r2[past], n_boot=300)
    assert a["pool"] == b["pool"]


def test_random_drop_keeps_counts_per_stratum() -> None:
    st = np.array(["a"] * 10 + ["b"] * 10)
    dropped = np.array([True] * 3 + [False] * 7 + [True] * 6 + [False] * 4)
    el = np.ones(20, dtype=bool)
    el[0] = False  # not eligible: never drawn
    dropped[0] = False
    out = rs.random_drop(st, dropped, el, np.random.default_rng(0))
    assert out[:10].sum() == 2 and out[10:].sum() == 6 and not out[0]


def test_shift_signs_preserves_share() -> None:
    s = {T0 + i * D: v for i, v in enumerate([0, 1, 1, 1, -1, -1])}
    sh = rs.shift_signs(s, 2)
    assert sh[T0] == 0
    assert sorted(sh.values()) == sorted(s.values())
    assert [sh[T0 + i * D] for i in range(1, 6)] == [-1, -1, 1, 1, 1]
    assert rs.shift_signs(s, 0) == s


def test_mde80() -> None:
    assert math.isclose(rs.mde80(1.0), 1.959964 + 0.841621, abs_tol=1e-5)
    assert math.isclose(rs.mde80(0.01, alpha=0.025), 0.01 * (2.241403 + 0.841621), abs_tol=1e-6)
