"""scripts/demo_topup.py: the one-off demo balance top-up (fake client and a real store, no network)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import demo_topup as dt  # noqa: E402

from tradedesk.store import Store  # noqa: E402


def test_amount_needed():
    assert dt.amount_needed(9_400.6, 50_000) == 40_599
    assert dt.amount_needed(50_000, 50_000) == 0 and dt.amount_needed(0, 50_000) == 0
    assert dt.amount_needed(1_000, 500_000) == dt.DEMO_FUNDS_MAX


class FakeClient:
    def __init__(self, equity):
        self.equity, self.asked = equity, []

    async def equity_usd(self):
        return self.equity

    async def apply_demo_funds(self, usdt):
        self.asked.append(usdt)
        self.equity += usdt
        return {"retCode": 0}


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "t.sqlite"))
    await s.init()
    yield s
    await s.aclose()


async def test_top_up_writes_a_funds_audit_row(store, monkeypatch):
    async def no_wait(_):
        return None

    monkeypatch.setattr(dt.asyncio, "sleep", no_wait)
    c = FakeClient(9_500.0)
    rep = await dt.run(c, store, 1, 50_000, dry_run=False)
    assert c.asked == [40_500] and rep["equity_after"] == 50_000
    rows = await store.exec_audit_list(kinds=("funds",))
    assert len(rows) == 1 and rows[0]["venue"] == dt.VENUE


async def test_dry_run_and_full_balance_ask_nothing(store):
    c = FakeClient(9_500.0)
    assert (await dt.run(c, store, 1, 50_000, dry_run=True))["add_usdt"] == 40_500 and c.asked == []
    c = FakeClient(60_000.0)
    assert (await dt.run(c, store, 1, 50_000, dry_run=False))["add_usdt"] == 0 and c.asked == []
    assert await store.exec_audit_list(kinds=("funds",)) == []
