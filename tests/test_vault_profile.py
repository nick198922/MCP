"""Pure parts of scripts/research/vault_profile.py: fills → episodes, funding rows → daily positions, equity maths."""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "research"))
import vault_profile as vp  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hl_vault_cryptobred_mkr_zro_2026-10-23.json").read_text())
DAY = vp.DAY_MS
T0 = 1_760_000_000_000 // DAY * DAY  # a UTC midnight


def raw_fill(t, coin, px, sz, side, start, closed=0.0, crossed=True, oid=1):
    return {
        "time": t,
        "coin": coin,
        "px": str(px),
        "sz": str(sz),
        "side": side,
        "startPosition": str(start),
        "closedPnl": str(closed),
        "fee": "0.1",
        "crossed": crossed,
        "oid": oid,
    }


# ------------------------------------------------------------------------------------------ fills → episodes


def test_real_fills_rebuild_flat_to_flat_episodes():
    fills = vp.parse_fills(FIXTURE["fills"], vp.withdrawal_times(FIXTURE["ledger"]))
    eps = vp.build_episodes(fills)
    assert [(e.coin, e.side) for e in eps] == [("ZRO", 1), ("MKR", 1), ("MKR", 1), ("MKR", 1), ("ZRO", -1), ("MKR", 1)]
    assert all(e.t_close is not None and not e.truncated for e in eps)  # the vault was closed: every episode ends flat
    # every fill lands in exactly one episode and realized PnL adds up to the exchange's closedPnl
    assert sum(len(e.opens) + len(e.closes) for e in eps) == len(fills)
    assert sum(e.realized for e in eps) == pytest.approx(sum(f.closed_pnl for f in fills))
    last = eps[-1]  # MKR 2025-06-02 → 06-10: the vault's biggest MKR trade
    assert vp.ymd(last.t_open) == "2025-06-02" and vp.ymd(last.t_close) == "2025-06-10"
    assert last.realized == pytest.approx(25732.58, abs=0.01)
    assert last.max_size == pytest.approx(74.079, abs=1e-3)
    assert last.add_direction() == {"up": 7}  # pyramided into strength
    assert eps[2].add_direction() == {"down": 6}  # the May MKR trade averaged down
    assert sum(f.forced for f in fills) == 3  # closes stamped with a withdrawal that had a closing cost


def test_truncated_and_flipped_positions():
    raws = [
        raw_fill(T0, "X", 10, 2, "A", 5, closed=4),  # data starts with an open long of 5 → truncated episode
        raw_fill(T0 + 1000, "X", 11, 3, "A", 3, closed=3),  # closes the rest
        raw_fill(T0 + 2000, "X", 12, 2, "A", 0),  # opens a short
        raw_fill(T0 + 3000, "X", 11, 5, "B", -2, closed=2),  # flips to a long of 3
    ]
    eps = vp.build_episodes(vp.parse_fills(raws))
    assert [(e.side, e.truncated, e.t_close is None) for e in eps] == [(1, True, False), (-1, False, False), (1, False, True)]
    assert eps[0].realized == pytest.approx(7)
    assert eps[2].max_size == pytest.approx(3)


def test_forced_close_matches_withdrawal_time_only_for_reducing_fills():
    raws = [raw_fill(T0, "X", 10, 1, "B", 0), raw_fill(T0 + 500, "X", 10, 1, "B", 1), raw_fill(T0 + 900, "X", 10, 1, "A", 2)]
    fills = vp.parse_fills(raws, forced_times=[T0 + 600])
    assert [f.forced for f in fills] == [False, False, True]  # opens are never forced


def test_close_gain_from_closed_pnl():
    long_close = vp.parse_fills([raw_fill(T0, "X", 110, 2, "A", 5, closed=20)])[0]  # entry 100 → +10 %
    short_close = vp.parse_fills([raw_fill(T0, "X", 90, 2, "B", -5, closed=20)])[0]  # entry 100 → +10 %
    assert long_close.close_gain == pytest.approx(0.10)
    assert short_close.close_gain == pytest.approx(0.10)
    assert math.isnan(vp.parse_fills([raw_fill(T0, "X", 10, 1, "B", 0)])[0].close_gain)


def test_add_clusters_split_by_gap():
    raws = [raw_fill(T0 + i * 60_000, "X", 100 - i, 1, "B", i) for i in range(3)] + [raw_fill(T0 + 3 * vp.HOUR_MS, "X", 120, 1, "B", 3)]
    ep = vp.build_episodes(vp.parse_fills(raws))[0]
    assert len(ep.add_clusters()) == 2
    assert ep.add_direction() == {"up": 1}


# ------------------------------------------------------------------------------------- funding → daily book


def funding_row(t, coin, szi, usdc, n=None):
    d = {"type": "funding", "coin": coin, "szi": str(szi), "usdc": str(usdc), "fundingRate": "0.0001"}
    if n is not None:
        d["nSamples"] = n
    return {"time": t, "delta": d}


