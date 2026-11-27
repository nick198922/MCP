"""Forward scanner of IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward, step 2 (item 484): scripts/pm_cpi_forward.py.

No network: Gamma is an ``httpx.MockTransport`` in-process and an unreachable proxy (127.0.0.1:9) in the subprocesses;
the time is a parameter. Prices of the decision tests are synthetic; the fixture of 26.11 is used only where the
registration asks for it (the CODE_PATHS list), and no decision on it is asserted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tradedesk.analytics.pm_cpi import book_depth, bracket_prob, brackets, brier_release, clv, row_digest, with_book
from tradedesk.collector.pm_cpi_snapshot import SERIES
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pm_cpi_forward as f  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "pm_cpi_snapshot_2026-11-26.json"
R = "2026-12-10"
AT_T7 = datetime(2026, 12, 3, 21, 30, tzinfo=UTC)
AT_T3 = datetime(2026, 12, 7, 22, 10, tzinfo=UTC)
AT_T1 = datetime(2026, 12, 9, 21, 30, tzinfo=UTC)
TITLES = ["≤3.3%", "3.4%", "3.5%", "3.6%", "3.7%", "3.8%", "3.9%", "4.0%", "4.1%", "4.2%", "4.3%", "≥4.4%"]
ASKS = {"3.8%": 0.10, "3.7%": 0.11}  # synthetic: q(3.8) ~ 0.239, q(3.7) ~ 0.207 at mu 3.79, sd 0.164; the rest 0.50
RULES = "the 12-month change ... before seasonal adjustment ... with only one decimal point ..."
GH = ("GitHub", "noreply@github.com")


def _ts(day: str, hm: str = "15:40") -> int:
    return int(datetime.fromisoformat(f"{day}T{hm}:00+00:00").timestamp())


def _bracket(series, eid, slug, end, i, title, ask, ts, fee_type="economics_fees", **kw):
    bid = round(ask - 0.01, 4) if ask is not None else None
    return {"series": series, "event_id": eid, "event_slug": slug, "end_date": end, "market_id": f"{eid}m{i}",
            "title": title, "fee_type": fee_type, "token": f"{eid}t{i}", "book": True, "bid": bid, "ask": ask,
            "bid_usd": 5.0, "ask_usd": 30.0, "book_ts_ms": ts * 1000, "read_ts": ts, **kw}  # fmt: skip


def synth_brackets(day, asks=None, yoy_slug="november-inflation-us-annual", extra=()):
    ts = _ts(day)
    asks = {**ASKS, **(asks or {})}
    yoy = [_bracket("10007", "e1", yoy_slug, "2026-12-10T13:30:00Z", i, t, asks.get(t, 0.5), ts) for i, t in enumerate(TITLES)]
    mom = [_bracket("10008", "e2", "november-inflation-us-monthly", "2026-12-11T04:59:00Z", i, t, 0.3, ts)
           for i, t in enumerate(["≤0.1%", "0.2%", "≥0.3%"])]  # fmt: skip
    core = [_bracket("11905", "e3", "core-cpi-yoy-november-2026", "2026-12-10T13:30:00Z", i, t, 0.3, ts)
            for i, t in enumerate(["≤2.8%", "2.9%", "≥3.0%"])]  # fmt: skip
    return [*yoy, *mom, *core, *extra]


def synth_depth(brs):
    main = {}
    for b in brs:
        if b.get("book") and isinstance(b.get("ask"), float) and math.isfinite(b["ask"]):
            main[b["token"]] = {"read_ts": b["read_ts"], "min_size": 5.0, "bids": [[b["bid"], 100.0]],
                                "asks": [[b["ask"], 300.0], [round(b["ask"] + 0.02, 4), 1000.0]]}  # fmt: skip
    return {"main": main, "early": []}


def base_row(day, brs, depth, **kw):
    ts = _ts(day)
    row = {"day": day, "created_ts": ts - 600, "updated_ts": ts + 60, "status": "complete", "late": 0, "cf_tries": 1,
           "cf_fetches": 1, "books_tries": 1, "cf_year_ts": ts - 60, "cf_month_ts": ts - 60, "cf_year_sha": "a" * 64,
           "cf_month_sha": "b" * 64, "cf_stamp_year": f"{day} 00:00", "cf_last_day_year": day, "cf_last_day_month": day,
           "cf_fresh": 1, "target": "2026-11", "mom_target": "2026-11", "yoy_headline_pct": 3.79, "cf_points": "{}",
           "books": json.dumps({"series_listed": list(SERIES), "counts": {}, "brackets": brs}), "books_paired": 1,
           "books_early": None, "books_depth_z": zlib.compress(json.dumps(depth).encode()), "books_ts": ts,
           "n_events": len({b["event_id"] for b in brs}), "n_brackets": len(brs), "n_quoted": len(brs)}  # fmt: skip
    return {**row, **kw}


def synth_row(day, asks=None, **kw):
    brs = synth_brackets(day, asks)
    return base_row(day, brs, synth_depth(brs), **kw)


def fixture_row(day):
    """The fixture's events and books of 26.11 as a complete row of ``day`` (nowcast point moved to that day)."""
    sample = json.loads(FIXTURE.read_text())
    ts = _ts(day)
    books = {b["asset_id"]: b for b in sample["books"]}
    main = [with_book(b, books.get(b["token"]), ts) for sid, evs in sample["events"].items() for b in brackets(sid, evs)]
    depth = {"main": {b["token"]: {"read_ts": ts, **book_depth(books[b["token"]])} for b in main if b["token"] in books}, "early": []}
    return base_row(day, main, depth)


def make_db(path: Path, *rows) -> Path:
    async def go():
        s = Store(path)
        await s.init()
        try:
            for r in rows:
                await s.upsert_pm_cpi(r)
        finally:
            await s.aclose()

    asyncio.run(go())
    return path


# Real Gamma answers of 2026-11-27 (non-price fields only): /markets/{id} has no `events` field (review C 484, blocker 2),
# the event title is in /events/{id}. The fakes below keep that shape.
GAMMA_SAMPLE = json.loads((REPO_ROOT / "tests" / "fixtures" / "polymarket_cpi_event_market_2026-11-27.json").read_text())
EVENT_TITLES = {"e1": "November Inflation US - Annual", "e2": "November Inflation US - Monthly", "e3": "Core CPI YoY - November 2026"}


def gamma_answer(mid, desc=RULES, **kw):
    """A /markets/{id} answer: the real sample's fields with this id, the rule text and a fee rate of 0.02 (other than
    the fee_type table's 0.01, so a test sees which one the decision took)."""
    return {**GAMMA_SAMPLE["market"], "id": mid, "description": desc, "feeSchedule": {"rate": 0.02, "exponent": 1},
            "feeType": "economics_fees", "orderMinSize": 5, **kw}  # fmt: skip


def event_answer(eid, **kw):
    """A /events/{id} answer: the real sample's fields with this id and title."""
    return {**GAMMA_SAMPLE["event"], "id": eid, "title": EVENT_TITLES.get(eid, f"event {eid}"), **kw}


def gamma_rt(answers=None, down=False, calls=None, events=None):
    def handler(req: httpx.Request) -> httpx.Response:
        kind, key = req.url.path.rsplit("/", 2)[-2:]
        if calls is not None:
            calls.append(req.url.path)
        if down:
            return httpx.Response(500, text="down")
        if kind == "events":
            a = (events or {}).get(key, event_answer(key))
        else:
            a = (answers or {}).get(key, gamma_answer(key))
        return httpx.Response(200, json=a) if a is not None else httpx.Response(404, text="not found")

    def factory():
        rt = Runtime(Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
                     transports={"polymarket:gamma": httpx.MockTransport(handler)})  # fmt: skip
        rt.polymarket.gamma._limiter = TokenBucket(1000.0, 1000)
        rt.now = lambda: AT_T3  # not read by the scanner (D is a parameter); pinned all the same
        return rt

    return factory


def write(stage, at, tmp_path, db, rt_factory=None):
    return f.write_stage(stage, R, at, db=db, root=tmp_path / "state", docs=tmp_path / "docs", repo=REPO_ROOT, code_sha="HEAD",
                         m0=None, tree=REPO_ROOT, rt_factory=rt_factory or gamma_rt(down=True))  # fmt: skip


def rec_of(tmp_path, stage):
    return json.loads((tmp_path / "state" / R / f"{stage}.json").read_bytes())


