from __future__ import annotations

import math

from tradedesk.analytics.mm_onchain import Bar, Transfer
from tradedesk.analytics.mm_twohop import (
    classify_receiver,
    day_end,
    exchange_bound,
    funding_short,
    known_since,
    net_daily,
    placebo_days,
    short_net,
)

MM = {"0xmm"}
EX = {"0xbinance14"}
R = "0xdep"
T0 = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def tr(ts: int, src: str, dst: str, contract: str = "0xtok", amt: float = 1.0) -> Transfer:
    return Transfer(ts, src, dst, contract, "TOK", amt)


def test_sweep_within_window_is_deposit() -> None:
    rows = [tr(T0, "0xmm", R), tr(T0 + 3600, R, "0xbinance14")]
    v = classify_receiver(R, rows, MM, EX)
    assert v.is_deposit and v.n_sweeps == 1 and v.first_sweep_ts == T0 + 3600 and v.ex_share == 1.0


def test_sweep_after_window_or_other_token_is_not() -> None:
    late = [tr(T0, "0xmm", R), tr(T0 + 86_401, R, "0xbinance14")]
    other = [tr(T0, "0xmm", R), tr(T0 + 60, R, "0xbinance14", contract="0xother")]
    before = [tr(T0, R, "0xbinance14"), tr(T0 + 60, "0xmm", R)]
    for rows in (late, other, before):
        assert not classify_receiver(R, rows, MM, EX).is_deposit
    # exactly at the window edge counts
    assert classify_receiver(R, [tr(T0, "0xmm", R), tr(T0 + 86_400, R, "0xbinance14")], MM, EX).is_deposit


def test_incoming_not_from_mm_does_not_make_a_sweep() -> None:
    rows = [tr(T0, "0xsomeone", R), tr(T0 + 60, R, "0xbinance14")]
    assert not classify_receiver(R, rows, MM, EX).is_deposit


def test_share_of_outgoing_to_exchanges() -> None:
    # 1 sweep, but 4 of 5 outgoing rows go to exchanges = 0.8 -> deposit; 4 of 6 -> not
    base = [tr(T0, "0xmm", R)] + [tr(T0 + 60 * i, R, "0xbinance14") for i in range(1, 5)]
    assert classify_receiver(R, [*base, tr(T0 + 999, R, "0xdex")], MM, EX).is_deposit
    v = classify_receiver(R, [*base, tr(T0 + 999, R, "0xdex"), tr(T0 + 1000, R, "0xdex")], MM, EX)
    assert not v.is_deposit and math.isclose(v.ex_share, 4 / 6)


def test_known_since_is_point_in_time() -> None:
    rows = [tr(T0, "0xmm", R), tr(T0 + 3600, R, "0xbinance14")]
    assert known_since(R, rows, MM, EX) == T0 + 3601
    # rows after the returned time cannot change it (even ones that would break the address-level share)
    later = [*rows, *(tr(T0 + 10 * DAY_S + i, R, "0xdex") for i in range(10))]
    assert known_since(R, later, MM, EX) == T0 + 3601
    assert not classify_receiver(R, later, MM, EX).is_deposit
    # classification with ``until`` ignores rows at or after it
    assert classify_receiver(R, later, MM, EX, until=T0 + 10 * DAY_S).is_deposit


DAY_S = 86_400


def test_exchange_bound_address_level_and_pit() -> None:
    t_day = tr(T0, "0xmm", R)  # 2023-11-14
    direct = tr(T0, "0xmm", "0xbinance14")
    other = tr(T0, "0xmm", "0xdex")
    assert exchange_bound([t_day, direct, other], EX, {R}) == [t_day, direct]
    end = day_end("2023-11-14")
    assert end == 1_700_006_400  # 2023-11-15 00:00 UTC
    assert exchange_bound([t_day], EX, {R: end}, point_in_time=True) == [t_day]
    assert exchange_bound([t_day], EX, {R: end + 1}, point_in_time=True) == []
    assert exchange_bound([t_day], EX, {R: None}, point_in_time=True) == []


def test_net_daily() -> None:
    g = {("TOK", "2024-01-01"): 10.0, ("TOK", "2024-01-02"): 3.0}
    w = {("TOK", "2024-01-01"): 4.0, ("TOK", "2024-01-02"): 5.0, ("TOK", "2024-01-03"): 9.0}
    assert net_daily(g, w) == {("TOK", "2024-01-01"): 6.0}


def test_short_net_and_funding_window() -> None:
    bars = {"2024-01-02": Bar(100.0, 101.0, 1e6), "2024-01-04": Bar(98.0, 90.0, 1e6)}
    e = day_end("2024-01-01") * 1000
    x = day_end("2024-01-04") * 1000
    # settlements stamped +12 ms: entry 00:00 one excluded, 08:00 and 16:00 included ..., exit 00:00 excluded
    fund = [(e + 12, 0.5), (e + 8 * 3_600_000 + 12, 0.001), (x - 8 * 3_600_000 + 12, 0.002), (x + 12, 0.5)]
    assert math.isclose(funding_short(fund, e, x), 0.003)
    net, gross, f = short_net(bars, fund, "2024-01-01", 3, 0.0041)  # type: ignore[misc]
    assert math.isclose(gross, 0.10) and math.isclose(f, 0.003) and math.isclose(net, 0.10 - 0.0041 + 0.003)
    assert short_net(bars, fund, "2024-01-01", 2, 0.0041) is None


def test_funding_window_exactly_on_the_hour() -> None:
    # settlements stamped exactly on the hour: entry 00:00 and exit 00:00 both excluded, the ones between included
    e = day_end("2024-01-01") * 1000
    x = day_end("2024-01-04") * 1000
    fund = [(e, 0.5), (e + 8 * 3_600_000, 0.001), (x - 8 * 3_600_000, 0.002), (x, 0.5), (x + 8 * 3_600_000, 0.5)]
    assert math.isclose(funding_short(fund, e, x), 0.003)


def test_short_net_ignores_later_bars() -> None:
    bars = {"2024-01-02": Bar(100.0, 101.0, 1e6), "2024-01-04": Bar(98.0, 90.0, 1e6)}
    a = short_net(bars, [], "2024-01-01", 3, 0.0)
    bars2 = {**bars, "2024-01-05": Bar(1.0, 1.0, 1.0)}
    assert a == short_net(bars2, [(day_end("2024-01-05") * 1000 + 5, 9.0)], "2024-01-01", 3, 0.0)


def test_cluster_boot() -> None:
    from tradedesk.analytics.mm_twohop import cluster_boot

    m, lo, hi, p = cluster_boot([0.01] * 6, ["a", "a", "b", "b", "c", "c"], n_boot=500)
    assert math.isclose(m, 0.01) and math.isclose(lo, 0.01) and math.isclose(hi, 0.01)
    assert math.isclose(p, 1 / 501)
    m, lo, hi, p = cluster_boot([1.0, -1.0, 3.0, -3.0], ["a", "b", "c", "d"], n_boot=2000)
    assert m == 0.0 and lo < 0 < hi and 0.3 < p < 0.7


def test_placebo_days_skip_events() -> None:
    got = placebo_days("2024-03-01", ["2024-03-01", "2024-02-10"], [7, 8, 20, 21, 30, 200, 3])
    # k=7 (02-23) is exactly 7 days from the event 03-01 -> dropped (needs > 7); k=8 (02-22) ok;
    # 02-10 (k=20) and 02-09 (k=21) are within 7 days of 02-10 -> dropped; 01-31 (k=30) ok; k outside [7, 120] dropped
    assert got == ["2024-02-22", "2024-01-31"]
