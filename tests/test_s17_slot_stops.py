"""Tests of analytics/s17_slot_stops.py (synthetic, analytic expectations)."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics import s17_slot_stops as ss
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.pending_slots import Interval
from tradedesk.analytics.stop_management import Fees

H = 3_600_000
D = 86_400_000
T0 = 20_000 * D  # a UTC midnight
SLOTS = "2 S17 trades at risk today: its 2 slots are taken and it never takes the reserve"
CAP = "15 open positions = max"


def sig(lid: str, ts: int, rr: float, entry: float = 100.0, side: str = "long", rid: int = 0) -> dict:
    s = {"side": side, "entry": entry, "stop": entry - 5 if side == "long" else entry + 5, "indicators": {"rest_until_ms": ts + 90 * D}}
    s["target"] = entry + 10 if side == "long" else entry - 10
    return {"id": rid, "ts": ts, "link_id": lid, "payload": {"signal": s, "proposal": {"rr": rr}, "score": 2}}


def dec(lid: str, ts: int, viol: list[str], score: int = 2, rid: int = 0) -> dict:
    return {"id": rid, "ts": ts, "link_id": lid, "payload": {"violations": viol, "score": score}}


def cand(lid: str, ts: int, viol: tuple[str, ...], score: int = 2, rr: float = 1.9) -> ss.Candidate:
    return ss.Candidate(lid, ts, score, rr, "long", 100.0, 95.0, 110.0, ts + 90 * D, viol)


def test_pairing_in_order_and_first_cycle_only() -> None:
    t = T0 + 120_000
    sigs = [sig("k", t, 1.8, rid=1), sig("k", t + 1000, 1.95, 101.0, rid=2), sig("k", t + 2 * D, 1.7, rid=9)]
    decs = [dec("k", t + 5000, ["stop 12.0% from the entry > 10%"], rid=3), dec("k", t + 6000, [SLOTS], rid=4)]
    got = ss.pair_first_cycle(sigs, decs)
    assert got is not None and len(got) == 2
    assert got[0].violations == ("stop 12.0% from the entry > 10%",) and got[0].rr == 1.8
    assert got[1].entry == 101.0 and got[1].violations == (SLOTS,) and got[1].ts == t + 6000


def test_pairing_count_mismatch_is_unmatched() -> None:
    t = T0
    assert ss.pair_first_cycle([sig("k", t, 1.8), sig("k", t + 1, 1.9)], [dec("k", t + 5, [SLOTS])]) is None
    assert ss.pair_first_cycle([sig("k", t, 1.8)], []) is None


def test_classify_uses_demo_rank_and_structural_filter() -> None:
    t = T0
    wide = cand("k", t, ("stop 11.0% from the entry > 10%",), rr=1.99)
    low = cand("k", t, ("score 1 < 2",), score=1, rr=1.98)
    a = cand("k", t, (SLOTS,), rr=1.7)
    b = cand("k", t, (SLOTS, CAP), rr=1.9)  # the higher R:R sibling decides
    k = ss.classify_key("k", [wide, low, a, b], placed=False)
    assert k.cls == "slots_and_limit" and k.best == b
    assert ss.classify_key("k", [cand("k", t, (SLOTS,))], False).cls == "slots_only"
    assert ss.classify_key("k", [cand("k", t, (CAP,))], False).cls == "limit_only"
    assert ss.classify_key("k", [cand("k", t, (SLOTS, "open risk 2450 + 100 USD > 2500"))], False).cls == "slots_limit_plus_other"
    assert ss.classify_key("k", [cand("k", t, ("already in ETH/USDT:USDT",))], False).cls == "other"
    assert ss.classify_key("k", [cand("k", t, (SLOTS, "this signal was already decided"))], False).cls == "slots_only"
    assert ss.classify_key("k", [wide, low], False).cls == "ineligible"
    assert ss.classify_key("k", None, False).cls == "unmatched"
    assert ss.classify_key("k", [a], True).cls == "placed"


UNREAD = "S17 trades at risk today unknown (book not read)"
AT_LEVEL = "price 99.5 already at the resting level 100: post-only would take"


def test_slot_text_without_n_and_at_level_have_own_classes() -> None:
    t = T0
    k = ss.classify_key("k", [cand("k", t, (UNREAD,))], False)
    assert k.cls == "slots_unread" and "setup_slots" in k.cats  # not counted as stopped by slots
    assert ss.classify_key("k", [cand("k", t, (UNREAD, CAP))], False).cls == "slots_unread"
    assert ss.classify_key("k", [cand("k", t, (AT_LEVEL,))], False).cls == "at_level"
    # at_level with another structural flaw, or with too low a score, stays ineligible
    assert ss.classify_key("k", [cand("k", t, (AT_LEVEL, "stop 12.0% from the entry > 10%"))], False).cls == "ineligible"
    assert ss.classify_key("k", [cand("k", t, (AT_LEVEL,), score=1)], False).cls == "ineligible"
    # an eligible sibling wins over an at_level one
    assert ss.classify_key("k", [cand("k", t, (AT_LEVEL,)), cand("k", t, (SLOTS,), rr=1.5)], False).cls == "slots_only"


def test_candidate_level_is_the_tick_rounded_proposal() -> None:
    t = T0
    s = sig("k", t, 1.8, entry=100.0004)
    s["payload"]["proposal"].update(entry=100.0, stop=95.0, target=110.0)
    got = ss.pair_first_cycle([s], [dec("k", t + 5, [SLOTS])])
    assert got is not None and got[0].entry == 100.0 and got[0].stop == 95.0
    got2 = ss.pair_first_cycle([sig("k", t, 1.8, entry=100.0004)], [dec("k", t + 5, [SLOTS])])  # no proposal levels
    assert got2 is not None and got2[0].entry == 100.0004


def test_share_vectors_use_every_eligible_key() -> None:
    day = 7
    classes = [
        "placed",
        "placed",
        "slots_only",
        "limit_only",
        "slots_and_limit",
        "slots_only",
        "slots_limit_plus_other",
        "slots_unread",
        "other",
        "ineligible",
        "at_level",
        "unmatched",
    ]
    v = ss.share_vectors([(c, day + i % 2) for i, c in enumerate(classes)])
    n = len(v["days"])
    assert n == 9  # ineligible, at_level, unmatched are out of the denominator
    assert sum(v["s"]) / n == pytest.approx(4 / 9)  # (a) over all eligible, not over placed + (a) (= 4/6)
    assert sum(v["s_prime"]) / n == pytest.approx(5 / 9)
    assert sum(v["placed"]) / n == pytest.approx(2 / 9)
    assert len(v["s"]) == len(v["s_prime"]) == len(v["placed"]) == n


def test_slots_used_parses_guard_text() -> None:
    assert ss.slots_used(["x", SLOTS]) == 2
    assert ss.slots_used(["3 S17 trades at risk today: its 2 slots and the reserve of 3 are taken"]) == 3
    assert ss.slots_used([CAP]) is None


def test_prev_day_pending_and_release() -> None:
    t = T0 + 5 * D + 120_000
    ivs = [
        Interval(T0 + 2 * D, T0 + 30 * D, False),  # resting since an earlier day: counts
        Interval(T0 + 5 * D + 60_000, T0 + 30 * D, False),  # placed today: not an earlier-day limit
        Interval(T0 + 1 * D, T0 + 4 * D, True),  # filled before t: no longer pending
    ]
    assert ss.prev_day_pending(ivs, t) == 1
    k1 = ss.classify_key("a", [cand("a", t, (SLOTS,), rr=1.7)], False)
    k2 = ss.classify_key("b", [cand("b", t, (SLOTS,), rr=1.9)], False)
    k3 = ss.classify_key("c", [cand("c", t, (SLOTS, CAP), rr=1.95)], False)  # also at the cap: never admitted
    assert ss.released_keys([k1, k2, k3], ivs) == {"b"}  # N 2 - P 1 = 1 used -> 1 free slot, best R:R first
    assert ss.released_keys([k1, k2, k3], ivs[1:]) == set()  # no earlier-day pending: nothing freed


def bars(rows: list[tuple[float, float, float, float]], t0: int = T0) -> Bars:
    return Bars.from_rows([[t0 + i * H, *r] for i, r in enumerate(rows)], H)


def test_first_touch_and_status_with_censoring() -> None:
    b = bars([(105, 106, 104, 105), (105, 105, 100, 101), (101, 102, 99, 100)])
    assert ss.first_touch(b, "long", 100.0, T0, T0 + 3 * H) == T0 + H
    assert ss.first_touch(b, "long", 100.0, T0, T0 + 3 * H, through=True) == T0 + 2 * H
    assert ss.first_touch(b, "short", 106.0, T0, T0 + 3 * H) == T0
    assert ss.first_touch(b, "long", 100.0, T0, T0 + H) is None  # bars from t1 on are not read
    assert ss.touch_status(T0 + H, T0, 3 * D, T0 + 3 * H) == "early_touch"  # horizon not over: never in a share
    assert ss.touch_status(T0 + H, T0, 3 * D, T0 + 3 * D) == "touched"
    assert ss.touch_status(None, T0, 3 * D, T0 + 3 * H) == "censored"
    assert ss.touch_status(None, T0, 3 * D, T0 + 3 * D) == "not"
    assert ss.touch_status(T0 + 4 * D, T0, 3 * D, T0 + 5 * D) == "not"  # a touch after the horizon does not count


def test_touch_share_counts_only_finished_horizons() -> None:
    cut = T0 + 10 * D
    # key 1: decided at T0, touched on day 1 (horizon 3 d over); key 2: decided at T0, never touched (over);
    # key 3: decided 1 d before the cut-off, already touched (horizon open); key 4: decided 1 d before, no touch yet
    sts = [
        ss.touch_status(T0 + D, T0, 3 * D, cut),
        ss.touch_status(None, T0, 3 * D, cut),
        ss.touch_status(cut - D + H, cut - D, 3 * D, cut),
        ss.touch_status(None, cut - D, 3 * D, cut),
    ]
    assert sts == ["touched", "not", "early_touch", "censored"]
    assert ss.touch_share(sts) == (1, 2)  # 1/2, not the biased 2/3 that counts the early touch
    assert ss.touch_share(["early_touch", "early_touch"]) == (0, 0)  # an all-unfinished horizon has no share (90 d)


def test_first_touch_glued_reads_the_first_hour_on_1m_bars() -> None:
    t_dec = T0 + 120_000  # 00:02, as the demo places
    t_min, t_hour = T0 + 120_000, T0 + H
    fine = Bars.from_rows([[t_min + i * 60_000, 101.0, 101.5, 99.5 if i == 6 else 100.5, 101.0] for i in range(58)], 60_000)
    coarse = bars([(101, 102, 100.5, 101)] * 5, T0 + H)
    # the 00:08 dip is found on the 1m bars; with the first hour skipped it would be lost
    assert ss.first_touch_glued(fine, coarse, "long", 100.0, t_min, t_hour, T0 + 6 * H) == t_min + 6 * 60_000
    assert ss.first_touch(coarse, "long", 100.0, t_hour, T0 + 6 * H) is None
    # no 1m touch: falls through to the 1h bars from the first full hour
    fine2 = Bars.from_rows([[t_min + i * 60_000, 101.0, 101.5, 100.5, 101.0] for i in range(58)], 60_000)
    coarse2 = bars([(101, 102, 100.5, 101), (101, 101, 99.0, 100)], T0 + H)
    assert ss.first_touch_glued(fine2, coarse2, "long", 100.0, t_min, t_hour, T0 + 6 * H) == T0 + 2 * H
    # a 1h bar opening before the switch (it overlaps the decision) is never read
    coarse3 = bars([(101, 101, 90.0, 95), (101, 102, 100.5, 101)], T0)
    assert ss.first_touch_glued(fine2, coarse3, "long", 100.0, t_dec, t_hour, T0 + 2 * H) is None
    # a 1m touch after t1 is not read either
    assert ss.first_touch_glued(fine, coarse, "long", 100.0, t_min, t_hour, t_min + 6 * 60_000) is None


def test_touch_before_t_does_not_depend_on_later_bars() -> None:
    rows = [(105.0, 106.0, 104.0, 105.0)] * 48
    b1 = bars(rows)
    later = rows[:24] + [(105.0, 105.0, 90.0, 95.0)] * 24  # change only the bars from T0 + 24h on
    b2 = bars(later)
    for b in (b1, b2):
        assert ss.first_touch(b, "long", 100.0, T0, T0 + 24 * H) is None
    assert ss.first_touch(b2, "long", 100.0, T0, T0 + 48 * H) == T0 + 24 * H


def test_km_touch_analytic() -> None:
    # 4 keys: touch at 1 d, censored at 2 d, touch at 3 d, censored at 5 d
    km = ss.km_touch([1.0, None, 3.0, None], [10.0, 2.0, 10.0, 5.0], [0.5, 1, 2.5, 3, 6])
    assert km[0] == 0.0
    assert km[1] == pytest.approx(0.25)
    assert km[2] == pytest.approx(0.25)
    assert km[3] == pytest.approx(1 - 0.75 * 0.5)  # risk set at 3 d: 2
    assert km[4] == pytest.approx(km[3])


def test_shadow_trade_target_stop_and_censoring() -> None:
    f0 = Fees(maker=0.0, taker_exit=0.0, risk_taker=0.0)
    # long limit 100, stop 95, target 110: through-fill in bar 1, target in bar 3
    b = bars([(103, 104, 102, 103), (101, 102, 99.5, 101), (101, 105, 100.5, 104), (106, 111, 105, 110)])
    r = ss.shadow_trade(b, "long", 100.0, 95.0, 110.0, T0, T0 + 90 * D, T0 + 4 * H, 20 * D, ss.RULE_A, f0)
    assert r["status"] == "resolved" and r["r"] == pytest.approx(2.0) and r["fill_ms"] == T0 + H
    # the fill bar also reaches the stop: a loss at the stop (pessimistic)
    b2 = bars([(103, 104, 102, 103), (101, 102, 94, 96)])
    r2 = ss.shadow_trade(b2, "long", 100.0, 95.0, 110.0, T0, T0 + 90 * D, T0 + 2 * H, 20 * D, ss.RULE_A, f0)
    assert r2["status"] == "resolved" and r2["r"] == pytest.approx(-1.0)
    # short mirror: limit 100, stop 105, target 90
    b3 = bars([(97, 98, 96, 97), (99, 100.5, 98, 99), (95, 96, 89, 90)])
    r3 = ss.shadow_trade(b3, "short", 100.0, 105.0, 90.0, T0, T0 + 90 * D, T0 + 3 * H, 20 * D, ss.RULE_A, f0)
    assert r3["status"] == "resolved" and r3["r"] == pytest.approx(2.0)
    # no fill, deadline in the future: pending; deadline passed: not_filled
    b4 = bars([(103, 104, 102, 103)] * 3)
    assert ss.shadow_trade(b4, "long", 100.0, 95.0, 110.0, T0, T0 + 90 * D, T0 + 3 * H, 20 * D)["status"] == "pending"
    assert ss.shadow_trade(b4, "long", 100.0, 95.0, 110.0, T0, T0 + 2 * H, T0 + 3 * H, 20 * D)["status"] == "not_filled"
    # filled, neither stop nor target by the cut-off: open, marked to the last close
    b5 = bars([(101, 102, 99.5, 101), (101, 103, 100.5, 102), (102, 103, 101, 102)])
    r5 = ss.shadow_trade(b5, "long", 100.0, 95.0, 110.0, T0, T0 + 90 * D, T0 + 3 * H, 20 * D, ss.RULE_A, f0)
    assert r5["status"] == "open" and r5["r"] is None and r5["r_mtm"] == pytest.approx(0.4)
    assert not math.isnan(r5["r_mtm"])
    assert np.isfinite(r["r"])


# ------------------------------------------------------------------- the runner on a synthetic DB (no network, no real data)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_s17_slot_stops as vs  # noqa: E402


def _synthetic_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE exec_audit (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, link_id TEXT, symbol TEXT, setup TEXT, payload TEXT)"
    )
    con.execute(
        "CREATE TABLE demo_trade (link_id TEXT, symbol TEXT, side TEXT, timeframe TEXT, status TEXT, signal_ts INTEGER, entry REAL,"
        " stop REAL, target REAL, opened_ts INTEGER, closed_ts INTEGER, rest_until INTEGER, setup TEXT)"
    )

    def aud(ts: int, kind: str, lid: str, sym: str, payload: dict | None = None) -> None:
        con.execute(
            "INSERT INTO exec_audit (ts, kind, link_id, symbol, setup, payload) VALUES (?,?,?,?,?,?)",
            (ts, kind, lid, sym, "S17", json.dumps(payload or {})),
        )

    def sp(score: int = 2, rr: float = 1.8) -> dict:
        return {
            "signal": {"side": "long", "entry": 100.0, "stop": 95.0, "target": 110.0, "indicators": {"rest_until_ms": 0}},
            "proposal": {"rr": rr},
            "score": score,
        }

    day1, day2 = vs.ms("2026-11-05T00:02"), vs.ms("2026-11-15T00:02")
    k1, k2 = vs.ms("2026-11-04T00:00") // 1000, vs.ms("2026-11-14T00:00") // 1000
    lid = lambda s, k: f"S17-long-{s}-1d-{k}"  # noqa: E731
    for coin, t, k, status, fill in (("ZEC", day1, k1, "pending", None), ("RENDER", day2, k2, "open", day2 + 60_000 + 6 * 60_000)):
        x = lid(coin, k)
        aud(t, "signal", x, f"{coin}/USDT:USDT", sp())
        aud(t + 60_000, "sent", x, f"{coin}/USDT:USDT")
        if fill:
            aud(fill, "filled", x, f"{coin}/USDT:USDT")
        con.execute(
            "INSERT INTO demo_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (x, f"{coin}/USDT:USDT", "long", "1d", status, k * 1000, 100.0, 95.0, 110.0, t + 60_000, None, t + 90 * D, "S17"),
        )
    keys = (  # (coin, day, k, decision kind, violations, score): 4 in (a), one with another cap, unread, other, at_level, ineligible
        ("SOL", day1, k1, "skipped", [SLOTS], 2),
        ("WLD", day1, k1, "refused", [CAP], 2),
        ("ATOM", day2, k2, "skipped", [SLOTS], 2),
        ("ETHFI", day2, k2, "skipped", [SLOTS, CAP], 2),
        ("ARB", day2, k2, "skipped", [SLOTS, "open risk 2450 + 100 USD > 2500"], 2),
        ("OP", day2, k2, "skipped", [UNREAD], 2),
        ("TAO", day1, k1, "skipped", ["already in TAO/USDT:USDT"], 2),
        ("SUI", day1, k1, "refused", [AT_LEVEL], 2),
        ("GALA", day1, k1, "skipped", ["score 1 < 2"], 1),
    )
    for i, (coin, t, k, kind, viol, score) in enumerate(keys):
        x = lid(coin, k)
        aud(t + 10 + i, "signal", x, f"{coin}/USDT:USDT", sp(score))
        aud(t + 70_000 + i, kind, x, f"{coin}/USDT:USDT", {"violations": viol, "score": score})
    con.commit()
    con.close()


def test_run_prints_registered_shares_and_integrity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db.sqlite"
    _synthetic_db(db)

    async def fake_bars(rt, b, cache, symbol, since, until, tf="1h"):  # 1h never reaches 100; the 1m dip at minute 3 does
        bar = vs.TF[tf]
        n = max(0, (until - since) // bar)
        low = [99.0 if tf == "1m" and i == 3 else 100.5 for i in range(n)]
        return Bars.from_rows([[since + i * bar, 101.0, 102.0, low[i], 101.0] for i in range(n)], bar)

    class RT:
        def __init__(self, *_a, **_k) -> None: ...

        async def aclose(self) -> None: ...

    monkeypatch.setattr(vs, "fetch_bars", fake_bars)
    monkeypatch.setattr(vs, "Runtime", RT)
    monkeypatch.setattr(vs, "LINES", [])
    a = argparse.Namespace(db=str(db), cache_dir=tmp_path, out=None, now_ms=vs.DATA_CUTOFF, stage="run")
    asyncio.run(vs.run(a, vs.DATA_CUTOFF))
    out = "\n".join(vs.LINES)
    # blocker 1: the denominator is every eligible key (2 placed + 4 (a) + ARB + OP + TAO = 9), and s′ is printed
    assert "придатних ключів n 9 " in out
    assert "s = (а) / придатні (головна): 0.444" in out
    assert "s′ = ((а) + slots_limit_plus_other) / придатні (чутливість): 0.556" in out
    assert "частка розміщених = placed / придатні (описово): 0.222" in out
    assert "◯-умови (пріоритет над ✓ / ≈ / ✗): придатних < 10: так" in out
    # finding 5: the slot text without N is its own class and the 18.11 sent check is printed
    assert "| slots_unread | 1 |" in out and "| at_level | 1 |" in out
    assert "«слоти зайняті» без числа N у тексті (клас slots_unread, у (а) не входить): 1" in out
    assert "збіг: НІ" in out  # only ZEC and RENDER are in the synthetic DB
    # blocker 3: the first-hour dip is found on 1m bars (RENDER filled at 00:09 in the demo); the 1h-only sensitivity misses it
    assert "±1 год від `filled` демо: 1 з 1" in out
    assert "| stopped | 3 д | 4 | 0 |" in out and "| stopped | 3 д, чутливість: лише 1h від першої повної години | 0 | 4 |" in out
    # blocker 2: no share for 90 d
    assert "| stopped | 90 д | 0 | 0 | ◯ (частку не оцінюю" in out
    assert "| stopped | 90 д | 0 | 0 | ◯ (частку не оцінюю: горизонт не скінчився в жодного ключа) | 4 | 0 |" in out
    assert "на розміщення" not in out  # finding 11: the duplicate row is gone


def test_fetch_bars_does_not_cache_a_partial_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bars").mkdir()
    calls = []

    def fake_venue_fetch(rt, b, venue):
        async def fetch(symbol, tf, since, until):
            calls.append(since)
            if len(calls) == 1:
                b.warn("bybit X 1h: a later page failed; the replay may say no_data")
            return [[since + i * H, 1.0, 2.0, 0.5, 1.5] for i in range(3)]

        return fetch

    monkeypatch.setattr(vs, "venue_fetch", fake_venue_fetch)
    b = vs.Builder()
    got = asyncio.run(vs.fetch_bars(None, b, tmp_path, "X/USDT:USDT", T0, T0 + 3 * H))  # type: ignore[arg-type]
    assert len(got) == 3 and not list((tmp_path / "bars").iterdir())  # partial: served but not cached
    asyncio.run(vs.fetch_bars(None, b, tmp_path, "X/USDT:USDT", T0, T0 + 3 * H))  # type: ignore[arg-type]
    assert len(calls) == 2 and len(list((tmp_path / "bars").iterdir())) == 1  # fetched again, complete -> cached