def offline_env():
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env.update(HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9", NO_PROXY="")
    return env


# --------------------------------------------------------------------------------------------------- pure functions


def test_job_series_are_the_collectors():
    assert f.JOB_SERIES == SERIES


@pytest.mark.parametrize(
    ("slug", "end", "want"),
    [("november-inflation-us-annual", "2026-12-10T13:30:00Z", "2026-11"),
     ("december-inflation-us-annual", "2027-01-13T13:30:00Z", "2026-12"),
     ("january-inflation-us-annual-lower-brackets-817", "2026-02-11T13:30:00Z", "2026-1"),
     ("november-inflation-us-annual", "2027-01-13T13:30:00Z", None),  # the end date is not in the next month
     ("inflation-us-annual", "2026-12-10", None), ("november-inflation-us-annual", "garbage", None)],
)  # fmt: skip
def test_event_month_needs_the_end_date_in_the_next_month(slug, end, want):
    assert f.event_month(slug, end) == want


def test_select_events_takes_only_headline_yoy_of_the_target():
    brs = synth_brackets("2026-12-07", extra=[
        _bracket("10007", "e4", "cpi-yoy-november-2026", "2026-12-10T13:30:00Z", 0, "3.7%", 0.2, 1),  # renamed
        _bracket("10007", "e5", "october-inflation-us-annual", "2026-11-13T13:30:00Z", 0, "3.7%", 0.2, 1),
        _bracket("99", "e6", "november-inflation-us-annual", "2026-12-10T13:30:00Z", 0, "3.7%", 0.2, 1),
    ])  # fmt: skip
    ev = {e["event_id"]: e for e in f.select_events(brs, "2026-11")}
    assert ev["e1"]["taken"] and ev["e1"]["month"] == "2026-11" and ev["e1"]["n_brackets"] == 12
    assert (ev["e2"]["why"], ev["e3"]["why"], ev["e4"]["why"]) == ("not headline YoY",) * 3
    assert ev["e5"]["why"] == "target" and ev["e6"]["why"] == "series"
    assert f.select_events(brs, "2026-1")[0]["why"] in ("target", "not headline YoY")
    assert f.target_of_release("2026-12-10") == "2026-11" and f.target_of_release("2027-01-13") == "2026-12"


def test_rule_phrases_and_gamma_fields():
    assert f.rules_why([None, ""]) == ("unchecked", None)
    assert f.rules_why([RULES, None]) == ("ok", None)
    assert f.rules_why([RULES, "12-month, one decimal"]) == ("missing", "rules: no 'before seasonal adjustment'")
    g = f.gamma_fields(gamma_answer("7", feeSchedule='{"rate": 0.03, "exponent": 1}', orderMinSize=float("nan")), "7")
    assert g["ok"] and g["feeSchedule"] == {"rate": 0.03, "exponent": 1} and g["orderMinSize"] is None
    assert g["description_sha256"] == hashlib.sha256(RULES.encode()).hexdigest() and all(g["phrases"].values())
    assert (g["closed"], g["active"], g["acceptingOrders"]) == (False, True, True)  # descriptive (finding 7)
    assert tuple(sorted(g)) == tuple(sorted(f.GAMMA_KEYS))
    assert f.gamma_fields({"id": "8"}, "7") == {"ok": False, "why": "not this market"}
    assert f.gamma_fields(None, "7")["ok"] is False
    e = f.event_fields(event_answer("e1"), "e1")
    assert e == {"ok": True, "title": "November Inflation US - Annual", "slug": "november-inflation-us-annual", "closed": False,
                 "active": True}  # fmt: skip
    assert f.event_fields(event_answer("e1"), "e2") == {"ok": False, "why": "not this event"}


def test_gamma_fakes_have_the_real_answer_shape():
    # review C 484, blocker 2: the live /markets/{id} has no `events`; the title is only in /events/{id}
    real = GAMMA_SAMPLE
    assert "events" not in real["market_keys"] and "title" in real["event_keys"] and "markets" in real["event_keys"]
    assert real["event"]["id"] == "1376034" and real["event"]["title"] == "November Inflation US - Annual"
    assert real["market"]["groupItemTitle"] == "≤3.3%" and set(real["market"]) <= set(real["market_keys"])
    assert "events" not in gamma_answer("x") and set(gamma_answer("x")) <= set(real["market_keys"])
    assert set(event_answer("e1")) <= set(real["event_keys"])
    prices = {"bestBid", "bestAsk", "lastTradePrice", "outcomePrices", "spread", "volume", "liquidity"}
    assert not prices & (set(real["market"]) | set(real["event"]) | {k for m in real["event"]["markets"] for k in m})


def test_provider_event_reads_one_gamma_event():
    def handler(req):
        if req.url.path == "/events/1376034":
            return httpx.Response(200, json=GAMMA_SAMPLE["event"])
        return httpx.Response(200, json=[])  # not a mapping

    rt = Runtime(Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
                 transports={"polymarket:gamma": httpx.MockTransport(handler)})  # fmt: skip
    rt.now = lambda: AT_T3  # not read by the provider; pinned all the same

    async def go():
        try:
            got = await rt.polymarket.event("1376034")
            with pytest.raises(ProviderError, match="unexpected shape at /events"):
                await rt.polymarket.event("2")
            return got
        finally:
            await rt.aclose()

    got = asyncio.run(go())
    assert got.data["title"] == "November Inflation US - Annual" and got.evidence.tool == "event"
    assert got.evidence.args == {"id": "1376034"}


@pytest.mark.parametrize(
    ("release", "journal", "want"),
    [(R, ["V1 · fail: x"], (0.164, None)),
     ("2027-01-13", ["- `V1 · pass` по year і month", "V2 · pass", "sigma_A · 0.1500 · n 150", "sigma_A · 0.2"], (0.15, None)),
     ("2027-01-13", ["V1 · pass", "V2 · fail: overwritten"], (0.164, None)),
     ("2027-01-13", ["V1 · pass", "V2 · pass"], (None, "no sigma_A")),
     ("2027-01-13", ["V1 · pass"], (None, "no sigma_A")), ("2027-01-13", [], (None, "no sigma_A"))],
)  # fmt: skip
def test_sigma_follows_the_vintage_checks_of_the_journal(release, journal, want):
    sd, _, why = f.sigma_for(release, journal)
    assert (sd, why) == want


def test_journal_lines_may_carry_a_day_or_a_pasted_prefix():
    # review C 484, findings 4 and 6: a V line starting with its day, a stage line pasted with 'journal: '
    dated = ["- 2026-12-03 V1 · pass по year і month", "2026-12-11 `V2 · pass` по year і month", "2026-12-11 sigma_A · 0.1700 · n 140"]
    assert f.sigma_for("2027-01-13", dated)[:1] == (0.17,)
    assert f.sigma_for("2027-01-13", ["2026-12-03 V1 · fail: x"])[0] == 0.164
    sha = "c" * 64
    card = CARD_TEXT.format(lines=f"journal: {R} T−3 · D x · без сигналу · sha256 {sha}\n")
    assert f.journal_sha(card, R) == (sha, None)
    assert f.one_line("3.8%\nabc\r\x1b d") == "3.8% abc   d"


def test_decide_picks_two_and_fills_along_the_depth():
    row = synth_row("2026-12-07")
    why, events, mids = f.t3_prepare(row, R)
    assert why is None and mids == [f"e1m{i}" for i in range(12)]  # every YoY market; titles come from /events/{id}
    assert f.event_ids(events) == ["e3", "e1", "e2"]  # every open event, ordered by slug
    p = f.t3_payload(row, R, why, events, {}, {}, [], (0.164, "x", None))
    picks = [r for r in p["decision"]["rule_signals"] if r["pick"]]
    assert [r["title"] for r in picks] == ["3.7%", "3.8%"] and p["skip"] is None  # flagged in the row order
    q38 = bracket_prob((38, 38), 3.79, 0.164)
    assert picks[1]["q"] == pytest.approx(q38) and picks[1]["edge"] == pytest.approx(q38 - 0.10 - 0.01 * 0.10 * 0.90)
    f37, f38 = p["decision"]["fills"]
    assert f38["stop"] == "stake" and f38["levels"] == 2 and f38["cost_usd"] == pytest.approx(50.0) and f38["executable"]
    assert f37["stop"] == "edge" and f37["shares"] == pytest.approx(300.0) and f37["p_in"] == pytest.approx(0.11)
    assert f.same_record(f.decide(json.loads(json.dumps(p["decision_input"]))), json.loads(json.dumps(p["decision"])))


def test_t3_skip_reasons():
    row = synth_row("2026-12-07")
    why, events, _ = f.t3_prepare(row, R)
    assert f.t3_payload(row, R, why, events, {}, {}, [], (None, None, "no sigma_A"))["skip"] == "no sigma_A"
    assert f.t3_payload(row, R, why, events, {}, {}, [], (float("nan"), "x", None))["skip"] == "model"
    brs = synth_brackets("2026-12-07", asks=dict.fromkeys(TITLES))  # no ask and no bid anywhere in the YoY event
    row = base_row("2026-12-07", brs, synth_depth(brs))
    why, events, _ = f.t3_prepare(row, R)
    assert f.t3_payload(row, R, why, events, {}, {}, [], (0.164, "x", None))["skip"] == "no quotes"


# --------------------------------------------------------------------------------------------- write stages in-process


def test_t3_writes_once_inside_its_window_from_the_row_and_gamma(tmp_path, capsys):
    row = synth_row("2026-12-07")
    db = make_db(tmp_path / "db.sqlite", row, synth_row("2026-12-06", asks={"3.8%": 0.01}))
    calls: list = []
    assert write("T-3", datetime(2026, 12, 7, 21, 4, tzinfo=UTC), tmp_path, db, gamma_rt(calls=calls)) == 1  # early
    assert write("T-3", datetime(2026, 12, 9, 12, 0, 1, tzinfo=UTC), tmp_path, db, gamma_rt(calls=calls)) == 1  # late
    assert not (tmp_path / "state").exists() and calls == []
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(calls=calls)) == 0
    out = capsys.readouterr().out.splitlines()
    raw = (tmp_path / "state" / R / "T-3.json").read_bytes()
    assert (tmp_path / "docs" / R / "T-3.json").read_bytes() == raw  # the copy for main: the same bytes
    # the journal line is printed alone on the line after the note, without a prefix, and gives the file's sha256
    line = out[out.index(f.JOURNAL_NOTE) + 1]
    assert line.startswith(f"{R} T−3 · D {AT_T3.isoformat()} · джерело рядок 2026-12-07/")
    assert f.journal_sha(CARD_TEXT.format(lines=line + "\n"), R) == (hashlib.sha256(raw).hexdigest(), None)
    rec = json.loads(raw)
    assert rec["source"]["day"] == "2026-12-07" and rec["D"] == AT_T3.isoformat() and rec["skip"] is None
    # the digest is recomputed from the file alone and equals the database row's (read mode=ro)
    assert f.row_digest(rec["digest_preimage"]) == rec["row_digest"] == row_digest(f.read_row(db, "2026-12-07"))
    assert rec["row"]["books_depth_z"]["main"]["e1t5"]["asks"][0] == [0.10, 300.0]  # unpacked
    # Gamma: /markets/{id} of every YoY market, /events/{id} of every open event; the market's own feeSchedule wins
    # over the fee_type table
    assert sorted(calls) == sorted([f"/markets/e1m{i}" for i in range(12)] + ["/events/e1", "/events/e2", "/events/e3"])
    assert set(rec["gamma"]) == {f"e1m{i}" for i in range(12)} and set(rec["gamma_events"]) == {"e1", "e2", "e3"}
    assert rec["gamma"]["e1m0"]["acceptingOrders"] is True and rec["gamma"]["e1m0"]["closed"] is False
    sig = {r["title"]: r for r in rec["decision"]["rule_signals"]}
    assert sig["3.8%"]["fee"] == pytest.approx(0.02 * 0.10 * 0.90) and sig["3.8%"]["pick"]
    ev = {e["event_id"]: e for e in rec["events"]}
    assert ev["e1"]["rules"] == "ok" and ev["e2"]["why"] == "not headline YoY"
    assert {k: e["title"] for k, e in ev.items()} == EVENT_TITLES  # the title of every open event (registration п. 2)
    assert f.gamma_why(rec, rec["gamma"], rec["gamma_events"]) == []
    assert f.same_record(f.decide(rec["decision_input"]), rec["decision"])
    assert rec["code_sha"] == "HEAD" and rec["CODE_PATHS"] == list(f.CODE_PATHS) and isinstance(rec["code_dirty"], str)
    # written once: a second run (also after deleting the local file, the copy stays) writes nothing
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt()) == 1 and "exists" in capsys.readouterr().out
    (tmp_path / "state" / R / "T-3.json").unlink()
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt()) == 1
    assert not (tmp_path / "state" / R / "T-3.json").exists() and (tmp_path / "docs" / R / "T-3.json").read_bytes() == raw


def test_unusable_t3_row_is_a_skip_and_no_other_day_is_substituted(tmp_path):
    # registration п. 5 (1): nothing is substituted, neither another day's row nor a snapshot. That no snapshot is read
    # rests on the scanner having no code that reads one (review C 484, finding 9 (a)): the snapshot directory below
    # sits where no implementation would look, so the assertions about it are a check of the source, not more.
    snap = tmp_path / "snapshots" / "20261207T1600Z"
    snap.mkdir(parents=True)
    (snap / "events_open.json").write_text("[]")
    (snap / "nowcast_year.json").write_text("[]")
    stale = synth_row("2026-12-07", cf_last_day_year="2026-12-01")
    db = make_db(tmp_path / "db.sqlite", stale, synth_row("2026-12-06"), synth_row("2026-12-08"))
    calls: list = []
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(calls=calls)) == 0
    rec = rec_of(tmp_path, "T-3")
    assert rec["skip"] == "nowcast stale" and rec["decision"] is None and rec["decision_input"] is None
    assert sorted(calls) == ["/events/e1", "/events/e2", "/events/e3"]  # titles only: no market asked for a skip
    assert rec["source"]["day"] == "2026-12-07" and rec["row_digest"] == row_digest(stale)
    assert "20261207T1600Z" not in json.dumps(rec) and "snapshots/" not in json.dumps(rec)
    empty = tmp_path / "e"
    empty.mkdir()
    db2 = make_db(empty / "db.sqlite", synth_row("2026-12-06"))
    assert write("T-3", AT_T3, empty, db2) == 0
    rec = rec_of(empty, "T-3")
    assert rec["skip"] == "no row" and rec["row_digest"] is None and rec["source"] == {"day": "2026-12-07", "why": "no row"}


