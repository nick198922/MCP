"""Review 2026-11-07 (ccxt funding dropped rows), point 7: a dropped funding row of a non-held symbol must not end up
inside the stored coverage. The incremental fetch in ``collector/carry_paper_feed.LiveFeed._history`` used to start at
``last_ts + 1``: once a later row was stored, the window with the dropped row was skipped and the next complete read
recorded coverage over it. It now starts at the end of the coverage, so the hole is re-read (and flagged) on the next
cycles; as engine R2 does, after GAP_CONFIRM_CYCLES funding steps the hole is confirmed once and coverage resumes."""

from __future__ import annotations

import pytest

from test_carry_paper_live import CYCLE, H8, LiveEx, _rt
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS
from tradedesk.collector.carry_paper_engine import GAP_CONFIRM_CYCLES, PARAMS_A, PARAMS_B, run_cycle
from tradedesk.collector.carry_paper_feed import LiveFeed, Pacer
from tradedesk.core.envelope import Builder

BTC = "BTC/USDT:USDT"


@pytest.mark.parametrize(("params", "venue"), [(PARAMS_A, "binanceusdm"), (PARAMS_B, "bybit")])
async def test_dropped_row_of_a_non_held_symbol_is_not_papered_over_and_is_confirmed_once(tmp_path, params, venue):
    hole = CYCLE + H8 + 3  # BTC is never opened (its rate is too low); this payment arrives without a timestamp

    class Ex(LiveEx):
        history_calls: list = []

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            if symbol == BTC and self.id == venue:
                rows = [dict(r, timestamp=None) if r["timestamp"] == hole else r for r in rows]
            return rows

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2, pacer=Pacer(10**6))
        assert (await run_cycle(rt.store, feed, params, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        flagged = 0
        for n in range(1, GAP_CONFIRM_CYCLES + 3):
            cyc_ts = CYCLE + n * H8
            Ex.cycle = cyc_ts
            Ex.history_calls.clear()
            assert (await run_cycle(rt.store, feed, params, cyc_ts, clock=lambda c=cyc_ts: c + DECISION_DELAY_MS))["status"] == "done"
            assert all(p["state"]["symbol"] != BTC for p in await rt.store.cp_positions(params.strategy, "open"))
            st = await rt.store.cp_funding_state(venue, BTC)
            assert st is not None and st["last_ts"] == (CYCLE + 3 if n == 1 else cyc_ts + 3)  # payments after the hole stored
            btc = [s for v, sym, s in Ex.history_calls if v == venue and sym == BTC]
            flags = (await rt.store.cp_cycle_get(params.strategy, cyc_ts))["payload"]["flags"]
            mine = [(f["symbol"], f["venue"], f["issue"], f.get("dropped_rows")) for f in flags if f.get("symbol") == BTC]
            flagged += bool(mine)
            if n <= GAP_CONFIRM_CYCLES:
                assert st["covered_to"] < hole, (n, st)  # the window with the dropped row is not covered yet
                assert btc and min(btc) <= hole, (n, btc)  # the hole is re-read, not skipped
                assert mine == [(BTC, venue, "funding_rows_dropped", 1)], (n, flags)  # named symbol + venue
            elif n == GAP_CONFIRM_CYCLES + 1:
                assert btc and min(btc) <= hole, (n, btc)  # the confirming read still starts at the coverage end
                assert mine == [(BTC, venue, "funding_rows_gap_confirmed", 1)], (n, flags)
                assert st["covered_to"] == cyc_ts + DECISION_DELAY_MS  # confirmed unfillable: coverage resumes
            else:
                assert mine == [], (n, flags)  # confirmed once, then no more flags
                assert btc and min(btc) > hole and st["covered_to"] == cyc_ts + DECISION_DELAY_MS
        assert flagged == GAP_CONFIRM_CYCLES + 1  # bounded, not every cycle while the hole stays in the 8-day window
    finally:
        await rt.aclose()


async def test_truncated_read_behind_an_old_hole_is_not_confirmed(tmp_path):
    rt = _rt(LiveEx, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        store, feed, until = rt.store, LiveFeed(rt, workers=1, pacer=Pacer(10**6)), CYCLE + 5 * H8 + DECISION_DELAY_MS
        await store.cp_funding_put("bybit", BTC, [(CYCLE + 3, 1e-4)], covered_from=CYCLE - 9 * H8, covered_to=CYCLE + DECISION_DELAY_MS)
        await store.cp_funding_put("bybit", BTC, [(CYCLE + 4 * H8 + 3, 1e-4)], covered_from=None, covered_to=None)
        calls: list = []

        async def fetch(b, venue, sym, since, until):  # our walk cut short (Bybit keeps the newest rows), plus a dropped row
            calls.append(since)
            return {
                "rows": [{"timestamp": CYCLE + 5 * H8 + 3, "fundingRate": 1e-4}],
                "complete": False,
                "truncated": True,
                "dropped_rows": 1,
            }

        feed._fetch_range = fetch  # type: ignore[method-assign]
        flags: list = []
        assert await feed._history(Builder(), "bybit", BTC, CYCLE - 6 * H8, until, flags)
        assert calls == [CYCLE + DECISION_DELAY_MS + 1]
        assert [f["issue"] for f in flags] == ["funding_history_incomplete"]  # our own failure is never confirmed
        assert (await store.cp_funding_state("bybit", BTC))["covered_to"] == CYCLE + DECISION_DELAY_MS
    finally:
        await rt.aclose()


async def test_hole_older_than_the_window_is_not_reread_and_coverage_advances(tmp_path):
    hole = CYCLE + H8 + 3

    class Ex(LiveEx):
        history_calls: list = []
        cycle = CYCLE + 3 * H8

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            return [dict(r, timestamp=None) if r["timestamp"] == hole else r for r in rows]

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        store, feed, until = rt.store, LiveFeed(rt, workers=1, pacer=Pacer(10**6)), CYCLE + 3 * H8 + DECISION_DELAY_MS
        # covered up to the cycle before the hole; a later incomplete read stored the payment after it
        await store.cp_funding_put(
            "binanceusdm", BTC, [(CYCLE + 3, 1e-4)], covered_from=CYCLE - 9 * H8, covered_to=CYCLE + DECISION_DELAY_MS
        )
        await store.cp_funding_put("binanceusdm", BTC, [(CYCLE + 2 * H8 + 3, 1e-4)], covered_from=None, covered_to=None)

        flags: list = []
        assert await feed._history(Builder(), "binanceusdm", BTC, CYCLE - 6 * H8, until, flags)
        assert Ex.history_calls[-1][2] == CYCLE + DECISION_DELAY_MS + 1  # the window holds the hole: read from the coverage end
        assert [(f["symbol"], f["venue"], f["issue"]) for f in flags] == [(BTC, "binanceusdm", "funding_rows_dropped")]
        assert (await store.cp_funding_state("binanceusdm", BTC))["covered_to"] < hole

        flags = []
        assert await feed._history(Builder(), "binanceusdm", BTC, hole + 1, until, flags)
        assert Ex.history_calls[-1][2] == hole + 1  # the hole is older than the window: clamped to `since`, not re-read
        assert flags == []
        assert (await store.cp_funding_state("binanceusdm", BTC))["covered_to"] == until
    finally:
        await rt.aclose()
