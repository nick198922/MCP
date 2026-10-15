"""Calls tracker: parsing calls.csv and evaluating a call on daily bars (analytics/calls.py, scripts/calls_check.py)."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import calls_check  # noqa: E402

from tradedesk.analytics.calls import Bar, Call, evaluate_call, parse_calls, summarize  # noqa: E402

D0 = date(2026, 10, 15)
HEADER = "id,date,source,asset,side,zone_low,zone_high,target,stop,horizon_days,symbol,note\n"


def bars(*ohlc):
    """Daily bars from the day after D0."""
    return [Bar(D0 + timedelta(days=i + 1), *x) for i, x in enumerate(ohlc)]


def call(**kw):
    base = dict(id="X", day=D0, source="S", asset="BTC", side="long", zone_low=80.0, zone_high=82.0, horizon_days=10)
    return Call(**{**base, **kw})


def test_repo_calls_file_parses_cleanly():
    text = (Path(__file__).resolve().parent.parent / "docs/calls/calls.csv").read_text(encoding="utf-8")
    calls, errors = parse_calls(text)
    assert errors == [] and len(calls) >= 20
    assert all(c.source.startswith("LIB-") for c in calls)


def test_parse_refuses_bad_rows():
    text = HEADER + "\n".join(
        [
            "# comment, ignored",
            "A,2026-10-15,S,btc,long,80,82,90,75,,,ok",
            "A,2026-10-15,S,BTC,long,80,82,,,,,duplicate",
            "B,15.10.2026,S,BTC,long,80,82,,,,,bad date",
            "C,2026-10-15,S,BTC,up,80,82,,,,,bad side",
            "D,2026-10-15,S,BTC,long,82,80,,,,,inverted zone",
            "E,2026-10-15,S,BTC,long,80,82,81,,,,target inside zone",
            "F,2026-10-15,S,BTC,short,85,86,,84,,,stop below a short zone",
            "G,2026-10-15,S,ENA,long,0.15,,,,30,,one price",
        ]
    )
    calls, errors = parse_calls(text)
    assert [c.id for c in calls] == ["A", "G"]
    assert calls[0].asset == "BTC" and calls[0].target == 90 and calls[0].horizon_days == 90
    assert calls[1].zone_low == calls[1].zone_high == 0.15 and calls[1].horizon_days == 30
    assert len(errors) == 6


def test_long_touch_entry_and_target_next_day():
    c = call(target=90.0, stop=75.0)
    r = evaluate_call(c, bars((85, 86, 81, 84), (84, 91, 83, 90), (90, 92, 89, 91)), D0 + timedelta(days=4))
    assert r.status == "target" and r.touched_on == D0 + timedelta(days=1) and r.days_to_touch == 1
    assert r.entry == 82.0  # touched from above: filled at the zone's top
    assert abs(r.result - (90 / 82 - 1)) < 1e-12
    assert abs(r.max_drawdown - (81 / 82 - 1)) < 1e-12 and abs(r.max_gain - (92 / 82 - 1)) < 1e-12
    assert abs(r.hold_return - (91 / 85 - 1)) < 1e-12


def test_target_on_touch_day_does_not_count_but_stop_does():
    r = evaluate_call(call(target=90.0), bars((85, 95, 81, 84)), D0 + timedelta(days=2))
    assert r.status == "open" and r.max_gain is None  # the high may have come before the entry
    r = evaluate_call(call(stop=79.0), bars((85, 86, 78, 84)), D0 + timedelta(days=2))
    assert r.status == "stopped" and abs(r.result - (79 / 82 - 1)) < 1e-12


def test_gap_through_zone_and_stop_gap():
    r = evaluate_call(call(stop=75.0), bars((79, 80, 78, 79), (70, 72, 69, 71)), D0 + timedelta(days=3))
    assert r.entry == 79.0  # opened below the zone: filled at the open
    assert r.status == "stopped" and abs(r.result - (70 / 79 - 1)) < 1e-12  # gapped through the stop: filled at the open


def test_both_target_and_stop_on_one_bar_is_a_stop():
    r = evaluate_call(call(target=90.0, stop=75.0), bars((85, 86, 81, 84), (84, 91, 74, 80)), D0 + timedelta(days=3))
    assert r.status == "stopped"


def test_short_mirrors_long():
    c = call(side="short", zone_low=85.0, zone_high=86.0, target=80.0, stop=88.0)
    r = evaluate_call(c, bars((84, 85.5, 83, 85), (85, 86, 79, 80)), D0 + timedelta(days=3))
    assert r.status == "target" and r.entry == 85.0 and abs(r.result - (1 - 80 / 85)) < 1e-12
    assert abs(r.hold_return - (1 - 80 / 84)) < 1e-12


def test_waiting_expired_ended_and_no_data():
    never = bars(*[(90, 91, 89, 90)] * 3)
    assert evaluate_call(call(), never, D0 + timedelta(days=4)).status == "waiting"
    assert evaluate_call(call(horizon_days=3), never, D0 + timedelta(days=4)).status == "expired"
    later = [Bar(D0 + timedelta(days=i), 90, 91, 89, 90) for i in range(3, 6)]  # after the 2-day window: ignored
    r = evaluate_call(call(horizon_days=2), bars((85, 86, 81, 84), (84, 85, 83, 83)) + later, D0 + timedelta(days=9))
    assert r.status == "ended" and r.closed_on == D0 + timedelta(days=2) and abs(r.result - (83 / 82 - 1)) < 1e-12
    assert evaluate_call(call(), [], D0 + timedelta(days=1)).status == "waiting"
    assert evaluate_call(call(), [], D0 + timedelta(days=5)).status == "no_data"


def test_summary_and_report():
    c1, c2 = call(id="A", target=90.0), call(id="B", source="T")
    r1 = evaluate_call(c1, bars((85, 86, 81, 84), (84, 91, 83, 90)), D0 + timedelta(days=3))
    r2 = evaluate_call(c2, [], D0 + timedelta(days=5))
    s = summarize([(c1, r1), (c2, r2)])
    assert s["S"]["touched"] == 1 and s["S"]["target"] == 1 and s["T"]["with_data"] == 0
    md = calls_check.render([(c1, r1, "binanceusdm:BTC/USDT:USDT"), (c2, r2, "")], ["Z: bad"], D0 + timedelta(days=5))
    assert "| A | 2026-10-15 | BTC | long | 80–82 | 90 / — | ціль ✓ |" in md
    assert "немає даних" in md and "- Z: bad" in md
    far = evaluate_call(call(id="C"), bars((300, 310, 290, 300)), D0 + timedelta(days=2))
    assert "перевірте тікер" in calls_check.render([(call(id="C"), far, "bybit:X")], [], D0 + timedelta(days=2))
