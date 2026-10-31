"""A v2 (analytics/carry_v2.py): the leg-vanish exit next to the frozen A book, and its line in the weekly report."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from carry_synth import H8, markets_a
from test_carry_paper_engine import SLOTS, FixtureFeed, _drive, _grid
from tradedesk.analytics.carry_rules import DECISION_DELAY_MS, carry_side_cost
from tradedesk.analytics.carry_v2 import A_V2_DESCRIPTION, A_V2_N_CYCLES, A_V2_TAG, a_v2_shadow, leg_vanished, vanish_rule_ts
from tradedesk.collector.carry_paper_engine import PARAMS_A, CarryParams, FeedData
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import paper_carry_report as rep  # noqa: E402

C0 = 1_700_000_000_000 // H8 * H8
OPEN = C0 + DECISION_DELAY_MS  # decision time of the entry cycle


def _pay(cycles: list[int]) -> list[int]:
    """Payment stamped a few hundred ms after the funding time that cycle ``i`` settles (C0 + i * 8h)."""
    return [C0 + i * H8 + 400 for i in cycles]


@pytest.mark.parametrize("n", [1, 2, 3])
@pytest.mark.parametrize("k", [1, 2, 5])
def test_leg_gone_from_cycle_k_fires_at_cycle_k_plus_n_minus_1(k, n):
    f = _pay(list(range(1, k)))  # cycles 1 .. k-1 paid, nothing from cycle k on
    assert vanish_rule_ts(f, OPEN, OPEN + 40 * H8, n_cycles=n) == OPEN + (k + n - 1) * H8


def test_registered_n_is_2():
    assert A_V2_N_CYCLES == 2
    assert vanish_rule_ts(_pay([1, 2, 3]), OPEN, OPEN + 40 * H8) == OPEN + 5 * H8  # gone at cycle 4 -> rule at cycle 5


def test_gap_of_one_cycle_does_not_fire():
    f = _pay([c for c in range(1, 30) if c != 7])  # cycle 7 has no payment, the leg comes back at cycle 8
    assert vanish_rule_ts(f, OPEN, OPEN + 29 * H8) is None
    assert vanish_rule_ts(f, OPEN, OPEN + 29 * H8, n_cycles=1) == OPEN + 7 * H8  # the same gap does fire with N = 1


def test_4h_funding_interval_counts_as_present():
    f = [C0 + i * H8 // 2 + 400 for i in range(1, 40)]  # a payment every 4 h
    assert vanish_rule_ts(f, OPEN, OPEN + 19 * H8) is None


def test_traded_perp_bars_block_the_rule_and_spot_is_required():
    f = _pay([1, 2, 3])
    bars = [C0 + i * H8 // 2 for i in range(1, 30)]  # 4h perp bars keep trading after funding stops
    assert vanish_rule_ts(f, OPEN, OPEN + 14 * H8, perp_close_ts=bars) is None
    stop_bars = [b for b in bars if b <= C0 + 6 * H8]  # bars stop at cycle 6: last window with a bar is cycle 6
    assert vanish_rule_ts(f, OPEN, OPEN + 14 * H8, perp_close_ts=stop_bars) == OPEN + 8 * H8
    t = OPEN + 5 * H8
    assert leg_vanished(f, t, 1.0) and not leg_vanished(f, t, None) and not leg_vanished(f, t, 0.0)
    assert not leg_vanished(f, t, 1.0, perp_close_ts=bars)


def test_rule_uses_only_data_up_to_its_cycle():
    f = _pay([1, 2, 3])
    fired = vanish_rule_ts(f, OPEN, OPEN + 40 * H8)
    back = f + _pay(list(range(6, 40)))  # the leg "returns" after the rule fired: the decision cannot change
    assert vanish_rule_ts(back, OPEN, OPEN + 40 * H8) == fired == OPEN + 5 * H8
    assert vanish_rule_ts(f, OPEN, fired - 1) is None  # end before the rule cycle: not fired yet


def _pos(tid: str, status: str, *, closed_k: int | None = None, pnl: float = 0.0) -> dict[str, Any]:
    return {
        "trade_id": tid, "symbol": tid.split(":")[1], "status": status, "opened_ts": OPEN,
        "closed_ts": OPEN + closed_k * H8 if closed_k else None, "slot_notional": 2000.0,
        "state": {"spot_e": 10.0, "fees": 0.0025}, "trade": {"pnl_usd": pnl} if status == "closed" else None,
    }  # fmt: skip


def _marks(ks: list[int], spot: float = 9.0, pnl: float = 5.0) -> list[dict[str, Any]]:
    return [{"cycle_ts": C0 + k * H8, "payload": {"pnl_usd": pnl - k, "spot": spot}} for k in ks]


SIDE = carry_side_cost(PARAMS_A.fee_1, PARAMS_A.fee_2, PARAMS_A.slippage)


def _shadow(positions, funding, marks, now_k: int = 40):
    return a_v2_shadow(positions, funding, marks, now_ms=OPEN + now_k * H8, side_cost=SIDE, decision_delay_ms=DECISION_DELAY_MS)


def test_shadow_without_vanish_is_v1_trade_for_trade():
    pays = _pay(list(range(1, 41)))
    pos = [_pos("A:X:1", "closed", closed_k=12, pnl=-3.5), _pos("A:Y:1", "open"), _pos("A:Z:1", "open")]
    marks = {"A:Y:1": _marks([1, 2, 3])}
    out = _shadow(pos, {p["trade_id"]: pays for p in pos}, marks)
    assert [x["v2_status"] for x in out["positions"]] == ["same_as_v1"] * 3
    assert [x["v2_pnl_usd"] for x in out["positions"]] == [-3.5, 2.0, -0.0025 * 2000.0]  # open: latest mark, or -fees
    assert out["v1_pnl_usd"] == out["v2_pnl_usd"] and out["diff_usd"] == 0.0 and out["rule_fired"] == 0
    assert out["tag"] == A_V2_TAG != PARAMS_A.tag


def test_shadow_prices_the_rule_exit_at_the_first_mark_after_the_rule():
    f = _pay([1, 2, 3])  # rule at cycle 5; the frozen engine holds it aside, so the next mark is at cycle 6
    pos = [_pos("A:X:1", "closed", closed_k=20, pnl=-400.0)]
    marks = {"A:X:1": _marks([1, 2, 3, 6, 9], spot=9.0, pnl=30.0)}
    x = _shadow(pos, {"A:X:1": f}, marks)["positions"][0]
    assert x["rule_ts"] == OPEN + 5 * H8 and x["v2_status"] == "rule_exit" and x["v2_exit_ts"] == OPEN + 6 * H8
    assert x["v2_pnl_usd"] == pytest.approx(30.0 - 6 - SIDE * 9.0 / 10.0 * 2000.0)
    assert x["v1_pnl_usd"] == -400.0


def test_shadow_rule_at_v1_exit_and_unpriced():
    f = _pay([1, 2, 3])
    closed_same = _pos("A:X:1", "closed", closed_k=6, pnl=-7.0)  # no mark between the rule and A's own close
    hanging = _pos("A:Y:1", "open")  # the rule is due, no mark since (data unavailable)
    out = _shadow([closed_same, hanging], {"A:X:1": f, "A:Y:1": f}, {"A:X:1": _marks([1, 2, 3]), "A:Y:1": _marks([1, 2, 3])})
    a, b = out["positions"]
    assert (a["v2_status"], a["v2_pnl_usd"], a["v2_exit_ts"]) == ("rule_at_v1_exit", -7.0, OPEN + 6 * H8)
    assert (b["v2_status"], b["v2_pnl_usd"]) == ("exit_due_unpriced", None)
    assert out["unpriced"] == 1 and out["v1_pnl_usd"] == out["v2_pnl_usd"] == -7.0  # unpriced left out of both totals


def test_description_states_the_backtest_and_its_limits():
    for s in ("-27.3 pp", "+7.3 pp", "contains 0", "not a proven", "mechanism", "rule (iii)"):
        assert s in A_V2_DESCRIPTION, s


def test_frozen_a_is_unchanged():
    assert PARAMS_A.hash() == "3f98b18a034a"  # the hash every recorded cycle of book A carries
    assert PARAMS_A.tag == "cc-paper" and (PARAMS_A.slots, PARAMS_A.entry_apr, PARAMS_A.exit_apr) == (5, 0.20, 0.0)


class VanishFeed(FixtureFeed):
    """From ``cut`` on, ``symbol`` has no funding payments (delisted perp); its bars stay, as live klines do."""

    def __init__(self, markets: dict[str, Any], symbol: str, cut: int) -> None:
        super().__init__(markets, "A")
        self.symbol, self.cut = symbol, cut

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        data = await super().load(params, decision_ts, open_positions)
        d = data.markets[self.symbol]
        data.markets = {**data.markets, self.symbol: {**d, "funding": [e for e in d["funding"] if e["ts_ms"] < self.cut]}}
        return data


async def _book(tmp_path, vanish: bool) -> tuple[str, int, str | None, int]:
    db = str(tmp_path / ("v.sqlite" if vanish else "n.sqlite"))
    store = Store(db)
    await store.init()
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), max(e["ts_ms"] for d in mk.values() for e in d["funding"]))
    plain = FixtureFeed(mk, "A")
    i = 0
    while not await store.cp_positions("A", "open"):
        await _drive(store, plain, params, [times[i]])
        i += 1
    sym = (await store.cp_positions("A", "open"))[0]["symbol"]
    feed = VanishFeed(mk, sym, cut=times[i] - DECISION_DELAY_MS) if vanish else plain
    await _drive(store, feed, params, times[i : i + 12])
    await store.aclose()
    return db, times[i + 11], sym, times[i]


async def test_report_shows_a_v2_next_to_the_unchanged_a_book(tmp_path):
    db, last, sym, cut_t = await _book(tmp_path, vanish=True)
    text = await rep.build_report(db, last + 60_000)
    books, v2 = text.split("## A v2")
    assert "## A: cash-and-carry" in books and "## B: perp-perp" in books  # the frozen books come first, as before
    assert A_V2_TAG in v2 and "not a proven improvement" in v2 and "funding condition only" in v2
    row = next(line for line in v2.splitlines() if line.startswith(f"| {sym} |"))
    assert "rule_exit" in row

    store = Store(db)
    await store.init()
    try:
        view = await rep.a_v2_view(store, last + 60_000)
        positions = await store.cp_positions("A", limit=100_000)
    finally:
        await store.aclose()
    x = next(p for p in view["positions"] if p["symbol"] == sym)
    # no payment from the cut cycle on: the rule fires one cycle later; the engine holds the position aside for two
    # cycles and marks it on the third (gap confirmation), which prices the v2 exit
    assert x["rule_ts"] == cut_t + H8 and x["v2_exit_ts"] == cut_t + 2 * H8
    assert all(p["v2_status"] == "same_as_v1" for p in view["positions"] if p["symbol"] != sym)
    assert view["v1_pnl_usd"] == pytest.approx(sum(p["v1_pnl_usd"] for p in view["positions"]))
    # book A's own records are what the frozen engine wrote: A v2 adds no position, cycle or log row
    assert all(p["trade_id"].startswith("A:") for p in positions) and {p["trade_id"] for p in positions} == {
        p["trade_id"] for p in view["positions"]
    }


async def test_report_a_v2_equals_a_when_no_leg_vanishes(tmp_path):
    db, last, _, _ = await _book(tmp_path, vanish=False)
    store = Store(db)
    await store.init()
    try:
        view = await rep.a_v2_view(store, last + 60_000)
        from types import SimpleNamespace

        book = await rep.book_view(SimpleNamespace(store=store), "A", last + 60_000, include_closed=False)  # type: ignore[arg-type]
    finally:
        await store.aclose()
    assert view["rule_fired"] == 0 and view["diff_usd"] == 0.0
    assert view["v1_pnl_usd"] == pytest.approx(book["pnl_usd"]["total"], abs=0.01)  # the same total as paper_carry_status