def test_daily_positions_mix_daily_aggregates_and_hourly_rows():
    rows = [funding_row(T0, "X", 10, -1.0, n=24), funding_row(T0 + DAY, "X", 20, -0.5, n=12)]
    rows += [funding_row(T0 + 2 * DAY + h * vp.HOUR_MS, "X", 6, -0.1) for h in range(6)]  # 6 hourly rows, then flat
    pos = vp.daily_positions(rows)["X"]
    assert pos[T0] == pytest.approx((10, 10, -1.0))
    assert pos[T0 + DAY] == pytest.approx((20, 10, -0.5))  # open half the day → exposure 10
    assert pos[T0 + 2 * DAY] == pytest.approx((6, 1.5, -0.6))


def test_daily_episodes_adds_trims_gaps_and_flips():
    sizes = {T0: 10, T0 + DAY: 13, T0 + 2 * DAY: 8, T0 + 4 * DAY: 5, T0 + 5 * DAY: -5}
    pos = {"X": {d: (s, s, 0.0) for d, s in sizes.items()}}
    eps = vp.daily_episodes(pos, last_day=T0 + 5 * DAY)
    assert [(e.side, len(e.days)) for e in eps] == [(1, 3), (1, 1), (-1, 1)]  # gap on day 3, flip on day 5
    assert eps[0].adds == [T0 + DAY] and eps[0].trims == [T0 + 2 * DAY]
    assert [e.open_at_end for e in eps] == [False, False, True]


def test_real_funding_rows_give_daily_episodes():
    pos = vp.daily_positions(FIXTURE["funding"])
    eps = vp.daily_episodes(pos, max(max(v) for v in pos.values()))
    assert [(e.coin, e.side, vp.ymd(e.start), len(e.days)) for e in eps] == [
        ("ZRO", 1, "2025-04-21", 2),
        ("MKR", 1, "2025-05-05", 5),
        ("MKR", 1, "2025-05-19", 2),
        ("ZRO", -1, "2025-05-20", 2),
        ("MKR", 1, "2025-06-02", 9),
    ]


def test_daily_book_and_episode_pnl():
    pos = {"X": {T0: (2.0, 1.0, -0.5)}, "Y": {T0: (-1.0, -1.0, 0.2)}}
    candles = {"X": {T0: (100, 120, 90, 110)}, "Y": {T0: (50, 55, 45, 40)}}
    b = vp.daily_book(pos, candles)[T0]
    assert b["long"] == pytest.approx(220) and b["short"] == pytest.approx(40) and b["gross"] == pytest.approx(260)
    assert b["pnl"] == pytest.approx(1.0 * 10 - 0.5 + (-1.0) * (-10) + 0.2)
    ep = vp.daily_episodes({"X": pos["X"]}, T0)[0]
    assert vp.episode_pnl(ep, pos, candles)["pnl"] == pytest.approx(9.5)
    assert math.isnan(vp.episode_pnl(ep, pos, {})["pnl"])  # no candles → unknown, not zero


# ------------------------------------------------------------------------------------------- equity maths


def test_max_drawdown_and_twr_ignore_flows():
    assert vp.max_drawdown([1, 2, 1, 3, 1.5]) == (pytest.approx(0.5), 1, 2)
    av = [(0, 100.0), (1, 210.0), (2, 231.0)]  # +100 deposit in the first interval, then +10 %
    pnl = [(0, 0.0), (1, 10.0), (2, 31.0)]
    nav = vp.twr_curve(av, pnl)
    assert nav[-1][1] == pytest.approx(1.1 * 1.1)


def test_flows_and_daily_equity():
    ledger = [
        {"time": T0, "delta": {"type": "vaultCreate", "usdc": "100"}},
        {"time": T0 + DAY, "delta": {"type": "vaultDeposit", "usdc": "50"}},
        {
            "time": T0 + 2 * DAY,
            "delta": {"type": "vaultWithdraw", "user": "0xLEAD", "requestedUsd": "22", "netWithdrawnUsd": "20", "commission": "2"},
        },
        {"time": T0 + 3 * DAY, "delta": {"type": "vaultDistribution", "usdc": "140"}},
    ]
    fl = vp.flows(ledger, leader="0xlead")
    assert (fl["deposits"], fl["withdrawals"], fl["commission"], fl["leader_withdrawals"]) == (150, 160, 2, 20)
    days = [T0 + i * DAY for i in range(3)]
    eq = vp.daily_equity(vp.daily_flows(ledger), [(T0, 0.0), (T0 + 2 * DAY, 10.0)], days)
    assert eq[T0] == pytest.approx(50)  # half of the day's deposit counts as working capital
    assert eq[T0 + DAY] == pytest.approx(100 + 5 + 25)
    assert eq[T0 + 2 * DAY] == pytest.approx(150 + 10 - 10)


def test_trim_after_close_drops_empty_tail():
    av = [(0, 0.0), (1, 100.0), (2, 150.0), (3, 0.0), (4, 0.0)]
    pnl = [(0, 0.0), (1, 0.0), (2, 50.0), (3, 60.0), (4, 60.0)]
    a, p = vp.trim_after_close(av, pnl)
    assert [t for t, _ in a] == [0, 1, 2, 3] and len(p) == 4


def test_ols_recovers_beta_and_alpha():
    x = [0.01 * ((i * 7) % 11 - 5) for i in range(50)]
    y = [0.002 + 3 * v for v in x]
    r = vp.ols(y, [x])
    assert r["coef"][0] == pytest.approx(0.002) and r["coef"][1] == pytest.approx(3) and r["r2"] == pytest.approx(1)
