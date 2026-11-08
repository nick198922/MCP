"""Review 2026-11-07 (ccxt funding dropped rows), point 7: a dropped funding row of a non-held symbol must not end up
inside the stored coverage. Reproduced 08.11; the fix belongs in ``collector/carry_paper_feed.LiveFeed._history`` (the
incremental fetch starts at ``last_ts + 1``, past a row dropped on an earlier cycle whose window got no coverage), so the
test is a strict xfail until that change lands: it starts passing, and must lose the marker, once the gap is fixed."""

from __future__ import annotations

import pytest

from test_carry_paper_live import CYCLE, H8, LiveEx, _rt
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS
from tradedesk.collector.carry_paper_engine import PARAMS_A, run_cycle
from tradedesk.collector.carry_paper_feed import LiveFeed, Pacer


@pytest.mark.xfail(strict=True, reason="known gap (review 2026-11-07 p.7): fix is in collector/carry_paper_feed._history")
async def test_dropped_row_of_a_non_held_symbol_is_not_papered_over_by_coverage(tmp_path):
    hole = CYCLE + H8 + 3  # BTC is never opened (its rate is too low); this payment arrives without a timestamp

    class Ex(LiveEx):
        history_calls: list = []

        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            if symbol.startswith("BTC"):
                rows = [dict(r, timestamp=None) if r["timestamp"] == hole else r for r in rows]
            return rows

    rt = _rt(Ex, tmp_path)
    assert rt.store is not None
    await rt.store.init()
    try:
        feed = LiveFeed(rt, workers=2, pacer=Pacer(10**6))
        assert (await run_cycle(rt.store, feed, PARAMS_A, CYCLE, clock=lambda: CYCLE + DECISION_DELAY_MS))["status"] == "done"
        for n in (1, 2, 3):
            cyc_ts = CYCLE + n * H8
            Ex.cycle = cyc_ts
            assert (await run_cycle(rt.store, feed, PARAMS_A, cyc_ts, clock=lambda c=cyc_ts: c + DECISION_DELAY_MS))["status"] == "done"
            assert all(p["state"]["symbol"] != "BTC/USDT:USDT" for p in await rt.store.cp_positions("A", "open"))
            st = await rt.store.cp_funding_state("binanceusdm", "BTC/USDT:USDT")
            assert st is not None and st["covered_to"] < hole, (n, st)  # cycle 3 today: covered_to jumps past the hole
    finally:
        await rt.aclose()