@pytest.mark.parametrize("how", ["renamed", "only other series", "rule text"])
def test_no_yoy_event_is_a_recorded_skip(tmp_path, how):
    if how == "renamed":
        brs = [b for b in synth_brackets("2026-12-07", yoy_slug="cpi-yoy-november-2026")]
    elif how == "only other series":
        brs = [b for b in synth_brackets("2026-12-07") if b["event_id"] != "e1"]
    else:
        brs = synth_brackets("2026-12-07")
    db = make_db(tmp_path / "db.sqlite", base_row("2026-12-07", brs, synth_depth(brs)))
    answers = {f"e1m{i}": gamma_answer(f"e1m{i}", desc="12-month change, one decimal") for i in range(12)} if how == "rule text" else None
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(answers)) == 0
    rec = rec_of(tmp_path, "T-3")
    assert rec["skip"] == "no YoY event" and not any(e["taken"] for e in rec["events"])
    assert {e["slug"] for e in rec["events"]} == {b["event_slug"] for b in brs}  # every open event listed with its reason
    # item 490, R1: excluded only by the rule phrases -> procedural for set B, rebuilt by п. 8 (в) with the event by slug
    fb = f.fallback_record(f.read_row(db, "2026-12-07"), R, "")
    assert set(fb) == set(f.REBUILT) and fb["row_digest"] == rec["row_digest"]
    if how == "rule text":
        e1 = next(e for e in rec["events"] if e["event_id"] == "e1")
        assert e1["why"] == "rules: no 'before seasonal adjustment'" and e1["rules"] == "missing"
        assert f.b_why(rec) == "rules"
        e1 = next(e for e in fb["events"] if e["event_id"] == "e1")
        assert fb["skip"] is None and e1["taken"] and e1["rules"] == "unchecked"
        sig = {r["title"]: r for r in fb["decision"]["rule_signals"]}
        assert sig["3.8%"]["pick"] and sig["3.8%"]["fee"] == pytest.approx(0.01 * 0.10 * 0.90)  # the fee_type table
    else:
        assert f.b_why(rec) is None and fb["skip"] == "no YoY event"  # a data reason: not in set B


def test_b_why_takes_only_a_no_yoy_event_excluded_by_the_rule_phrases():
    ev = [{"event_id": "e1", "why": "rules: no 'one decimal'"}, {"event_id": "e2", "why": "not headline YoY"}]
    assert f.b_why({"skip": "no YoY event", "events": ev}) == "rules"
    assert f.b_why({"skip": "no YoY event", "events": ev[1:]}) is None  # renamed / other series: a data reason
    assert f.b_why({"skip": None, "events": ev}) is None  # a valid release (another YoY event taken) stays as recorded
    assert f.b_why({"skip": "model", "events": ev}) is None and f.b_why({}) is None
    assert f.keys_why({"a": 1, "b": 2}, ["b", "c"]) == "+a, -c" and f.keys_why({"a": 1}, ["a"]) is None


def test_gamma_down_takes_the_registered_fallbacks(tmp_path):
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"))
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(down=True)) == 0
    rec = rec_of(tmp_path, "T-3")
    assert rec["skip"] is None and len(rec["missing"]) == 12 + 3 and not any(g["ok"] for g in rec["gamma"].values())
    assert all(e["title"] is None for e in rec["events"]) and f.gamma_why(rec, rec["gamma"], rec["gamma_events"]) == []
    e1 = next(e for e in rec["events"] if e["event_id"] == "e1")
    assert e1["taken"] and e1["rules"] == "unchecked"  # taken by its slug
    sig = {r["title"]: r for r in rec["decision"]["rule_signals"]}
    assert sig["3.8%"]["fee"] == pytest.approx(0.01 * 0.10 * 0.90)  # the fee_type table: economics_fees 0.01


def test_non_finite_prices_are_no_ask_and_the_file_is_strict_json(tmp_path):
    # fourth review B 481, finding 5
    brs = synth_brackets("2026-12-07", asks={"3.8%": float("nan"), "3.9%": float("inf")})
    depth = synth_depth(brs)
    depth["main"]["e1t7"] = {"read_ts": 1, "min_size": float("nan"), "asks": [[float("nan"), 5.0], [0.05, float("inf")]]}
    text = json.dumps({"series_listed": list(SERIES), "counts": {}, "brackets": brs})  # NaN / Infinity literals
    db = make_db(tmp_path / "db.sqlite", base_row("2026-12-07", brs, depth, books=text))
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(down=True)) == 0
    raw = (tmp_path / "state" / R / "T-3.json").read_text()
    rec = json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-strict JSON literal {c}"))  # allow_nan=False
    sig = {r["title"]: r for r in rec["decision"]["rule_signals"]}
    assert sig["3.8%"]["why"] == "no ask" and sig["3.9%"]["why"] == "no ask" and sig["3.8%"]["q"] is not None
    assert rec["decision_input"]["depth"]["e1t7"] == {"asks": [], "min_size": None, "read_ts": 1}


