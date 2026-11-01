import random

import pytest

from tradedesk.analytics.carry_delisting import simulate_carry_delisting
from tradedesk.analytics.carry_entry_filter import (
    DAY_MS,
    age_ok,
    block_symbols,
    first_traded_open,
    make_entry_filter,
    median_volume,
    random_blocked,
    volume_ok,
)

H8 = 8 * 3_600_000
H4 = 4 * 3_600_000
T0 = 1_700_006_400_000  # a UTC midnight
KW = dict(slots=2, entry_apr=0.2, spot_fee=0.001, perp_fee=0.0005, slippage=0.002)


def _daily(start, vols):
    """Daily rows [open, o, h, l, c, base_volume, quote_volume]."""
    return [[start + i * DAY_MS, 1.0, 1.0, 1.0, 1.0, (v > 0) * 1.0, v] for i, v in enumerate(vols)]


def _market(rates, prices, start=T0):
    return {
        "funding": [{"ts_ms": start + i * H8, "rate": r} for i, r in enumerate(rates)],
        "spot": [{"ts": start + i * H4, "close": prices[i]} for i in range(len(prices))],
        "perp": [{"ts": start + i * H4, "close": prices[i] * 1.001, "high": prices[i] * 1.01} for i in range(len(prices))],
    }


def _random_markets(seed=7, n_sym=6, n_pay=300):
    rng = random.Random(seed)
    out = {}
    for k in range(n_sym):
        rates, r = [], 0.0002
        for _ in range(n_pay):
            r = 0.9 * r + 0.1 * rng.gauss(0.0002, 0.004)
            rates.append(r)
        p, prices = 100.0, []
        for _ in range(n_pay * 2 + 2):
            p *= 1 + rng.gauss(0, 0.02)
            prices.append(p)
        out[f"S{k}"] = _market(rates, prices)
    return out


def test_first_traded_open_skips_zero_volume_filler():
    rows = _daily(T0, [0.0, 0.0, 5.0, 7.0])
    assert first_traded_open(rows) == T0 + 2 * DAY_MS
    assert first_traded_open(_daily(T0, [0.0, 0.0])) is None
    assert first_traded_open([]) is None


def test_age_boundaries():
    first = T0
    assert age_ok(first, T0 + 180 * DAY_MS, 180)
    assert not age_ok(first, T0 + 180 * DAY_MS - 1, 180)
    assert not age_ok(None, T0 + 1000 * DAY_MS, 180)
    assert not age_ok(T0 + DAY_MS, T0, 180)  # listed after t


def test_median_volume_reads_only_closed_days():
    vols = [float(i) for i in range(1, 41)]  # day i has volume i + 1
    rows = _daily(T0, vols)
    opens, qv = [int(r[0]) for r in rows], [r[6] for r in rows]
    t = T0 + 35 * DAY_MS + 9 * 3_600_000  # inside day 35: days 5..34 are the window (volumes 6..35)
    assert median_volume(opens, qv, t) == pytest.approx(statistics_median(range(6, 36)))
    # the bar of day 35 opened before t but has not closed: it is not read
    qv2 = list(qv)
    qv2[35] = 1e12
    assert median_volume(opens, qv2, t) == median_volume(opens, qv, t)
    # too little history
    assert median_volume(opens, qv, T0 + 10 * DAY_MS) is None
    assert median_volume(opens, qv, T0 + 20 * DAY_MS) == pytest.approx(10.5)  # days 0..19, volumes 1..20
    assert volume_ok(opens, qv, t, 20.5) and not volume_ok(opens, qv, t, 21.0)


