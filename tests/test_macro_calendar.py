"""macro_calendar: ForexFactory weekly feed filter, UTC/Kyiv times, seed merge, provider failure.

The feed fixture is a hand-built sample (see its "_note"), not a real capture.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from conftest import NOW, FakeEx, make_transports
from test_server import call
from tradedesk.analytics.macro_calendar import build_macro_calendar, classify, clean_text
from tradedesk.collector.assistant_paper import EVENT_KINDS
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "forexfactory_calendar_sample.json").read_text())
FEED = FIX["thisweek"] + FIX["nextweek"]
SEED = json.loads((Path(__file__).parents[1] / "src" / "tradedesk" / "data" / "events_seed.json").read_text())["events"]


def test_classify():
    assert classify("Core PCE Price Index m/m", "USD") == "core_pce"
    assert classify("CPI m/m", "USD") == "cpi"
    assert classify("Non-Farm Employment Change", "USD") == "nfp"
    assert classify("Unemployment Claims", "USD") == "jobless_claims"
    assert classify("FOMC Meeting Minutes", "USD") == "fomc_minutes"
    assert classify("FOMC Statement", "USD") == "fomc"
    assert classify("Federal Funds Rate", "USD") == "fomc"
    assert classify("ADP Non-Farm Employment Change", "USD") == "adp"
    assert classify("Fed Chair Powell Testifies", "USD") == "usd_other"  # not matched: wording differs
    assert classify("Fed Chair Testimony", "USD") == "fed_speech"
    assert classify("Monetary Policy Report", "USD") == "fed_speech"
    assert classify("Building Permits", "USD") == "usd_other"
    assert classify("Main Refinancing Rate", "EUR") == "ecb_decision"
    assert classify("Official Bank Rate", "GBP") == "boe_decision"
    assert classify("BOJ Policy Rate", "JPY") == "boj_decision"
    assert classify("CPI y/y", "GBP") is None  # non-USD, not a central-bank decision
    assert classify("German ZEW Economic Sentiment", "EUR") is None


def test_clean_text_strips_control_chars_and_truncates():
    assert clean_text("a\x07b\nc", 10) == "a b c"
    assert len(clean_text("x" * 500, 40)) == 40
    assert clean_text(None, 5) == ""


def test_build_filters_normalises_times_and_supersedes_seed():
    res = build_macro_calendar(FEED, SEED, NOW, 30)
    ev = res["events"]
    titles = [e["title"] for e in ev]
    assert "Empire State Manufacturing Index" not in titles  # medium impact
    assert "Prelim UoM Consumer Sentiment" not in titles  # low impact
    assert "German ZEW Economic Sentiment" not in titles  # non-USD, not a central bank
    assert "Building Permits" not in titles  # naive date: skipped, not guessed
    assert "ISM Manufacturing PMI" not in titles  # before now
    wanted = {"CPI m/m", "Core CPI m/m", "Unemployment Claims", "Main Refinancing Rate", "Official Bank Rate", "Retail Sales m/m"}
    assert wanted <= set(titles)

    cpi = next(e for e in ev if e["title"] == "CPI m/m")
    assert cpi["ts_utc"] == "2026-10-14T12:30:00+00:00"
    assert cpi["ts_kyiv"].startswith("2026-10-14T15:30:00") and cpi["ts_kyiv"].endswith("+03:00")  # EEST
    assert cpi["kind"] == "cpi" and cpi["forecast"] == "0.3%" and cpi["previous"] == "0.4%" and cpi["source"] == "forexfactory"
    ecb = next(e for e in ev if e["kind"] == "ecb_decision")
    assert ecb["ts_utc"] == "2026-10-15T12:15:00+00:00"

    # the seed CPI (2026-10-14) lies inside the feed span and is superseded; the seed FOMC (2026-10-28) is added
    assert not [e for e in ev if e["source"] == "seed" and e["kind"] == "cpi" and e["ts_utc"].startswith("2026-10-14")]
    assert res["seed_superseded"] >= 1
    assert any(e["source"] == "seed" and e["kind"] == "fomc" and e["ts_utc"].startswith("2026-10-28") for e in ev)
    assert [e["ts_utc"] for e in ev] == sorted(e["ts_utc"] for e in ev)
    assert res["feed"]["covers_from"].startswith("2026-10-05")
    assert any("skipped" in w for w in res["warnings"])


def test_untrusted_title_is_sanitised():
    ev = build_macro_calendar(FEED, [], NOW, 14)["events"]
    speech = next(e for e in ev if e["kind"] == "fed_speech")
    assert "\x07" not in speech["title"] and speech["forecast"] is None


def test_48h_flag_uses_the_passed_time():
    assert build_macro_calendar(FEED, SEED, NOW, 14)["high_impact_within_48h"] is False  # next release is 4 days away
    later = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    res = build_macro_calendar(FEED, SEED, later, 14)
    assert res["high_impact_within_48h"] is True and res["next_event"]["kind"] == "cpi"


def test_grace_window_and_all_rows_unparsable():
    cpi_ts = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    after = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)  # 1.5 h after the release
    res = build_macro_calendar(FEED, [], after, 1)
    assert res["high_impact_within_48h"] is True
    assert any(e["ts_utc"] == cpi_ts.isoformat() and e["hours_ahead"] < 0 for e in res["events"])
    assert res["next_event"]["hours_ahead"] >= 0 or all(e["hours_ahead"] < 0 for e in res["events"])
    late = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)  # 2.5 h after: dropped
    assert not any(e["ts_utc"] == cpi_ts.isoformat() for e in build_macro_calendar(FEED, [], late, 1)["events"])
    bad = build_macro_calendar([{"title": "CPI m/m", "country": "USD", "date": "2026-10-14 08:30", "impact": "High"}], [], NOW, 7)
    assert bad["all_feed_rows_unparsable"] is True


def test_feed_down_falls_back_to_seed_only():
    res = build_macro_calendar([], SEED, NOW, 30)
    assert res["feed"]["rows"] == 0 and res["seed_added"] >= 2
    assert all(e["source"] == "seed" for e in res["events"])
    assert any("approximate" in w for w in res["warnings"])


def test_seed_is_sorted_and_has_the_2026_midterms():
    ts = [e["ts"] for e in SEED]
    assert ts == sorted(ts)
    el = [e for e in SEED if e["kind"] == "election"]
    assert len(el) == 1 and el[0]["impact"] == "high"
    assert datetime.fromtimestamp(el[0]["ts"], UTC) == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # nominal time, a Tuesday


def test_midterms_explain_the_week_and_do_not_trigger_paper_trades():
    # the week 30.10 -> 06.11: FOMC is the week before, the election sits inside it (forward-vol bump in the card)
    res = build_macro_calendar([], SEED, datetime(2026, 10, 30, 8, 0, tzinfo=UTC), 7)
    ev = [e for e in res["events"] if e["kind"] == "election"]
    assert len(ev) == 1 and ev[0]["source"] == "seed" and ev[0]["ts_utc"].startswith("2026-11-03")
    assert ev[0]["ts_kyiv"].startswith("2026-11-03T14:00:00") and ev[0]["hours_ahead"] == 100.0
    assert "election" not in EVENT_KINDS  # the paper method trades around FOMC/CPI only


def test_warns_when_horizon_passes_end_of_seed():
    last = datetime.fromtimestamp(max(e["ts"] for e in SEED), UTC)  # seed now runs to 2027-12
    res = build_macro_calendar([], SEED, last - timedelta(days=10), 60)
    assert any("seed ends" in w for w in res["warnings"])


async def test_macro_calendar_tool(server):
    env = await call(server, "macro_calendar", horizon_days=30)
    assert env["ok"], env["missing"]
    d = env["data"]
    assert env["missing"] == []
    assert {e["provider"] for e in env["evidence"]} == {"forexfactory"} and len(env["evidence"]) == 2
    kinds = {e["kind"] for e in d["events"]}
    assert {"cpi", "jobless_claims", "ecb_decision", "boe_decision", "retail_sales", "fomc"} <= kinds
    assert d["feed"]["rows"] == len(FEED) - 1  # the timezone-less row is dropped
    assert all(e["hours_ahead"] >= 0 for e in d["events"])


async def test_macro_calendar_provider_failure(state):
    tr = make_transports(state)
    tr["forexfactory"] = httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "macro_calendar", horizon_days=30)  # must not raise
    assert {m["provider"] for m in env["missing"]} == {"forexfactory"} and len(env["missing"]) == 2
    assert env["ok"] is True  # the seed still supplies dates
    assert env["data"]["feed"]["rows"] == 0
    assert env["data"]["events"] and all(e["source"] == "seed" for e in env["data"]["events"])
    assert any("incomplete" in w for w in env["warnings"])


async def test_macro_calendar_bad_shape_goes_to_missing(state):
    tr = make_transports(state)
    tr["forexfactory"] = httpx.MockTransport(lambda req: httpx.Response(200, json={"oops": 1}))
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "macro_calendar", horizon_days=7)
    assert len(env["missing"]) == 2 and all(m["reason"] == "backend_error" for m in env["missing"])


def test_real_feed_capture_parses_and_keeps_only_in_scope_rows():
    """A real ForexFactory capture (2026-10-18): all rows parse; that week had no high-impact USD or central-bank event,
    so only seed events remain and the high-impact CAD/GBP/NZD CPI rows are dropped."""
    cap = json.loads((Path(__file__).parent / "fixtures" / "forexfactory_thisweek_2026-10-18.json").read_text())
    res = build_macro_calendar(cap["thisweek"], SEED, datetime(2026, 10, 18, 6, 30, tzinfo=UTC), 14)
    assert res["feed"]["rows"] == len(cap["thisweek"]) == 86
    assert res["events"] and all(e["source"] == "seed" for e in res["events"])
    assert [e["kind"] for e in res["events"]] == ["fomc"]


async def test_macro_calendar_next_week_not_published_is_still_ok(state):
    # ForexFactory answers 404 for next week from Sunday to mid-week: an empty horizon is a valid answer, not a failure.
    tr = make_transports(state)
    tr["forexfactory"] = httpx.MockTransport(
        lambda req: (
            httpx.Response(200, json=FIX["thisweek"])
            if req.url.path == "/ff_calendar_thisweek.json"
            else httpx.Response(404, text="not found")
        )
    )
    rt2 = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr, exchange_factory=FakeEx)
    rt2.now = lambda: NOW
    srv, _ = build_server(rt2)
    env = await call(srv, "macro_calendar", horizon_days=1)
    assert env["ok"] is True and env["data"]["events"] == []
    assert len(env["missing"]) == 1 and env["missing"][0]["provider"] == "forexfactory"
    assert any("no high-impact event" in w for w in env["warnings"])