def test_the_scanner_has_no_code_that_reads_a_snapshot():
    # the property the snapshot-directory tests rest on (review C 484, finding 9 (a)): apart from docstrings, no
    # string, name or attribute of the scanner refers to the snapshot directory or the snapshot stage
    import ast

    tree = ast.parse((REPO_ROOT / "scripts" / "pm_cpi_forward.py").read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}  # fmt: skip
    words = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]
    words += [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert not [w for w in words if "snapshots" in w or "stage_snapshot" in w or "/snapshots" in w]


def test_journal_line_is_printed_alone_and_on_one_line(capsys):
    f.journal_line(f"{R} T−3 · D x · 3.8%\nevil · sha256 {'d' * 64}")
    out = capsys.readouterr().out.splitlines()
    assert out == [f.JOURNAL_NOTE, f"{R} T−3 · D x · 3.8% evil · sha256 {'d' * 64}"]


def test_same_record_takes_two_nans_as_equal():
    assert f.same_record({"edge": float("nan")}, {"edge": float("nan")})
    assert not f.same_record({"edge": float("nan")}, {"edge": 0.1})


def test_t1_row_without_a_yoy_nowcast_gives_the_clv_from_its_books_alone(tmp_path, capsys):
    # item 484 (a): the T-1 source is the job's row only. The snapshot directory of 09.12 next to it is a check of the
    # source, as above: the scanner has no code that reads snapshots (review C 484, finding 9 (a))
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"))
    assert write("T-3", AT_T3, tmp_path, db) == 0
    t3 = rec_of(tmp_path, "T-3")
    picks = {x["token"]: x for x in t3["decision"]["fills"]}
    brs = synth_brackets("2026-12-09", asks={"3.8%": 0.15, "3.7%": 0.09})
    for b in brs:
        b["read_ts"] = _ts("2026-12-09", "15:31")  # read before the nowcast download: the T-3 pairing would drop it
    t1 = base_row("2026-12-09", brs, synth_depth(brs), status="partial", cf_year_sha=None, yoy_headline_pct=None,
                  cf_year_ts=_ts("2026-12-09", "15:35"), cf_month_ts=_ts("2026-12-09", "15:35"), books_paired=0)  # fmt: skip
    snap = tmp_path / "snapshots" / "20261209T1600Z"
    snap.mkdir(parents=True)
    (snap / "books_e1.json").write_text(json.dumps([{"asset_id": t, "bids": [{"price": "0.90", "size": "9"}],
                                                       "asks": [{"price": "0.95", "size": "9"}]} for t in picks]))  # fmt: skip
    db = make_db(tmp_path / "db.sqlite", t1)
    assert f.row_why(t1, "2026-12-09", "2026-11") == "no yoy nowcast"
    assert write("T-1", datetime(2026, 12, 9, 21, 4, tzinfo=UTC), tmp_path, db) == 1  # early
    assert write("T-1", AT_T1, tmp_path, db) == 0
    rec = rec_of(tmp_path, "T-1")
    assert rec["t3_file"]["kind"] == "docs" and rec["source"]["day"] == "2026-12-09"
    by = {c["token"]: c for c in rec["clv"]}
    assert set(by) == set(picks)
    b38 = next(b for b in brs if b["title"] == "3.8%")
    c = by[b38["token"]]
    assert (c["bid"], c["ask"], c["why"]) == (b38["bid"], 0.15, None)
    want = clv(picks[b38["token"]]["p_in"], picks[b38["token"]]["shares"], b38["bid"], 0.15)
    assert c["clv"]["usd"] == pytest.approx(want["usd"]) and c["clv"]["mid"] == pytest.approx((b38["bid"] + 0.15) / 2)
    assert rec["quotes"][b38["token"]] == {"bid": b38["bid"], "ask": 0.15, "why": None}
    assert "decision" not in rec and "20261209T1600Z" not in json.dumps(rec) and "snapshots/" not in json.dumps(rec)
    assert all(c["clv"]["mid"] < 0.9 for c in rec["clv"])  # the snapshot's 0.90 / 0.95 books were never used
    assert f.t1_check(rec, f.read_row(db, "2026-12-09")) == (True, True)
    assert f.t1_check(rec, {**t1, "updated_ts": 1}) == (False, False)  # another row: the DB row applies (п. 8)


def test_t1_without_a_row_has_no_clv_and_t7_is_a_record_without_decision(tmp_path):
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-03"))
    calls: list = []
    assert write("T-7", AT_T7, tmp_path, db, gamma_rt(calls=calls)) == 0
    rec = rec_of(tmp_path, "T-7")
    assert rec["mu"] == 3.79 and len(rec["quotes"]) == 18 and "decision" not in rec and "gamma" not in rec
    assert sorted(calls) == ["/events/e1", "/events/e2", "/events/e3"] and rec["missing"] == []  # titles only
    assert {e["event_id"]: e["title"] for e in rec["events"]} == EVENT_TITLES
    assert write("T-1", AT_T1, tmp_path, db) == 0
    rec = rec_of(tmp_path, "T-1")
    assert rec["source"]["why"] == "no row" and rec["quotes"] == {} and rec["clv"] is None and rec["t3_file"]["kind"] is None


# ------------------------------------------------------------------------------------------- the result (item 491)
# Synthetic outcomes only: the real samples (a resolved October 2026 market, the FRED tail of 2026-11-27) give the
# response shapes; the CPI of 2026-11 and every payout below are made up (no December price is compared with anything).

RESOLVED = json.loads((REPO_ROOT / "tests" / "fixtures" / "polymarket_cpi_resolved_market_2026-11-27.json").read_text())["market"]
FRED_TAIL = json.loads((REPO_ROOT / "tests" / "fixtures" / "fred_cpiaucns_tail_2026-11-27.json").read_text())["csv"]
CSV_NOV = FRED_TAIL + "2026-11-01,336.000\n"  # synthetic level: 336.000 / 324.122 - 1 = 3.6647 % -> 3.7
AT_RESULT = datetime(2026, 12, 10, 15, 30, tzinfo=UTC)


def resolved(yes="3.8%", eid="e1"):
    """Resolved /markets/{id} answers (the real resolved sample's shape) of the YoY event: YES pays on ``yes``."""
    return {f"{eid}m{i}": {**RESOLVED, "id": f"{eid}m{i}", "outcomePrices": '["1", "0"]' if t == yes else '["0", "1"]'}
            for i, t in enumerate(TITLES)}  # fmt: skip


def fred(csv=CSV_NOV, down=False, calls=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req.url.path + "?" + req.url.query.decode())
        return httpx.Response(500, text="down") if down else httpx.Response(200, content=csv.encode())

    return lambda: Http("fred", f.FRED_URL, retries=0, backoff_base=0.0, transport=httpx.MockTransport(handler))


def write_res(at, tmp_path, db, rt_factory, fred_factory, code_sha="HEAD", r="HEAD"):
    """The result stage in this process (``r`` = ``code_sha``: as `launch` runs it, the tree of R; the selection of R
    itself is tested with the git world below)."""
    return f.write_stage("result", R, at, db=db, root=tmp_path / "state", docs=tmp_path / "docs", repo=REPO_ROOT,
                         code_sha=code_sha, m0=None, tree=REPO_ROOT, rt_factory=rt_factory, fred_factory=fred_factory,
                         r=r)  # fmt: skip


def test_result_waits_until_every_input_is_final_then_writes_once(tmp_path, capsys):
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"), synth_row("2026-12-09", asks={"3.8%": 0.15, "3.7%": 0.09}))
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt()) == 0
    assert write("T-1", AT_T1, tmp_path, db) == 0
    t3 = rec_of(tmp_path, "T-3")
    assert t3["decision"]["fills"]
    calls: list = []
    assert write_res(datetime(2026, 12, 10, 14, 59, 59, tzinfo=UTC), tmp_path, db, gamma_rt(resolved(), calls=calls), fred()) == 1
    assert "outside its window (early)" in capsys.readouterr().out and calls == []
    # not final: nothing written, every reason printed (the stage is run again later)
    for rt, fr, want in ((gamma_rt(resolved()), fred(down=True), "not final: cpi: request failed"),
                         (gamma_rt(resolved()), fred(FRED_TAIL), "not final: cpi: not published"),
                         (gamma_rt(), fred(), "not final: polymarket e1m0: open"),
                         (gamma_rt({**resolved(), "e1m3": {**RESOLVED, "id": "e1m3", "umaResolutionStatus": "proposed"}}), fred(),
                          "not final: polymarket e1m3: pending"),
                         (gamma_rt(down=True), fred(), "not final: polymarket e1m11: request failed")):  # fmt: skip
        assert write_res(AT_RESULT, tmp_path, db, rt, fr) == 1
        out = capsys.readouterr().out
        assert want in out and "not written: result" in out, out
        assert not (tmp_path / "state" / R / "result.json").exists() and not (tmp_path / "docs" / R / "result.json").exists()
    fred_calls: list = []
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved(), calls=calls), fred(calls=fred_calls)) == 0
    out = capsys.readouterr().out.splitlines()
    raw = (tmp_path / "state" / R / "result.json").read_bytes()
    assert (tmp_path / "docs" / R / "result.json").read_bytes() == raw
    rec = json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-strict JSON literal {c}"))
    assert sorted(calls) == sorted(f"/markets/e1m{i}" for i in range(12))  # every market of the YoY event, nothing else
    assert fred_calls == ["/graph/fredgraph.csv?id=CPIAUCNS"]
    assert rec["stage"] == "result" and rec["D"] == AT_RESULT.isoformat() and "pending" not in rec
    assert rec["cpi"]["value"] == 3.7 and (rec["cpi"]["level"], rec["cpi"]["level_12m_before"]) == ("336.000", "324.122")
    assert rec["cpi"]["csv_sha256"] == hashlib.sha256(CSV_NOV.encode()).hexdigest()
    # both values written: Polymarket paid 3.8 %, the published CPI is 3.7 %; the payout of Polymarket counts
    assert rec["resolution_check"] == [{"event_id": "e1", "pm_yes": ["e1m5"], "cpi_yes": ["e1m4"], "agree": False}]
    assert rec["resolution"]["e1m5"]["yes"] == 1.0 and rec["resolution"]["e1m4"]["yes"] == 0.0
    pay = {f"e1m{i}": 1.0 if t == "3.8%" else 0.0 for i, t in enumerate(TITLES)}
    assert f.same_record(rec["brier_t3"], json.loads(json.dumps(brier_release(t3["decision"]["rule_signals"], pay))))
    by_cpi = {f"e1m{i}": 1.0 if t == "3.7%" else 0.0 for i, t in enumerate(TITLES)}
    assert rec["brier_t3"]["model"] != pytest.approx(brier_release(t3["decision"]["rule_signals"], by_cpi)["model"])
    assert rec["brier_t3"]["n_events"] == 1 and rec["brier_t3"]["delta"] is not None
    # CLV$ from close_quote of the T-1 row (bid 0.14 / ask 0.15 for 3.8 %), the paper result from the payout
    sig = {s["title"]: s for s in rec["signals"]}
    fl = {x["title"]: x for x in t3["decision"]["fills"]}
    s38 = sig["3.8%"]
    assert (s38["bid_t1"], s38["ask_t1"], s38["payout"], s38["why"]) == (0.14, 0.15, 1.0, None)
    assert s38["clv"]["usd"] == pytest.approx(fl["3.8%"]["shares"] * ((0.14 + 0.15) / 2 - fl["3.8%"]["p_in"]))
    assert s38["paper_usd"] == pytest.approx(fl["3.8%"]["shares"] - fl["3.8%"]["cost_usd"])
    assert sig["3.7%"]["paper_usd"] == pytest.approx(-fl["3.7%"]["cost_usd"])
    ex = [s for s in rec["signals"] if s["executable"]]
    assert rec["clv_rel"] == {"usd": pytest.approx(sum(s["clv"]["usd"] for s in ex)), "n": len(ex)}
    assert rec["t1_check"] == {"row_digest_db": True, "quotes": True} and rec["t1_file"]["kind"] == "docs"
    assert rec["t1_row_digest"] == row_digest(f.read_row(db, "2026-12-09"))
    t3_raw = (tmp_path / "docs" / R / "T-3.json").read_bytes()
    assert rec["t3_file"]["kind"] == "docs" and rec["t3_file"]["sha256"] == hashlib.sha256(t3_raw).hexdigest()
    assert rec["absent"] == [] and rec["missing"] == [] and rec["markets_from"] == "T-3 record"
    # the rule text at the resolution (the real sample's) is not the one Gamma gave at T-3 (RULES): shown
    assert all(r["rules_changed_since_t3"] is True and r["phrases"]["one decimal"] for r in rec["resolution"].values())
    assert rec["code_sha"] == rec["R"] == "HEAD" and "M0" not in rec and rec["CODE_PATHS"] == list(f.CODE_PATHS)
    assert rec["db"] == str(db) and rec["cutoff"] == "2027-01-09T15:00:00+00:00" and rec["invalid"] is None  # R3, blocker 2
    # R1: the exit figure takes off both taker fees (entry, and the exit at the T-1 bid by the bracket's fee schedule)
    rs = {r["token"]: r for r in t3["decision"]["rule_signals"]}
    out_fee = f.taker_fee(0.14, "economics_fees", rs[s38["token"]]["fee_schedule"], rs[s38["token"]]["fees_enabled"])
    assert s38["exit_fee_usd"] == pytest.approx(s38["shares"] * out_fee) and s38["exit_fee_usd"] > 0
    assert s38["exit_usd"] == pytest.approx(s38["shares"] * (0.14 - s38["p_in"]) - s38["fee_usd"] - s38["exit_fee_usd"])
    line = out[out.index(f.JOURNAL_NOTE) + 1]
    assert line.startswith(f"{R} result · D {AT_RESULT.isoformat()} · R HEAD · CPI 2026-11 3.7 % (FRED CPIAUCNS)")
    assert f.journal_sha(CARD_TEXT.format(lines=line + "\n"), R, "result") == (hashlib.sha256(raw).hexdigest(), None)
    # written once
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved()), fred()) == 1 and "exists" in capsys.readouterr().out
    assert (tmp_path / "state" / R / "result.json").read_bytes() == raw


def test_result_without_a_t3_record_or_a_t1_row_writes_what_can_never_come_as_absent(tmp_path):
    # no T-3 record: the markets of the YoY events of the database row of day T-3 (set B, п. 8 (в), needs their outcome)
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"))
    calls: list = []
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved(), calls=calls), fred()) == 0
    rec = rec_of(tmp_path, "result")
    assert sorted(calls) == sorted(f"/markets/e1m{i}" for i in range(12)) and rec["markets_from"] == "database row of day T-3"
    assert rec["absent"] == ["t3: no T-3 record (markets from the database row of day T-3)"] and rec["t3_file"]["kind"] is None
    assert rec["brier_t3"] is None and rec["signals"] == [] and rec["clv_rel"] == {"usd": None, "n": 0}
    assert rec["t1_source"] == {"day": "2026-12-09", "why": "no row"} and rec["t1_check"] is None
    assert rec["resolution_check"][0]["pm_yes"] == ["e1m5"]
    # no row at all: no market to ask, the CPI is enough
    empty = tmp_path / "e"
    empty.mkdir()
    db2 = make_db(empty / "db.sqlite")
    assert write_res(AT_RESULT, empty, db2, gamma_rt(down=True), fred()) == 0
    rec = rec_of(empty, "result")
    assert rec["resolution"] == {} and rec["cpi"]["value"] == 3.7 and rec["markets_from"] is None
    assert rec["absent"] == ["t3: no T-3 record (markets from the database row of day T-3)", "no YoY event (no market asked)"]


def test_result_reads_only_the_rebuilt_fields_and_the_gamma_part(tmp_path):
    rec = {**dict.fromkeys(f.T3_KEYS, None), "note": "an extra key"}
    assert set(f.t3_part(rec)) == {*f.REBUILT, "gamma", "gamma_events"} and f.t3_part(None) is None
    # an event left out only by the rule phrases (set B «rules») is still a YoY event of the release for the result
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"))
    answers = {f"e1m{i}": gamma_answer(f"e1m{i}", desc="12-month change, one decimal") for i in range(12)}
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt(answers)) == 0
    t3 = rec_of(tmp_path, "T-3")
    assert t3["skip"] == "no YoY event" and f.b_why(t3) == "rules"
    assert [b["market_id"] for b in f.yoy_brackets(f.t3_part(t3))] == [f"e1m{i}" for i in range(12)]


