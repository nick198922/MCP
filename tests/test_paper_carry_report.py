"""scripts/paper_carry_report.py: weekly report from a DB copy (no network)."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from carry_synth import H8, markets_a
from test_carry_paper_engine import SLOTS, FixtureFeed, _drive, _grid
from tradedesk.collector.carry_paper_engine import PARAMS_A
from tradedesk.store import Store
from tradedesk.tools.paper_carry import book_view

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import paper_carry_report as rep  # noqa: E402


async def test_report_renders_from_a_db_copy(tmp_path):
    db = str(tmp_path / "copy.sqlite")
    store = Store(db)
    await store.init()
    mk = markets_a(days=110)
    params = replace(PARAMS_A, slots=SLOTS, entry_apr=0.10)
    times = _grid(min(d["funding"][0]["ts_ms"] for d in mk.values()), max(e["ts_ms"] for d in mk.values() for e in d["funding"]))
    await _drive(store, FixtureFeed(mk, "A"), params, times)
    await store.aclose()

    text = await rep.build_report(db, times[-1] + H8)
    assert "## A: cash-and-carry" in text and "## B: perp-perp" in text
    assert "Funding collected" in text and "actual vs model" in text
    assert "about zero or negative" in text and "-8.7 %" in text  # B expectation, never +76 %
    assert "artefact of holes" in text  # +76 % appears only as a debunked number
    assert "Engine: " in text and "closed trades" in text.lower()
    assert "Deferred trades" in text

    # numbers, not only strings: the report equals book_view's figures
    store2 = Store(db)
    await store2.init()
    try:
        v = await book_view(SimpleNamespace(store=store2), "A", times[-1] + H8, include_closed=False)  # type: ignore[arg-type]
    finally:
        await store2.aclose()
    a_section = text.split("## B:")[0]
    assert f"closed trades: {v['closed'].get('n', 0)}" in a_section
    assert f"realised {rep._usd(v['pnl_usd']['realised'])}" in a_section
    assert f"total {rep._usd(v['pnl_usd']['total'])}" in a_section
    assert f"Open positions: {len(v['open'])}" in a_section


def test_deferred_line_counts_share_and_applies_registered_thresholds():
    assert "3 of 20 (15.0 %)" in rep.deferred_line(3, 20) and "not assessable" not in rep.deferred_line(3, 20)
    assert "6 of 20 (30.0 %)" in rep.deferred_line(6, 20) and "above 20%" in rep.deferred_line(6, 20)
    assert "fewer than 10 trades" in rep.deferred_line(0, 5)
    assert "0 of 0 (n/a)" in rep.deferred_line(0, 0)
