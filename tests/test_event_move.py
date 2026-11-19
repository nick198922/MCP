"""deribit_event_move and analytics/event_move.py (item 413 of docs/ideas/IDEA-20261102-standing-improvements.md)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from conftest import EXPIRIES, NOW, SPOT, FakeEx, State, make_transports
from test_server import call
from tradedesk.analytics.event_move import forward_variance, implied_event_move
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


async def test_tool_deribit_down_goes_to_missing(server, state):
    state.deribit_status = 503
    env = await call(server, "deribit_event_move", event_ts="2026-11-05T19:00:00Z")
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == "deribit" and m["reason"] in ("backend_error", "network") for m in env["missing"])