CUTOFF = datetime(2027, 1, 9, 15, 0, tzinfo=UTC)  # 15:00 UTC of 10.12 + 30 days (registration п. 7, result)


def _staged(tmp_path):
    """T-3 (with two signals, 3.8 % = e1m5 and 3.7 % = e1m4) and T-1 written; the database."""
    db = make_db(tmp_path / "db.sqlite", synth_row("2026-12-07"), synth_row("2026-12-09", asks={"3.8%": 0.15, "3.7%": 0.09}))
    assert write("T-3", AT_T3, tmp_path, db, gamma_rt()) == 0 and write("T-1", AT_T1, tmp_path, db) == 0
    assert {x["title"] for x in rec_of(tmp_path, "T-3")["decision"]["fills"]} == {"3.8%", "3.7%"}
    return db


def test_result_refuses_a_run_not_by_the_code_of_r(tmp_path, capsys):
    # review C of item 491, blocker 1: written only by the tree `launch` extracts for R (code_sha = R), never by HEAD
    db = _staged(tmp_path)
    capsys.readouterr()
    for code_sha, r in (("HEAD", None), ("HEAD", ""), ("abc", "def")):
        assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved()), fred(), code_sha=code_sha, r=r) == 1
        assert "runs only by the code of R" in capsys.readouterr().out
    assert not (tmp_path / "state" / R / "result.json").exists()


def test_result_from_the_cutoff_writes_what_did_not_come_under_absent(tmp_path, capsys):
    # review C of item 491, blocker 2: a market that never resolves cannot hold the file back past the cutoff
    db = _staged(tmp_path)
    t3 = rec_of(tmp_path, "T-3")
    stuck = {**resolved(), "e1m4": {**RESOLVED, "id": "e1m4", "umaResolutionStatus": "proposed"},  # 3.7 %, a signal
             "e1m7": {**RESOLVED, "id": "e1m8"},  # R5: an answer of another market
             "e1m9": None}  # HTTP 404: an answer, the market is not there  # fmt: skip
    for at in (AT_RESULT, CUTOFF - timedelta(seconds=1)):
        assert write_res(at, tmp_path, db, gamma_rt(stuck), fred()) == 1
        out = capsys.readouterr().out
        for want in ("not final: polymarket e1m4: pending", "not final: polymarket e1m7: not this market",
                     "not final: polymarket e1m9: not found", "from the cutoff 2027-01-09T15:00:00+00:00"):  # fmt: skip
            assert want in out, out
    # a failed request is no answer: it waits after the cutoff too
    assert write_res(CUTOFF, tmp_path, db, gamma_rt(down=True), fred()) == 1
    assert "not final: polymarket e1m0: request failed" in capsys.readouterr().out
    assert not (tmp_path / "state" / R / "result.json").exists()
    assert write_res(CUTOFF, tmp_path, db, gamma_rt(stuck), fred()) == 0
    rec = rec_of(tmp_path, "result")
    by = "by 2027-01-09T15:00:00+00:00"
    assert rec["absent"] == [f"polymarket e1m4: not resolved {by} (pending; its event has no Brier, no payout)",
                             f"polymarket e1m7: not resolved {by} (not this market; its event has no Brier, no payout)",
                             f"polymarket e1m9: not resolved {by} (not found; its event has no Brier, no payout)"]  # fmt: skip
    assert rec["invalid"] is None and rec["cpi"]["value"] == 3.7
    # the event of those markets has no Brier ('no payout', registration п. 7 (ґ), R8); the payout is never filled in
    assert rec["brier_t3"]["events"][0]["why"] == "no payout" and rec["brier_t3"]["n_events"] == 0
    assert rec["brier_t3"]["model"] is None and rec["brier_t3"]["delta"] is None
    sig = {s["title"]: s for s in rec["signals"]}
    assert sig["3.7%"]["payout"] is None and sig["3.7%"]["paper_usd"] is None and sig["3.7%"]["clv"] is not None
    assert sig["3.8%"]["payout"] == 1.0 and rec["clv_rel"]["n"] == sum(1 for x in t3["decision"]["fills"] if x["executable"])
    assert rec["resolution"]["e1m7"] == {"ok": False, "why": "not this market", "event_id": "e1", "title": "4.0%",
                                         "token": rec["resolution"]["e1m7"]["token"], "rules_changed_since_t3": None}  # fmt: skip


def test_result_cpi_not_published_by_the_cutoff_makes_the_release_invalid(tmp_path, capsys):
    # review C of item 491, blocker 2, scenario 3 (as 10.2025): FRED answers without the target month
    db = _staged(tmp_path)
    capsys.readouterr()
    for fr, want in ((fred(FRED_TAIL), "not final: cpi: not published"), (fred(down=True), "not final: cpi: request failed"),
                     (fred("<html>maintenance</html>"), "not final: cpi: unreadable")):  # fmt: skip
        assert write_res(CUTOFF - timedelta(seconds=1), tmp_path, db, gamma_rt(resolved()), fr) == 1
        assert want in capsys.readouterr().out
    for fr in (fred(down=True), fred("<html>maintenance</html>")):  # no answer: it waits after the cutoff too
        assert write_res(CUTOFF, tmp_path, db, gamma_rt(resolved()), fr) == 1
    assert write_res(CUTOFF, tmp_path, db, gamma_rt(resolved()), fred(FRED_TAIL)) == 0
    rec = rec_of(tmp_path, "result")
    assert rec["invalid"] == "CPI not published (registration п. 5)" and rec["cpi"]["value"] is None
    assert rec["absent"] == ["cpi: not published by 2027-01-09T15:00:00+00:00 (the release is invalid, registration п. 5)"]
    assert rec["resolution_check"][0]["cpi_yes"] == [] and rec["brier_t3"]["n_events"] == 1  # Polymarket's payout counts
    line = capsys.readouterr().out.splitlines()
    assert "реліз недійсний: CPI not published (registration п. 5)" in line[line.index(f.JOURNAL_NOTE) + 1]


def test_result_clv_comes_from_the_database_row_not_from_t1_json(tmp_path):
    # review C of item 491, R5 (registration п. 8): T-1.json with other quotes changes nothing; t1_check says so
    db = _staged(tmp_path)
    p = tmp_path / "docs" / R / "T-1.json"
    t1 = json.loads(p.read_bytes())
    t1["quotes"] = {k: {**q, "bid": 0.01, "ask": 0.02} for k, q in t1["quotes"].items()}
    p.write_bytes(f.dump(t1))
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved()), fred()) == 0
    rec = rec_of(tmp_path, "result")
    assert rec["t1_check"] == {"row_digest_db": True, "quotes": False} and rec["t1_file"]["kind"] == "docs"
    s38 = next(s for s in rec["signals"] if s["title"] == "3.8%")
    assert (s38["bid_t1"], s38["ask_t1"]) == (0.14, 0.15)  # the T-1 row of the database
    assert s38["clv"]["usd"] == pytest.approx(s38["shares"] * ((0.14 + 0.15) / 2 - s38["p_in"]))


@pytest.mark.parametrize(("stage", "raw", "err"), [("T-1", b"not json", "JSONDecodeError"), ("T-1", b"[1, 2]", "not an object"),
                                                   ("T-3", b"not json", "JSONDecodeError"), ("T-3", b"[1, 2]", "not an object")])  # fmt: skip
def test_result_with_an_unreadable_stage_file_is_written_and_says_so(tmp_path, stage, raw, err):
    # review C of item 491, R4: neither unwritable nor taken for a missing file; N1 (b): a clone's copy that does not
    # read falls back to the local file, and both are recorded
    db = _staged(tmp_path)
    key = {"T-1": "t1_file", "T-3": "t3_file"}[stage]
    p, local = tmp_path / "docs" / R / f"{stage}.json", tmp_path / "state" / R / f"{stage}.json"
    good = local.read_bytes()
    p.write_bytes(raw)
    bad = {"kind": "docs", "path": str(p), "sha256": hashlib.sha256(raw).hexdigest(), "error": err}
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved()), fred()) == 0
    rec = rec_of(tmp_path, "result")
    assert rec[key] == {"kind": "local", "path": str(local), "sha256": hashlib.sha256(good).hexdigest(), "error": None, "also": [bad]}
    assert rec["absent"] == [] and rec["markets_from"] == "T-3 record" and rec["clv_rel"]["n"] > 0
    assert rec["t1_check"] == {"row_digest_db": True, "quotes": True}
    # no copy reads: the error of the clone's copy, the other listed; the file is written all the same
    for out in (tmp_path / "state" / R / "result.json", tmp_path / "docs" / R / "result.json"):
        out.unlink()
    local.write_bytes(raw)
    assert write_res(AT_RESULT, tmp_path, db, gamma_rt(resolved()), fred()) == 0
    rec = rec_of(tmp_path, "result")
    assert rec[key] == {**bad, "also": [{**bad, "kind": "local", "path": str(local)}]}
    if stage == "T-1":
        assert rec["t1_check"] == {"unreadable": f"unreadable ({err})"} and rec["clv_rel"]["n"] > 0  # CLV from the row
    else:
        assert rec["absent"] == [f"t3: T-3.json unreadable ({err}; markets from the database row of day T-3)"]
        assert rec["markets_from"] == "database row of day T-3" and rec["signals"] == [] and rec["brier_t3"] is None


# ------------------------------------------------------------------------------------------------------ subprocesses