def statistics_median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2 - 1] + xs[n // 2]) / 2 if n % 2 == 0 else xs[n // 2]


def test_filter_is_causal_in_its_inputs():
    """Changing daily bars that close after t (spot volume, perp first bar) never changes the decision at t."""
    t = T0 + 200 * DAY_MS + 5 * 3_600_000
    day_t = t // DAY_MS * DAY_MS
    spot = _daily(T0, [2e7] * 240)
    perp = _daily(T0 + 10 * DAY_MS, [1.0] * 230)
    f = make_entry_filter(first_open={"X": first_traded_open(perp)}, min_age_days=180, spot_daily={"X": spot}, min_volume=1e7)
    before = [f("X", t - k * H8) for k in range(60)]
    spot2 = [r if r[0] < day_t else [*r[:6], 0.0] for r in spot]
    perp2 = [r for r in perp if r[0] < day_t]  # the perp's future (bars after t) is cut away
    f2 = make_entry_filter(first_open={"X": first_traded_open(perp2)}, min_age_days=180, spot_daily={"X": spot2}, min_volume=1e7)
    assert [f2("X", t - k * H8) for k in range(60)] == before
    assert before[0] is True
    # a perp listed after t: whatever its future first bar, the answer at t is "no"
    for later in (day_t + DAY_MS, day_t + 50 * DAY_MS):
        g = make_entry_filter(first_open={"X": later}, min_age_days=180)
        assert g("X", t) is False


def test_missing_data_is_not_allowed():
    f = make_entry_filter(first_open={}, min_age_days=180)
    assert f("NOPE", T0 + 1000 * DAY_MS) is False
    g = make_entry_filter(spot_daily={}, min_volume=1.0)
    assert g("NOPE", T0 + 1000 * DAY_MS) is False
    h = make_entry_filter()  # no filter set
    assert h("NOPE", T0) is True


def test_simulation_none_filter_and_block_all():
    mk = _random_markets()
    a = simulate_carry_delisting(mk, **KW)
    b = simulate_carry_delisting(mk, entry_allowed=lambda s, t: True, **KW)
    assert len(a["trades"]) > 3
    assert [(x["symbol"], x["open_ts"], x["close_ts"]) for x in a["trades"]] == [
        (x["symbol"], x["open_ts"], x["close_ts"]) for x in b["trades"]
    ]
    assert a["equity"] == b["equity"]
    none = simulate_carry_delisting(mk, entry_allowed=lambda s, t: False, **KW)
    assert none["trades"] == [] and all(v == 0 for _, v in none["equity"])
    top = max({x["symbol"] for x in a["trades"]}, key=lambda s: sum(1 for x in a["trades"] if x["symbol"] == s))
    c = simulate_carry_delisting(mk, entry_allowed=block_symbols([top]), **KW)
    assert top not in {x["symbol"] for x in c["trades"]} and c["trades"]


def test_simulation_with_filter_is_causal():
    """The filter reads daily spot volume: zeroing every bar after ``cut`` must not change trades opened, trades
    closed or equity at or before ``cut``."""
    mk = _random_markets()
    n_days = 220
    spot = {s: _daily(T0 - 40 * DAY_MS, [2e7 if (i + k) % 9 else 5e6 for i in range(n_days)]) for k, s in enumerate(mk)}
    first = {s: T0 - 200 * DAY_MS + k * 3 * DAY_MS for k, s in enumerate(mk)}
    kw = dict(min_age_days=180, min_volume=1e7, first_open=first)
    a = simulate_carry_delisting(mk, entry_allowed=make_entry_filter(spot_daily=spot, **kw), **KW)
    cut = T0 + 60 * DAY_MS + 5 * 3_600_000
    day_cut = cut // DAY_MS * DAY_MS
    spot2 = {s: [r if r[0] < day_cut else [*r[:6], 0.0] for r in rows] for s, rows in spot.items()}
    b = simulate_carry_delisting(mk, entry_allowed=make_entry_filter(spot_daily=spot2, **kw), **KW)

    def upto(res):
        return sorted((x["symbol"], x["open_ts"], x["close_ts"]) for x in res["trades"] if x["close_ts"] <= cut), sorted(
            (x["symbol"], x["open_ts"]) for x in res["trades"] if x["open_ts"] <= cut
        )

    assert upto(a) == upto(b) and upto(a)[1]
    assert [x for x in a["equity"] if x[0] <= cut] == [x for x in b["equity"] if x[0] <= cut]
    # once more than half of the 30-day window is zeroed, the filter blocks every new entry
    assert all(x["open_ts"] < day_cut + 17 * DAY_MS for x in b["trades"])
    assert any(x["open_ts"] > cut for x in a["trades"])


def test_random_blocked_is_deterministic():
    syms = [f"S{i}" for i in range(20)]
    a = random_blocked(syms, 5, 1)
    assert a == random_blocked(list(reversed(syms)), 5, 1) and len(set(a)) == 5
    assert random_blocked(syms, 50, 1) == sorted(syms)
