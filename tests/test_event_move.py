"""deribit_event_move and analytics/event_move.py (item 413 of docs/ideas/IDEA-20261102-standing-improvements.md)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from conftest import EXPIRIES, NOW, SPOT, FakeEx, State, make_transports
from test_server import call
from tradedesk.analytics.event_move import (
    base_forward_iv,
    base_window_is_short,
    events_in_event_window,
    excess_event_move,
    forward_variance,
    implied_event_move,
)
from tradedesk.analytics.options import black76_price
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

T0 = datetime(2026, 11, 1, 8, tzinfo=UTC)
E10, E17, E24 = (T0 + timedelta(days=d) for d in (10, 17, 24))


# --- pure function -----------------------------------------------------------------------------------------------


def test_analytic_reference_unequal_steps():
    # sigma1 = 0.40 over 10 days, sigma2 = 0.50 over 17 days: total variances 1.6 / 365 and 4.25 / 365
    res = implied_event_move([(E17, 0.50), (E10, 0.40), (E24, 0.55)], T0 + timedelta(days=12, hours=11), T0)
    assert res["ok"] is True and not res["calendar_arbitrage"]
    assert res["before"]["expiry"] == E10 and res["after"]["expiry"] == E17
    assert res["window_days"] == pytest.approx(7.0)
    assert res["event_var"] == pytest.approx((4.25 - 1.6) / 365)
    assert res["forward_var"] == pytest.approx((4.25 - 1.6) / 7)
    assert res["forward_iv"] == pytest.approx(math.sqrt(2.65 / 7))  # 0.6153
    assert res["event_move"] == pytest.approx(math.sqrt(2.65 / 365))  # 0.0852 = 8.52 % 1-sigma
    assert res["event_share"] == pytest.approx(2.65 / 4.25)


def test_flat_vol_gives_the_same_forward_vol_and_sqrt_t_move():
    res = implied_event_move([(E10, 0.5), (E24, 0.5)], E17, T0)
    assert res["forward_iv"] == pytest.approx(0.5)
    assert res["event_move"] == pytest.approx(0.5 * math.sqrt(14 / 365))
    assert res["window_days"] == pytest.approx(14.0)


def test_unequal_t_one_day_window():
    e1, e2 = T0 + timedelta(days=1), T0 + timedelta(days=2)
    res = implied_event_move([(e1, 0.45), (e2, 0.60)], T0 + timedelta(days=1, hours=11), T0)
    w1, w2 = 0.45**2 / 365, 0.60**2 * 2 / 365
    assert res["event_var"] == pytest.approx(w2 - w1)
    assert res["forward_iv"] == pytest.approx(math.sqrt((w2 - w1) * 365))
    assert res["event_move"] == pytest.approx(math.sqrt(w2 - w1))


def test_event_before_first_or_after_last_expiry_or_in_the_past():
    exps = [(E10, 0.4), (E17, 0.5)]
    before = implied_event_move(exps, E10 - timedelta(hours=1), T0)
    assert before["ok"] is False and "first listed expiry" in before["reason"]
    assert implied_event_move(exps, E10, T0)["ok"] is False  # exactly at the first expiry: nothing earlier to difference
    after = implied_event_move(exps, E17 + timedelta(seconds=1), T0)
    assert after["ok"] is False and "after the last listed expiry" in after["reason"]
    past = implied_event_move(exps, T0 - timedelta(days=1), T0)
    assert past["ok"] is False and "not after now" in past["reason"]
    assert implied_event_move([], E17, T0) == {"ok": False, "reason": "no live expiry with an ATM IV"}


def test_event_at_an_expiry_belongs_to_the_window_ending_there():
    res = implied_event_move([(E10, 0.4), (E17, 0.5), (E24, 0.6)], E17, T0)
    assert res["before"]["expiry"] == E10 and res["after"]["expiry"] == E17


def test_calendar_arbitrage_gives_no_square_root_of_a_negative():
    e11 = T0 + timedelta(days=11)
    res = implied_event_move([(E10, 0.60), (e11, 0.40)], E10 + timedelta(hours=12), T0)
    assert res["ok"] is True and res["calendar_arbitrage"] is True
    assert res["event_var"] < 0 and res["forward_var"] < 0
    assert res["forward_iv"] is None and res["event_move"] is None


def test_expired_and_unusable_expiries_are_ignored():
    exps = [(T0 - timedelta(days=1), 0.3), (E10, math.nan), (E17, 0.5), (E24, math.inf), (T0 + timedelta(days=30), 0.0)]
    assert implied_event_move(exps, E24, T0)["ok"] is False  # only E17 is usable
    res = implied_event_move([*exps, (E10 - timedelta(days=5), 0.4)], E17, T0)
    assert res["before"]["expiry"] == E10 - timedelta(days=5) and res["after"]["expiry"] == E17


def test_forward_variance_rejects_bad_inputs():
    assert forward_variance(0.4, 0.0, 0.5, 1.0) == pytest.approx(0.25)
    for args in ((0.4, 1.0, 0.5, 1.0), (0.4, 2.0, 0.5, 1.0), (0.4, -1.0, 0.5, 1.0), (math.nan, 0.1, 0.5, 1.0), (0.4, 0.1, 0.0, 1.0)):
        with pytest.raises(ValueError):
            forward_variance(*args)


# --- base diffusion and the excess (item 414) ---------------------------------------------------------------------

E3 = T0 + timedelta(days=3)
EV12 = T0 + timedelta(days=12)  # inside (E10, E17]
# sigma 0.40 / 3 d, 0.42 / 10 d, 0.55 / 17 d, 0.50 / 24 d; T = days / 365, so every w = sigma^2 x days / 365
LADDER = [(E3, 0.40), (E10, 0.42), (E17, 0.55), (E24, 0.50)]
EVENT_VAR = (0.55**2 * 17 - 0.42**2 * 10) / 365  # 3.3785 / 365
BEFORE_FWD = (0.42**2 * 10 - 0.40**2 * 3) / 7  # (1.764 - 0.48) / 7 = 0.183429 (annualised)
AFTER_FWD = (0.50**2 * 24 - 0.55**2 * 17) / 7  # (6 - 5.1425) / 7 = 0.1225 = 0.35^2


def test_base_forward_iv_analytic_reference_and_tie_goes_to_the_earlier_window():
    base = base_forward_iv(LADDER, EV12, T0)
    assert base["ok"] is True and base["rejected"] == []
    assert base["side"] == "before" and base["start"] == E3 and base["end"] == E10  # both 7 d long: the earlier one
    assert base["window_days"] == pytest.approx(7.0)
    assert base["forward_var"] == pytest.approx(BEFORE_FWD)
    assert base["forward_iv"] == pytest.approx(math.sqrt(1.284 / 7))  # 0.4283
    ex = excess_event_move(EVENT_VAR, base["forward_var"], 10 / 365, 17 / 365)
    assert ex["base_var"] == pytest.approx(1.284 / 365)
    assert ex["excess_var"] == pytest.approx((3.3785 - 1.284) / 365)
    assert ex["excess_move"] == pytest.approx(math.sqrt(2.0945 / 365))  # 0.0758 = 7.58 % 1-sigma, vs 9.62 % for the window
    assert ex["below_base"] is False
    assert ex["excess_move"] < implied_event_move(LADDER, EV12, T0)["event_move"]


def test_base_window_with_another_event_falls_back_to_the_other_side():
    base = base_forward_iv(LADDER, EV12, T0, [T0 + timedelta(days=5)])  # inside (E3, E10]
    assert base["ok"] is True and base["side"] == "after" and base["start"] == E17 and base["end"] == E24
    assert base["forward_iv"] == pytest.approx(0.35)
    assert len(base["rejected"]) == 1 and "before" in base["rejected"][0] and "another event" in base["rejected"][0]
    ex = excess_event_move(EVENT_VAR, base["forward_var"], 10 / 365, 17 / 365)
    assert ex["excess_move"] == pytest.approx(math.sqrt((3.3785 - 0.8575) / 365))
    # an event exactly at E10 belongs to (E3, E10], like the event window (E1, E2]; one at E3 does not
    assert base_forward_iv(LADDER, EV12, T0, [E10])["side"] == "after"
    assert base_forward_iv(LADDER, EV12, T0, [E3])["side"] == "before"


def test_both_neighbours_with_events_or_absent_give_no_base():
    both = base_forward_iv(LADDER, EV12, T0, [T0 + timedelta(days=5), T0 + timedelta(days=20)])
    assert both["ok"] is False and both["reason"].count("another event") == 2
    absent = base_forward_iv([(E10, 0.42), (E17, 0.55)], EV12, T0)
    assert absent["ok"] is False
    assert "before: no earlier expiry" in absent["reason"] and "after: no later expiry" in absent["reason"]
    one_side = base_forward_iv([(E10, 0.42), (E17, 0.55), (E24, 0.50)], EV12, T0)  # no E0: the after window only
    assert one_side["ok"] is True and one_side["side"] == "after" and "no earlier expiry" in one_side["rejected"][0]
    # no event window at all: the same reason implied_event_move gives
    assert base_forward_iv(LADDER, E24 + timedelta(days=1), T0) == {
        "ok": False,
        "reason": implied_event_move(LADDER, E24 + timedelta(days=1), T0)["reason"],
    }


def test_neighbour_with_calendar_arbitrage_is_not_a_base():
    ladder = [(E3, 0.80), (E10, 0.42), (E17, 0.55), (E24, 0.50)]  # 0.42^2 x 10 = 1.764 < 0.80^2 x 3 = 1.92
    base = base_forward_iv(ladder, EV12, T0)
    assert base["side"] == "after" and "non-positive forward variance" in base["rejected"][0]


def test_unequal_t_base_window_length_closest_to_the_event_window():
    e5, e9, e31 = (T0 + timedelta(days=d) for d in (5, 9, 31))
    # event window 7 d; before 5 d (off by 2), after 7 d (off by 0): the after window
    assert base_forward_iv([(e5, 0.40), (E10, 0.42), (E17, 0.55), (E24, 0.50)], EV12, T0)["side"] == "after"
    # before 1 d (off by 6), after 14 d (off by 7): the before window, whose 1-day forward variance scales to 7 days
    ladder = [(e9, 0.41), (E10, 0.42), (E17, 0.55), (e31, 0.50)]
    base = base_forward_iv(ladder, EV12, T0)
    fwd = (0.42**2 * 10 - 0.41**2 * 9) / 1  # 1.764 - 1.5129 = 0.2511 (annualised)
    assert base["side"] == "before" and base["window_days"] == pytest.approx(1.0)
    assert base["forward_var"] == pytest.approx(fwd)
    ex = excess_event_move(EVENT_VAR, fwd, 10 / 365, 17 / 365)
    assert ex["base_var"] == pytest.approx(fwd * 7 / 365)
    assert ex["excess_move"] == pytest.approx(math.sqrt((3.3785 - 0.2511 * 7) / 365))


def test_excess_below_the_base_is_zero():
    ex = excess_event_move(0.001, 0.25, 10 / 365, 17 / 365)  # base var 0.25 x 7 / 365 = 0.004795 > 0.001
    assert ex["excess_move"] == 0.0 and ex["below_base"] is True
    assert ex["excess_var"] == pytest.approx(0.001 - 0.25 * 7 / 365)
    exact = excess_event_move(0.25 * 7 / 365, 0.25, 10 / 365, 17 / 365)
    assert exact["excess_move"] == pytest.approx(0.0, abs=1e-6) and exact["excess_var"] == pytest.approx(0.0, abs=1e-12)


def test_excess_event_move_rejects_bad_inputs():
    for args in (
        (math.nan, 0.2, 0.1, 0.2),
        (0.01, 0.0, 0.1, 0.2),
        (0.01, -0.1, 0.1, 0.2),
        (0.01, 0.2, 0.2, 0.2),
        (0.01, 0.2, -0.1, 0.2),
        (0.01, math.inf, 0.1, 0.2),
    ):
        with pytest.raises(ValueError):
            excess_event_move(*args)


# --- caveats on the excess (item 424) -----------------------------------------------------------------------------


def test_events_in_event_window_boundaries_and_the_event_itself():
    inside, later = T0 + timedelta(days=15), T0 + timedelta(days=20)
    assert events_in_event_window([later, inside, E17], EV12, E10, E17) == [inside, E17]  # E2 itself is inside (E1, E2]
    assert events_in_event_window([E10, later, E3], EV12, E10, E17) == []  # E1 is not inside; outside events ignored
    assert events_in_event_window([EV12], EV12, E10, E17) == []  # the event itself is not "another event"
    assert events_in_event_window([], EV12, E10, E17) == []


def test_events_in_event_window_returns_a_duplicated_time_once():
    inside = T0 + timedelta(days=15)
    same_instant = inside.astimezone(timezone(timedelta(hours=2)))  # equal datetime, other tz: still one event
    assert events_in_event_window([inside, E17, inside, same_instant, E17], EV12, E10, E17) == [inside, E17]


def test_base_window_is_short_thresholds():
    assert base_window_is_short(0.5, 0.75) is True  # under 1 day
    assert base_window_is_short(3.0, 7.0) is True  # under half the event window (3.5)
    assert base_window_is_short(3.5, 7.0) is False  # exactly half: not short
    assert base_window_is_short(1.0, 2.0) is False  # exactly 1 day and half: not short
    assert base_window_is_short(7.0, 7.0) is False and base_window_is_short(119.0, 28.0) is False


async def test_tool_other_event_inside_the_event_window_warns():
    srv = _server({OCT: 0.40, NOV: 0.55, MAR: 0.50})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", other_events=["2026-11-18T19:00:00Z"])
    assert env["ok"] is True and env["data"]["excess_event_move_pct"] is not None
    w = next(w for w in env["warnings"] if w.startswith("another event inside the event window"))
    assert "(30OCT26, 27NOV26]" in w and "2026-11-18T19:00:00+00:00" in w and "attributed to the main event" in w


async def test_tool_duplicated_other_event_is_printed_once():
    srv = _server({OCT: 0.40, NOV: 0.55, MAR: 0.50})
    dup = ["2026-11-18T19:00:00Z", "2026-11-18T19:00:00Z"]
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", other_events=dup)
    assert env["ok"] is True and env["data"]["excess_event_move_pct"] is not None
    w = next(w for w in env["warnings"] if w.startswith("another event inside the event window"))
    assert w.count("2026-11-18T19:00:00+00:00") == 1


@pytest.mark.parametrize(
    "other",
    [
        ["2026-10-30T08:00:00Z"],  # exactly E1: not inside (E1, E2]
        ["2026-11-05T19:00:00Z"],  # the event itself
        ["2026-10-20T12:00:00Z", "2027-04-01T12:00:00Z"],  # outside every window used
    ],
)
async def test_tool_other_event_outside_or_on_the_boundary_does_not_warn(other):
    srv = _server({OCT: 0.40, NOV: 0.55, MAR: 0.50})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", other_events=other)
    assert env["ok"] is True and env["data"]["excess_event_move_pct"] is not None
    assert not any("another event inside the event window" in w for w in env["warnings"])
    # the base window (27NOV26, 26MAR27] is 119 days, longer than the 28-day event window: no short-base warning
    assert not any("base window is short" in w for w in env["warnings"])


async def test_tool_short_base_window_warns():
    # event in (27NOV26, 26MAR27] (119 d); the only base is (30OCT26, 27NOV26] (28 d), under half of it
    srv = _server({OCT: 0.40, NOV: 0.45, MAR: 0.60})
    env = await call(srv, "deribit_event_move", event_ts="2026-12-16T19:00:00Z")
    assert env["ok"] is True, env["missing"]
    d = env["data"]
    assert d["base_window"]["side"] == "before" and d["excess_event_move_pct"] is not None
    w = next(w for w in env["warnings"] if w.startswith("the base window is short"))
    assert "28.00 days" in w and "119.00" in w and "badly biased" in w


# --- the tool ------------------------------------------------------------------------------------------------------


def _flat_book(vols: dict[datetime, float], thin: tuple[datetime, ...] = ()) -> list[dict]:
    """The conftest option book with a flat smile per expiry (ATM IV = the given vol exactly).

    ``thin``: extra expiries (IV taken from ``vols``) listed with only two strikes, too few for a smile."""
    out = []
    for exp, fwd_mult in [*EXPIRIES, *((e, 1.0) for e in thin)]:
        F, iv = SPOT * fwd_mult, vols[exp]
        T = (exp - NOW).total_seconds() / (365 * 86400)
        tok = f"{exp.day}{exp.strftime('%b').upper()}{exp.year % 100:02d}"
        for k in (95_000, 100_000) if exp in thin else range(50_000, 130_001, 5_000):
            for is_call in (True, False):
                out.append(
                    {
                        "instrument_name": f"BTC-{tok}-{k}-{'C' if is_call else 'P'}",
                        "mark_iv": iv * 100,
                        "mark_price": black76_price(F, k, T, iv, is_call) / F,
                        "underlying_price": F,
                        "estimated_delivery_price": SPOT,
                        "open_interest": 10.0,
                        "volume": 1.0,
                        "base_currency": "BTC",
                        "quote_currency": "BTC",
                    }
                )
    return out


def _server(vols: dict[datetime, float], thin: tuple[datetime, ...] = ()):
    tr = make_transports(State())

    def deribit(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("get_book_summary_by_currency") and req.url.params.get("kind") == "option"
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _flat_book(vols, thin)})

    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


OCT, NOV, MAR = (e for e, _ in EXPIRIES)  # 30OCT26, 27NOV26, 26MAR27 08:00 UTC; NOW = 2026-10-10 13:07 UTC


async def test_tool_happy_path_matches_the_analytic_value():
    srv = _server({OCT: 0.40, NOV: 0.55, MAR: 0.50})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z")
    assert env["ok"] is True, env["missing"]
    d = env["data"]
    assert d["event_utc"] == "2026-11-05T19:00:00+00:00"
    assert d["expiry_before"]["expiry"] == "30OCT26" and d["expiry_after"]["expiry"] == "27NOV26"
    assert d["expiry_before"]["atm_iv"] == pytest.approx(0.40) and d["expiry_after"]["atm_iv"] == pytest.approx(0.55)
    t1, t2 = ((e - NOW).total_seconds() / (365 * 86400) for e in (OCT, NOV))
    ev = 0.55**2 * t2 - 0.40**2 * t1
    assert d["window_days"] == pytest.approx(28.0)
    assert d["forward_iv"] == pytest.approx(math.sqrt(ev / (t2 - t1)), abs=1e-4)
    assert d["event_move_pct"] == pytest.approx(math.sqrt(ev) * 100, abs=1e-3)
    assert d["event_move_usd"] == pytest.approx(math.sqrt(ev) * d["expiry_after"]["forward"], abs=0.02)
    assert d["calendar_arbitrage"] is False
    vs = d["vs_expected_move"]
    assert vs["expiry"] == "27NOV26" and vs["sigma_move_pct"] == pytest.approx(0.55 * math.sqrt(t2) * 100, abs=1e-3)
    assert vs["event_share_of_variance"] == pytest.approx(ev / (0.55**2 * t2), abs=1e-4)
    assert 0 < vs["straddle_pct"] < vs["sigma_move_pct"]  # straddle ~ sqrt(2/pi) x sigma move
    assert any(e["provider"] == "deribit" for e in env["evidence"])
    assert any("upper bound" in w for w in env["warnings"]) and any("Risk-neutral" in w for w in env["warnings"])
    # review 19.11: the move is the whole window's (event plus diffusion), an upper bound even for a one-day window
    bound = next(w for w in env["warnings"] if "upper bound" in w)
    assert "whole 28.00-day window" in bound and "even for a one-day window" in bound and "close to" not in bound
    # the same numbers deribit_expected_move gives for that expiry
    em = await call(srv, "deribit_expected_move", expiry="27NOV26")
    assert em["data"]["expiries"][0]["straddle_pct"] == vs["straddle_pct"]
    # item 414: no expiry before 30OCT26, so the base diffusion is the forward vol of (27NOV26, 26MAR27]
    t3 = (MAR - NOW).total_seconds() / (365 * 86400)
    base_fwd = (0.50**2 * t3 - 0.55**2 * t2) / (t3 - t2)
    assert d["base_window"] == {"side": "after", "from": "27NOV26", "to": "26MAR27", "days": pytest.approx(119.0)}
    assert d["base_forward_iv"] == pytest.approx(math.sqrt(base_fwd), abs=1e-4)  # ~0.478
    excess = math.sqrt(ev - base_fwd * (t2 - t1))
    assert d["excess_event_move_pct"] == pytest.approx(excess * 100, abs=1e-3)  # ~11.6 %, below the window's 13.6 %
    assert d["excess_event_move_usd"] == pytest.approx(excess * d["expiry_after"]["forward"], abs=0.02)
    assert 0 < d["excess_event_move_pct"] < d["event_move_pct"]
    assert any("excess over the base diffusion" in w and "estimate" in w for w in env["warnings"])
    assert not any("no base forward vol" in w for w in env["warnings"])


async def test_tool_neighbour_window_with_another_event_gives_no_excess():
    srv = _server({OCT: 0.40, NOV: 0.55, MAR: 0.50})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", other_events=["2026-12-16T19:00:00Z"])
    assert env["ok"] is True, env["missing"]
    d = env["data"]
    assert d["base_forward_iv"] is None and d["base_window"] is None
    assert d["excess_event_move_pct"] is None and d["excess_event_move_usd"] is None
    assert d["event_move_pct"] is not None  # the window's own numbers are still there
    w = next(w for w in env["warnings"] if w.startswith("no base forward vol"))
    assert "before: no earlier expiry" in w and "after: window" in w and "another event (2026-12-16T19:00:00+00:00)" in w


async def test_tool_excess_below_the_base_is_zero_with_a_warning():
    # base (27NOV26, 26MAR27] forward vol ~0.65, far above the event window's ~0.43: no event premium
    srv = _server({OCT: 0.40, NOV: 0.42, MAR: 0.60})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z")
    assert env["ok"] is True, env["missing"]
    d = env["data"]
    assert d["base_forward_iv"] > d["forward_iv"]
    assert d["excess_event_move_pct"] == 0.0 and d["excess_event_move_usd"] == 0.0
    assert any("not above the base diffusion" in w and "27NOV26..26MAR27" in w for w in env["warnings"])


async def test_tool_bad_other_event_is_invalid_input_without_a_request(server, state):
    state.deribit_status = 503  # a request would show up as a deribit miss
    env = await call(server, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", other_events=["2026-12-16", "soon"])
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("other_events", "invalid_input")]


async def test_tool_skips_a_thin_expiry_between_the_pair_and_says_so():
    mid = datetime(2026, 11, 13, 8, tzinfo=UTC)  # 13NOV26, between 30OCT26 and 27NOV26, only two strikes
    srv = _server({OCT: 0.40, mid: 0.45, NOV: 0.55, MAR: 0.50}, thin=(mid,))
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z")
    assert env["ok"] is True, env["missing"]
    assert env["data"]["expiry_before"]["expiry"] == "30OCT26" and env["data"]["expiry_after"]["expiry"] == "27NOV26"
    assert any(w.startswith("expiries between the pair skipped") and "13NOV26" in w for w in env["warnings"])


async def test_tool_on_the_conftest_book(server):
    env = await call(server, "deribit_event_move", currency="BTC", event_ts="2026-12-16T19:00:00+00:00")
    assert env["ok"] is True
    assert env["data"]["expiry_before"]["expiry"] == "27NOV26" and env["data"]["expiry_after"]["expiry"] == "26MAR27"
    assert 0.3 < env["data"]["forward_iv"] < 0.5  # the fake smile is the same at every expiry's forward (~0.40)


async def test_tool_calendar_arbitrage_warns_without_a_move():
    srv = _server({OCT: 0.90, NOV: 0.30, MAR: 0.50})
    env = await call(srv, "deribit_event_move", event_ts="2026-11-05T19:00:00Z")
    assert env["ok"] is True and env["data"]["calendar_arbitrage"] is True
    assert env["data"]["event_move_pct"] is None and env["data"]["forward_iv"] is None
    assert env["data"]["forward_variance"] < 0
    assert any("calendar arbitrage" in w for w in env["warnings"])
    assert any(m["field"] == "event_move" for m in env["missing"])


@pytest.mark.parametrize(
    ("event_ts", "reason", "word"),
    [
        ("2026-10-20T12:00:00Z", "data_unavailable", "first listed expiry"),  # before 30OCT26
        ("2027-04-01T12:00:00Z", "data_unavailable", "after the last listed expiry"),
        ("2026-10-01T12:00:00Z", "invalid_input", "not in the future"),
        ("next FOMC", "invalid_input", "ISO-8601"),
        ("", "invalid_input", "ISO-8601"),
    ],
)
async def test_tool_event_out_of_range_or_bad_is_ok_false(server, event_ts, reason, word):
    env = await call(server, "deribit_event_move", event_ts=event_ts)
    assert env["ok"] is False and env["data"] is None
    assert any(m["reason"] == reason and word in m["detail"] for m in env["missing"]), env["missing"]


async def test_tool_naive_time_is_utc(server):
    env = await call(server, "deribit_event_move", event_ts="2026-11-05T19:00")
    assert env["ok"] is True and env["data"]["event_utc"] == "2026-11-05T19:00:00+00:00"


@pytest.mark.parametrize("extra", [{}, {"other_events": ["2026-12-16T19:00:00Z"]}])
async def test_tool_deribit_down_goes_to_missing(server, state, extra):
    state.deribit_status = 503
    env = await call(server, "deribit_event_move", event_ts="2026-11-05T19:00:00Z", **extra)
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == "deribit" and m["reason"] in ("backend_error", "network") for m in env["missing"])