def test_code_paths_are_every_module_the_scanner_imports(tmp_path):
    # item 484 (c), registration п. 7: the scanner run in a subprocess on the fixture (Gamma unreachable, so the
    # request path runs too); every tradedesk.* and scanner module in sys.modules must be one of CODE_PATHS and back
    db = make_db(tmp_path / "db.sqlite", fixture_row("2026-12-07"), fixture_row("2026-12-09"))
    found, foreign = {}, []
    for stage, at in (("T-3", AT_T3), ("T-1", AT_T1)):
        args = ["--stage", stage, "--release", R, "--at", at.isoformat(), "--code-sha", "HEAD", "--tree", str(REPO_ROOT),
                "--repo", str(REPO_ROOT), "--db", str(db), "--root", str(tmp_path / "state"), "--docs", str(tmp_path / "docs"),
                "--http-retries", "0"]  # fmt: skip
        res = f.run_tree(REPO_ROOT, args, REPO_ROOT, offline_env())
        assert res.returncode == 0, res.stdout + res.stderr
        rec = rec_of(tmp_path, stage)
        found[stage] = set(rec["modules"])
        foreign += [x for x in rec["code_dirty"].splitlines() if x[:1] in "XO"]  # imported, not in CODE_PATHS / the tree
    t3 = rec_of(tmp_path, "T-3")
    assert t3["skip"] is None and t3["missing"] and t3["decision"] is not None  # Gamma was asked (and failed offline)
    want = "CODE_PATHS = (\n" + "".join(f'    "{p}",\n' for p in sorted(found["T-3"], key=_order)) + ")"
    assert found["T-3"] == set(f.CODE_PATHS), f"regenerate the constant in scripts/pm_cpi_forward.py:\n{want}"
    assert found["T-1"] <= set(f.CODE_PATHS) and foreign == []
    assert list(f.CODE_PATHS) == sorted(f.CODE_PATHS, key=_order)
    # item 491: the result stage (FRED and Gamma unreachable, so it writes nothing) imports nothing outside CODE_PATHS
    snippet = ("import json, pathlib, sys; t = sys.argv[1]; sys.path[:0] = [t + '/src', t + '/scripts']; import pm_cpi_forward as m; "
               "rc = m.inner(sys.argv[2:]); print(json.dumps([rc, m.loaded_modules(pathlib.Path(t))]))")  # fmt: skip
    args = ["--stage", "result", "--release", R, "--at", "2026-12-10T15:30:00+00:00", "--code-sha", "HEAD", "--r", "HEAD",
            "--tree", str(REPO_ROOT), "--repo", str(REPO_ROOT), "--db", str(db), "--root", str(tmp_path / "state"),
            "--docs", str(tmp_path / "docs"), "--http-retries", "0"]  # fmt: skip
    res = subprocess.run([sys.executable, "-I", "-B", "-c", snippet, str(REPO_ROOT), *args], cwd=REPO_ROOT, capture_output=True,
                         text=True, env=offline_env())  # fmt: skip
    rc, (inside, outside) = json.loads(res.stdout.splitlines()[-1])
    assert rc == 1 and "not final: cpi: request failed" in res.stdout, res.stdout + res.stderr
    assert "not final: polymarket" in res.stdout and set(inside) <= set(f.CODE_PATHS) and outside == []


def _order(p):
    return (not p.startswith("scripts/"), p)


def _git(repo, *args, when="2026-12-07T20:00:00Z", who=("agent", "agent@example.invalid"), home=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@example.invalid", "GIT_COMMITTER_NAME": who[0],
           "GIT_COMMITTER_EMAIL": who[1], "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "HOME": str(home)}  # fmt: skip
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


CARD_TEXT = "# card\n\n## Форвард-журнал\nРядки етапів.\n\n{lines}\n## Вердикт\n"


def fetched(repo, when):
    """A FETCH_HEAD in ``repo`` with mtime ``when`` (ISO): what `git fetch` leaves."""
    p = repo / ".git" / "FETCH_HEAD"
    p.write_text("0" * 40 + "\t\tbranch 'main' of origin\n")
    ts = datetime.fromisoformat(when).timestamp()
    os.utime(p, (ts, ts))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A git repository whose main has M0 (07.12 20:00, the scanner's CODE_PATHS) and a later commit changing
    core/http.py (07.12 21:30); T-3 written by `launch` from the M0 tree (subprocess, Gamma unreachable)."""
    base = tmp_path_factory.mktemp("fwd")
    home = base / "home"
    home.mkdir()
    (home / "gitconfig").write_text("")
    repo = base / "repo"
    repo.mkdir()
    g = lambda *a, **kw: _git(repo, *a, home=home, **kw)  # noqa: E731
    g("init", "-q", "-b", "main")
    for p in f.CODE_PATHS:
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / p, repo / p)
    (repo / f.CARD).parent.mkdir(parents=True, exist_ok=True)
    (repo / f.CARD).write_text(CARD_TEXT.format(lines="(записів ще нема)\n"))
    g("add", "-A")
    g("commit", "-q", "-m", "M0")
    m0 = g("rev-parse", "HEAD")
    http = repo / "src/tradedesk/core/http.py"
    http.write_text(http.read_text() + "\n# a merge after M0\n")
    g("commit", "-q", "-am", "after M0", when="2026-12-07T21:30:00Z")
    after = g("rev-parse", "HEAD")
    # a local commit before 21:00 with M0's CODE_PATHS and another card (review C 484, blocker 1: «чужий --ref»)
    g("checkout", "-q", "-b", "side", m0)
    (repo / f.CARD).write_text(CARD_TEXT.format(lines="V1 · fail: chosen\n"))
    g("commit", "-q", "-am", "side", when="2026-12-07T20:30:00Z")
    side = g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    g("update-ref", "refs/remotes/origin/main", after)
    fetched(repo, "2026-12-07T21:30:00+00:00")  # the launcher's `git fetch` at the start of step 486
    db = make_db(base / "db.sqlite", synth_row("2026-12-07"), synth_row("2026-12-09"))
    db2 = make_db(base / "db2.sqlite", synth_row("2026-12-07", updated_ts=1), synth_row("2026-12-09", updated_ts=1))
    out: list[str] = []
    rc = f.launch("T-3", R, AT_T3, repo=repo, ref="origin/main", db=db, root=base / "state", docs=base / "docs",
                  tmp=base / "tmp", http_retries=0, env=offline_env())  # fmt: skip
    raw = (base / "state" / R / "T-3.json").read_bytes()
    fetched(repo, "2026-12-10T15:30:00+00:00")  # the `git fetch` of the proof run (item 487, from 10.12 15:00)
    return SimpleNamespace(base=base, home=home, repo=repo, g=g, m0=m0, after=after, side=side, db=db, db2=db2, raw=raw, rc=rc,
                           out=out)  # fmt: skip


def _case(w, name, content, *, journal=None, merge_by=GH, merge_at="2026-12-07T22:30:00Z", branch_edits=0, change_after=False,
          base=None):  # fmt: skip
    g, path, base = w.g, w.repo / f.DOCS_DIR / R / "T-3.json", base or w.after
    g("checkout", "-q", "-B", f"br-{name}", base)
    path.parent.mkdir(parents=True, exist_ok=True)
    for k in range(branch_edits):
        path.write_bytes(content + b" " * (k + 1))
        g("add", "-A")
        g("commit", "-q", "-m", f"edit {k}", when="2026-12-07T22:12:00Z")
    sha = journal or hashlib.sha256(content).hexdigest()
    line = f"{R} T−3 · D {AT_T3.isoformat()} · джерело рядок 2026-12-07 · без сигналу · sha256 {sha}\n"
    (w.repo / f.CARD).write_text(CARD_TEXT.format(lines=line))
    path.write_bytes(content)
    g("add", "-A")
    g("commit", "-q", "-m", "T-3", when="2026-12-07T22:15:00Z")
    g("checkout", "-q", "-B", f"mn-{name}", base)
    g("merge", "-q", "--no-ff", "--no-edit", f"br-{name}", when=merge_at, who=merge_by)
    if change_after:
        path.write_bytes(content + b"\n")
        g("commit", "-q", "-am", "later", when="2026-12-09T20:00:00Z", who=merge_by)
    g("update-ref", f"refs/remotes/{name}/main", "HEAD")
    g("checkout", "-q", "--detach", w.after)
    return f"{name}/main"


def _ask(inp, title, ask):
    """The decision input with the ask of the bracket ``title`` set to ``ask`` (and its mid)."""
    inp = json.loads(json.dumps(inp))
    for g in inp["events"]:
        for b in g:
            if b["title"] == title:
                b["ask"], b["mid"] = ask, (b["bid"] + ask) / 2 if b["bid"] is not None else None
    return inp


DROP = object()


def _edit(raw, **kw):
    rec = json.loads(raw)
    for k, v in kw.items():
        if v is DROP:
            del rec[k]
        else:
            rec[k] = v(rec) if callable(v) else v
    return f.dump(rec)


def prove(w, ref, **kw):
    """``--stage proof`` with the registered ref (R5: origin/main only), origin/main of the world pointed at ``ref`` for
    this one run and put back after."""
    w.g("update-ref", "refs/remotes/origin/main", ref)
    try:
        return f.proof(R, repo=w.repo, ref="origin/main", **kw)
    finally:
        w.g("update-ref", "refs/remotes/origin/main", w.after)


def test_t3_runs_the_code_of_m0_and_its_proof_passes(world, tmp_path, capsys):
    # fourth review B 481, finding 1: main changed a CODE_PATHS file after M0, the record stays valid
    assert world.rc == 0
    rec = json.loads(world.raw)
    assert rec["code_sha"] == rec["M0"] == world.m0 and rec["code_dirty"] == "" and rec["skip"] is None
    assert set(rec["modules"]) == set(f.CODE_PATHS) and rec["decision"]["fills"]
    assert tuple(sorted(rec)) == f.T3_KEYS  # item 490, R3: the key set the proof demands is the one written
    assert f.m0_of(world.repo, "origin/main", R) == world.m0
    ref = _case(world, "pass", world.raw)
    capsys.readouterr()
    assert prove(world, ref, db=world.db, root=tmp_path, tmp=tmp_path) is True
    out = capsys.readouterr().out.rstrip().splitlines()
    assert out[-1] == "proof pass", out
    # finding 8: the first line names the arguments, the ref with its sha
    assert out[0].startswith(f"{R} proof args · repo {world.repo} · ref origin/main = ") and f"db {world.db}" in out[0]
    assert out[1] == "  args registered"
    assert any("row_digest = БД так" in x for x in out)
    assert any("перерахунок = записаному так · перебудова з рядка БД = записаному так · Gamma ok · ключі = M0 так" in x
               for x in out), out  # fmt: skip
    assert "  set B: not rules (descriptive)" in out  # review C 484 / 490, finding 4: descriptive, the verdict stays
    assert any("T-1: no file" in x for x in out)
    # T-1 checked against the database (finding 3): the verdict stays, the line says which applies
    assert f.write_stage("T-1", R, AT_T1, db=world.db, root=tmp_path / "s", docs=tmp_path / "d", repo=world.repo,
                         code_sha=world.after, m0=None, tree=world.repo, rt_factory=gamma_rt()) == 0  # fmt: skip
    capsys.readouterr()
    assert prove(world, ref, db=world.db, root=tmp_path / "s", tmp=tmp_path) is True
    assert any("T-1 (local): row_digest = БД так · close quotes = recomputed так" in x for x in capsys.readouterr().out.splitlines())
    assert prove(world, ref, db=world.db2, root=tmp_path / "s", tmp=tmp_path) is False  # T-3 row differs
    out = capsys.readouterr().out
    assert "T-1 (local): row_digest = БД ні" in out and "the DB row applies" in out


@pytest.mark.parametrize(
    ("name", "build", "db2", "want"),
    [("absent", None, False, "proof fail: not merged (not merged)"),
     ("late", {"merge_at": "2026-12-09T12:00:01Z"}, False, "not merged (late)"),
     ("notgh", {"merge_by": ("agent", "agent@example.invalid")}, False, "not merged (not a GitHub merge)"),
     ("after", {"change_after": True}, False, "not merged (changed after)"),
     ("journal", {"journal": "0" * 64}, False, "not merged (sha256 ≠ journal"),
     ("branch", {"branch_edits": 1}, False, "not merged (edited on the way to main: 2 commits)"),
     ("dbrow", {}, True, "mismatch (row_digest ≠ DB row)"),
     ("preimage", {"edit": {"digest_preimage": lambda r: {**r["digest_preimage"], "late": 1}}}, False,
      "mismatch (row_digest ≠ its preimage)"),
     ("recompute", {"edit": {"decision": lambda r: {**r["decision"], "fills": [{**r["decision"]["fills"][0], "shares": 1.0},
                                                                              *r["decision"]["fills"][1:]]}}}, False,
      "mismatch (recompute)"),
     ("dirty", {"edit": {"code_dirty": "M src/tradedesk/core/http.py"}}, False, "code (code_dirty not empty)"),
     ("codesha", {"edit": {"code_sha": "AFTER"}}, False, "code (code_sha ≠ M0)"),
     # review C 484, blocker 1 (b): a commit before 21:00 with M0's CODE_PATHS (git diff --quiet = 0) and another card
     ("foreign", {"edit": {"code_sha": "SIDE", "M0": "SIDE"}}, False, "code (code_sha ≠ M0, M0 field ≠ M0)"),
     ("paths", {"edit": {"CODE_PATHS": lambda r: r["CODE_PATHS"][:-1]}}, False, "code (CODE_PATHS ≠ those of M0)"),
     ("window", {"edit": {"D": "2026-12-07T21:00:00+00:00"}}, False, "window (early)"),
     # finding 9: D inside the window but after the merge commit (22:30 07.12)
     ("dafter", {"edit": {"D": "2026-12-08T10:00:00+00:00"}}, False, "window (D after the merge)"),
     # blocker 1, scenario A: the release hidden as a skip with a data reason
     ("skipmodel", {"edit": {"skip": "model", "decision_input": None, "decision": None}}, False,
      "mismatch (rebuild: decision_input, decision, skip)"),
     ("skipnorow", {"edit": {"skip": "no row", "decision_input": None, "decision": None}}, False,
      "mismatch (rebuild: decision_input, decision, skip)"),
     # blocker 1, scenario B: an ask of the decision input changed and the decision recomputed by the same decide
     ("input", {"edit": {"decision_input": lambda r: _ask(r["decision_input"], "3.9%", 0.05),
                         "decision": lambda r: f.decide(r["decision_input"])}}, False,
      "mismatch (rebuild: decision_input, decision)"),
     ("title", {"edit": {"events": lambda r: [{**e, "title": "x"} for e in r["events"]]}}, False, "mismatch (rebuild: events)"),
     ("gammakeys", {"edit": {"gamma": lambda r: dict(list(r["gamma"].items())[1:])}}, False,
      "mismatch (gamma: gamma keys ≠ asked (11 vs 12))"),
     ("missing", {"edit": {"missing": lambda r: r["missing"][1:]}}, False, "mismatch (gamma: gamma e1m0: request failed ≠ missing)"),
     # item 490, R3: a key added or dropped before `git add` (both passed before)
     ("extrakey", {"edit": {"note": "read me"}}, False, "mismatch (keys)"),
     ("dropkey", {"edit": {"modules": DROP}}, False, "mismatch (keys)"),
     ("raised", {"raw": b"not json"}, False, "proof fail: raised JSONDecodeError")],
)  # fmt: skip
def test_proof_fails_for_every_reason(world, tmp_path, capsys, name, build, db2, want):
    if build is None:
        ref = world.after
    else:
        build = dict(build)
        raw = build.pop("raw", world.raw)
        subst = {"AFTER": world.after, "SIDE": world.side}
        edit = {k: subst.get(v, v) if isinstance(v, str) else v for k, v in (build.pop("edit", {}) or {}).items()}
        ref = _case(world, name, _edit(raw, **edit) if edit else raw, **build)
    capsys.readouterr()
    assert prove(world, ref, db=world.db2 if db2 else world.db, root=tmp_path, tmp=tmp_path) is False
    out = capsys.readouterr().out.rstrip().splitlines()
    last = out[-1]
    assert last.startswith("proof fail: ") and want in last, last
    if name in ONLY:  # this reason alone; for all but codesha / dafter it is a check the proof lacked before review C 484,
        assert last == f"proof fail: {want}", last  # so the record passed without it
    if name == "extrakey":
        assert any("ключі = M0 ні: +note" in x for x in out), out
    if name == "dropkey":
        assert any("ключі = M0 ні: -modules" in x for x in out), out


ONLY = {"codesha", "foreign", "paths", "dafter", "skipmodel", "skipnorow", "input", "title", "gammakeys", "missing", "extrakey",
        "dropkey"}  # fmt: skip


def test_proof_fails_on_arguments_other_than_the_registered(world, tmp_path, capsys):
    # item 490, R5: a --ref other than origin/main, or a fetch before 15:00 UTC of the release day, is `args` (the same
    # record passes with the registered arguments, test above)
    ref = _case(world, "args", world.raw)
    capsys.readouterr()
    assert f.proof(R, repo=world.repo, ref=ref, db=world.db, root=tmp_path, tmp=tmp_path) is False
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last == f"proof fail: args (ref {ref} is not origin/main)", last
    fetched(world.repo, "2026-12-10T14:59:59+00:00")
    try:
        assert prove(world, ref, db=world.db, root=tmp_path, tmp=tmp_path) is False
    finally:
        fetched(world.repo, "2026-12-10T15:30:00+00:00")
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last == ("proof fail: args (last fetch 2026-12-10T14:59:59+00:00 is before 2026-12-10T15:00:00+00:00; "
                    "run `git fetch origin main` first)"), last  # fmt: skip


@pytest.mark.parametrize(
    ("name", "keys", "want", "line"),
    [("oldm0", "", "proof pass", "ключі = M0 не застосовно, M0 до пункту 490"),
     ("badkeys", ', "keys": None', "proof fail: mismatch (keys)", "ключі = M0 ні: M0 gives no key list")],
)  # fmt: skip
def test_key_check_does_not_apply_to_an_m0_before_item_490(world, tmp_path, capsys, name, keys, want, line):
    # review C of item 490, blocker 2: a proof merged after 07.12 21:00 must not fail a lawful record of an M0 whose
    # scanner gives no key set (registration п. 9); a key field that is not a list still fails
    g, scanner = world.g, world.repo / "scripts/pm_cpi_forward.py"
    g("checkout", "-q", "-B", f"m0-{name}", world.m0)
    text = scanner.read_text()
    new = text.replace(', "keys": list(T3_KEYS)}', keys + "}")
    assert new != text, "the rebuild answer of the scanner changed; update this test"
    scanner.write_text(new)
    g("commit", "-q", "-am", "M0 before item 490", when="2026-12-07T20:30:00Z")
    old = g("rev-parse", "HEAD")
    g("checkout", "-q", "--detach", world.after)
    ref = _case(world, name, _edit(world.raw, code_sha=old, M0=old), base=old)
    capsys.readouterr()
    assert prove(world, ref, db=world.db, root=tmp_path, tmp=tmp_path) is (want == "proof pass")
    out = capsys.readouterr().out.rstrip().splitlines()
    assert out[-1] == want, out
    assert any(line in x for x in out), out
    # review C of item 491, R6: an M0 before item 490 does not know «rules» (п. 9); the line says so, the verdict stays
    sb = "  set B: not applicable, M0 before item 490 (descriptive)" if name == "oldm0" else "  set B: not rules (descriptive)"
    assert sb in out, out


def test_proof_names_a_rules_release_of_set_b(world, tmp_path, capsys):
    # review C of item 490, finding 4: the descriptive line `set B: rules`; this record was edited (the rebuild from
    # the row gives the event back), so the verdict is the rebuild's, not the line's
    ev = lambda r: [{**e, "taken": False, "why": "rules: no 'one decimal'"} if e["event_id"] == "e1" else e for e in r["events"]]  # noqa: E731
    ref = _case(world, "setb", _edit(world.raw, events=ev, skip="no YoY event"))
    capsys.readouterr()
    assert prove(world, ref, db=world.db, root=tmp_path, tmp=tmp_path) is False
    out = capsys.readouterr().out.rstrip().splitlines()
    assert "  set B: rules (rebuilt by п. 8 (в) for set B; descriptive)" in out
    assert out[-1] == "proof fail: mismatch (rebuild: events, skip)", out[-1]


def test_fallback_of_set_b_runs_in_the_tree_of_m0(world, tmp_path):
    # item 490, R1: п. 8 (в) by the code of M0 (`--fallback` of its tree), no Gamma: events by slug, fee by fee_type
    tree = f.extract(world.repo, world.m0, f.CODE_PATHS, tmp_path)
    res = f.run_tree(tree, ["--fallback", "--release", R, "--db", str(world.db), "--repo", str(world.repo), "--m0", world.m0],
                     world.repo, offline_env())  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    got = json.loads(res.stdout)
    assert f.same_record(got, json.loads(json.dumps(f.fallback_record(f.read_row(world.db, "2026-12-07"), R, ""))))
    assert got["skip"] is None and got["row_digest"] == json.loads(world.raw)["row_digest"]
    assert all(e["rules"] == "unchecked" for e in got["events"] if e["taken"]) and got["decision"]["fills"]


def test_launch_refuses_outside_the_window_and_without_m0(world, tmp_path, capsys):
    kw = {"repo": world.repo, "db": world.db, "root": tmp_path / "s", "docs": tmp_path / "d", "tmp": tmp_path}
    assert f.launch("T-3", R, datetime(2026, 12, 9, 12, 1, tzinfo=UTC), ref="origin/main", **kw) == 1
    assert "outside its window (late)" in capsys.readouterr().out
    assert f.launch("T-3", "2026-12-08", datetime(2026, 12, 5, 22, 0, tzinfo=UTC), ref="origin/main", **kw) == 1
    assert "no M0" in capsys.readouterr().out  # the repository has no commit before 05.12 21:00
    assert not (tmp_path / "s").exists()
    # an M0 without the scanner (not merged before 21:00 of T-3): nothing written, no traceback
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q", "-b", "main", home=world.home)
    (bare / "README").write_text("x")
    _git(bare, "add", "-A", home=world.home)
    _git(bare, "commit", "-q", "-m", "M0 without the scanner", home=world.home)
    _git(bare, "update-ref", "refs/remotes/origin/main", "HEAD", home=world.home)
    # finding 5: origin/main is trusted only after a fetch at or after 21:00 UTC of day T-3; T-3 takes origin/main only
    assert f.launch("T-3", R, AT_T3, ref="origin/main", **{**kw, "repo": bare}) == 1
    assert "may be stale (no FETCH_HEAD); run `git fetch origin main` first" in capsys.readouterr().out
    # review C of item 490, finding 3: a failed `git fetch` leaves an empty FETCH_HEAD with a fresh mtime
    fh, ts = bare / ".git" / "FETCH_HEAD", datetime(2026, 12, 7, 21, 30, tzinfo=UTC).timestamp()
    fh.write_text("")
    os.utime(fh, (ts, ts))
    assert f.fetch_head(bare) is None
    assert f.launch("T-3", R, AT_T3, ref="origin/main", **{**kw, "repo": bare}) == 1
    assert "may be stale (no FETCH_HEAD); run `git fetch origin main` first" in capsys.readouterr().out
    fetched(bare, "2026-12-07T20:59:59+00:00")
    assert f.launch("T-3", R, AT_T3, ref="origin/main", **{**kw, "repo": bare}) == 1
    assert "may be stale (last fetch 2026-12-07T20:59:59+00:00 is before 2026-12-07T21:00:00+00:00)" in capsys.readouterr().out
    fetched(bare, "2026-12-07T21:00:00+00:00")
    assert f.launch("T-3", R, AT_T3, ref="main", **{**kw, "repo": bare}) == 1
    assert "T-3 takes M0 from origin/main only" in capsys.readouterr().out
    assert f.launch("T-3", R, AT_T3, ref="origin/main", **{**kw, "repo": bare}) == 1
    assert "cannot be extracted (CalledProcessError" in capsys.readouterr().out and not (tmp_path / "s").exists()


def _main_with(w, name, when, edit):
    """A commit on top of main of the world at ``when`` (``edit(repo)`` changes the files), made origin/main and HEAD."""
    w.g("checkout", "-q", "-B", f"res-{name}", w.after)
    edit(w.repo)
    w.g("commit", "-q", "-am", name, when=when)
    sha = w.g("rev-parse", "HEAD")
    w.g("update-ref", "refs/remotes/origin/main", sha)
    return sha


def _restore(w):
    w.g("checkout", "-q", "--detach", w.after)
    w.g("update-ref", "refs/remotes/origin/main", w.after)


def test_result_runs_the_code_of_r_not_head(world, tmp_path, capsys, monkeypatch):
    # review C of item 491, blocker 1: R = main before the publication (13:30 UTC on 10.12); a merge after it, here HEAD
    # and origin/main, changes the Brier code and is not run
    assert f.publication_utc(R) == datetime(2026, 12, 10, 13, 30, tzinfo=UTC)
    assert f.publication_utc("2027-07-15") == datetime(2027, 7, 15, 12, 30, tzinfo=UTC)  # daylight saving: 08:30 EDT
    kw = {"repo": world.repo, "ref": "origin/main", "db": world.db, "root": tmp_path / "s", "docs": tmp_path / "d", "tmp": tmp_path,
          "default_db": world.db}  # fmt: skip

    def later(repo):
        p = repo / "src/tradedesk/analytics/pm_cpi.py"
        p.write_text(p.read_text() + "\n# Brier changed after the outcome\n")

    try:
        head = _main_with(world, "later", "2026-12-10T14:00:00Z", later)
        assert f.r_of(world.repo, "origin/main", R) == world.after != head == f.git(world.repo, "rev-parse", "HEAD").stdout.strip()
        # the real subprocess of the tree of R (offline: FRED and Gamma fail, nothing is written)
        capsys.readouterr()
        assert f.launch("result", R, AT_RESULT, http_retries=0, env=offline_env(), **kw) == 1
        out = capsys.readouterr().out
        assert f"code of {world.after} (R) extracted" in out and "not final: cpi: request failed" in out, out
        tree = tmp_path / f"pm_cpi_code_{world.after[:12]}"
        assert "after the outcome" not in (tree / "src/tradedesk/analytics/pm_cpi.py").read_text()
        # the same run with the answers in this process: the file names R as its code
        seen: list = []

        def run_here(tree_, args, repo, env=None):
            a = dict(zip(args[::2], args[1::2], strict=True))
            seen.append(a)
            rc = f.write_stage("result", a["--release"], datetime.fromisoformat(a["--at"]), db=Path(a["--db"]), root=Path(a["--root"]),
                               docs=Path(a["--docs"]), repo=Path(a["--repo"]), code_sha=a["--code-sha"], m0=a["--m0"] or None,
                               tree=Path(a["--tree"]), rt_factory=gamma_rt(resolved()), fred_factory=fred(), r=a["--r"])  # fmt: skip
            return SimpleNamespace(returncode=rc, stdout="", stderr="")

        monkeypatch.setattr(f, "run_tree", run_here)
        assert f.launch("result", R, AT_RESULT, http_retries=0, **kw) == 0
        assert seen[0]["--code-sha"] == seen[0]["--r"] == world.after and Path(seen[0]["--tree"]) == tree
        rec = json.loads((tmp_path / "s" / R / "result.json").read_bytes())
        assert rec["code_sha"] == rec["R"] == world.after != head and rec["CODE_PATHS"] == list(f.code_paths_at(world.repo, world.after))
    finally:
        _restore(world)


def test_result_refuses_a_stale_fetch_another_ref_and_an_r_without_the_stage(world, tmp_path, capsys, monkeypatch):
    # review C of item 491, blocker 1: refused, never run by HEAD instead
    kw = {"repo": world.repo, "db": world.db, "root": tmp_path / "s", "docs": tmp_path / "d", "tmp": tmp_path, "default_db": world.db}
    monkeypatch.setattr(f, "run_tree", lambda *a, **k: pytest.fail("no tree may run"))
    capsys.readouterr()
    assert f.launch("result", R, AT_RESULT, ref="main", **kw) == 1
    assert "the result takes R from origin/main only" in capsys.readouterr().out
    fetched(world.repo, "2026-12-10T13:29:59+00:00")
    try:
        assert f.launch("result", R, AT_RESULT, ref="origin/main", **kw) == 1
    finally:
        fetched(world.repo, "2026-12-10T15:30:00+00:00")
    assert "may be stale (last fetch 2026-12-10T13:29:59+00:00 is before 2026-12-10T13:30:00+00:00)" in capsys.readouterr().out

    # item 491 merged after the publication: the scanner of R has no result stage
    def old(repo):
        p = repo / "scripts/pm_cpi_forward.py"
        text = p.read_text()
        assert 'RESULT = "result"' in text
        p.write_text(text.replace('RESULT = "result"', 'NOT_YET = "result"'))

    try:
        r = _main_with(world, "old", "2026-12-08T10:00:00Z", old)
        assert f.has_result_stage(world.repo, r) is False and f.has_result_stage(world.repo, world.after) is True
        assert f.launch("result", R, AT_RESULT, ref="origin/main", **kw) == 1
        out = capsys.readouterr().out
        assert f"the scanner of R {r[:12]} (main before the publication 2026-12-10T13:30:00+00:00) has no result stage" in out
        assert ") extracted to" not in out and not list(tmp_path.glob("pm_cpi_code_*")) and not (tmp_path / "s").exists()
    finally:
        _restore(world)


def test_result_refuses_another_db_and_a_t3_record_not_of_the_journal(world, tmp_path, capsys, monkeypatch):
    # review C of item 491, N1 (b): a result.json read from another database or another T-3 record would hold its one
    # canonical place for good and not count (registration п. 7, result: only with db = the collector's and
    # t3_file.sha256 = the sha256 on the journal line T−3), so such a run is refused before any tree runs
    root, docs = tmp_path / "s", tmp_path / "d"
    kw = {"repo": world.repo, "ref": "origin/main", "db": world.db, "root": root, "docs": docs, "tmp": tmp_path}
    ran: list = []

    def run(tree, args, repo, env=None):
        ran.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(f, "run_tree", run)
    capsys.readouterr()
    assert f.launch("result", R, AT_RESULT, **kw) == 1  # default_db: the collector's database
    assert f"reads the collector's database {f.DEFAULT_DB} only (registration п. 7, result), not {world.db}" in capsys.readouterr().out
    assert ran == [] and not list(tmp_path.glob("pm_cpi_code_*"))
    kw["default_db"] = world.db
    good = hashlib.sha256(world.raw).hexdigest()
    t3_docs, t3_local = docs / R / "T-3.json", root / R / "T-3.json"
    t3_docs.parent.mkdir(parents=True)
    t3_local.parent.mkdir(parents=True)
    # the journal of main (the world's card) has no T-3 line: a T-3 file does not count
    t3_docs.write_bytes(world.raw)
    assert f.launch("result", R, AT_RESULT, **kw) == 1
    assert f"(docs T-3.json has sha256 {good}, the journal of origin/main has no T-3 line)" in capsys.readouterr().out
    line = f"{R} T−3 · D {AT_T3.isoformat()} · джерело рядок 2026-12-07 · без сигналу · sha256 {good}\n"
    try:
        _main_with(world, "two", "2026-12-08T10:00:00Z", lambda repo: (repo / f.CARD).write_text(CARD_TEXT.format(lines=line * 2)))
        assert f.launch("result", R, AT_RESULT, **kw) == 1
        assert f"(the journal of origin/main: 2 journal lines of {R} T-3)" in capsys.readouterr().out
        r = _main_with(world, "journal", "2026-12-08T10:00:00Z", lambda repo: (repo / f.CARD).write_text(CARD_TEXT.format(lines=line)))
        assert f.r_of(world.repo, "origin/main", R) == r
        # scenario 3: the clone's copy changed (a merge conflict, a hand edit) while the local file is right
        t3_local.write_bytes(world.raw)
        edited = _edit(world.raw, skip="model")
        t3_docs.write_bytes(edited)
        assert f.launch("result", R, AT_RESULT, **kw) == 1
        out = capsys.readouterr().out
        assert f"(docs T-3.json has sha256 {hashlib.sha256(edited).hexdigest()}, the journal of origin/main has {good})" in out
        assert ran == []
        # the clone's copy does not read: the local file is read (load_stage), and it is the journal's
        t3_docs.write_bytes(b"not json")
        assert f.launch("result", R, AT_RESULT, **kw) == 0 and ran[-1][ran[-1].index("--r") + 1] == r
        # the clone's copy is the journal's: a local file that is not read does not count (it is listed under `also`)
        t3_docs.write_bytes(world.raw)
        t3_local.write_bytes(b"x")
        assert f.launch("result", R, AT_RESULT, **kw) == 0 and len(ran) == 2
        # no T-3 file while the journal has the line
        t3_docs.unlink()
        t3_local.unlink()
        assert f.launch("result", R, AT_RESULT, **kw) == 1
        assert f"(no T-3.json in the clone or the state directory, the journal of origin/main has {good})" in capsys.readouterr().out
        assert len(ran) == 2
    finally:
        _restore(world)


def test_code_dirty_lists_changed_missing_and_foreign_files(world, tmp_path):
    tree = f.extract(world.repo, world.m0, f.CODE_PATHS, tmp_path)
    assert f.code_dirty(world.repo, world.m0, tree, f.CODE_PATHS, (list(f.CODE_PATHS), [])) == ""
    (tree / "src/tradedesk/core/http.py").write_text("changed")
    (tree / "src/tradedesk/core/cache.py").unlink()
    out = f.code_dirty(world.repo, world.m0, tree, f.CODE_PATHS, ([*f.CODE_PATHS, "src/tradedesk/extra.py"], ["/elsewhere/x.py"]))
    assert out.splitlines() == ["D src/tradedesk/core/cache.py", "M src/tradedesk/core/http.py", "X src/tradedesk/extra.py",
                                "O /elsewhere/x.py"]  # fmt: skip
    assert f.code_dirty(world.repo, "0" * 40, tree, f.CODE_PATHS, ([], [])).startswith("git ls-tree failed")
    assert f.code_paths_at(world.repo, world.m0) == f.CODE_PATHS
    with pytest.raises(subprocess.CalledProcessError):
        f.code_paths_at(world.repo, "0" * 40)
