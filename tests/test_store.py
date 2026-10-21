from __future__ import annotations

import pytest

from tradedesk.store import SCHEMA_VERSION, Store


@pytest.fixture
async def store() -> Store:
    s = Store(":memory:")
    await s.init()
    yield s
    await s.aclose()


async def test_schema_bootstrap(store: Store):
    assert await store.version() == SCHEMA_VERSION
    for t in ("atm_term", "funding_hist", "basis_hist", "snapshots"):
        assert await store.table_count(t) == 0


async def test_atm_term_write_read_replaces_on_duplicate(store: Store):
    rows = [
        {"expiry": "2026-10-30T08:00:00+00:00", "days": 20.0, "forward": 83000.0, "atm_iv": 0.42, "rr25": -0.03, "fly25": 0.009},
        {"expiry": "2026-11-27T08:00:00+00:00", "days": 48.0, "forward": 83500.0, "atm_iv": 0.46, "rr25": -0.025, "fly25": 0.012},
    ]
    inserted = await store.insert_atm_term(ts=1_000_000, currency="BTC", rows=rows)
    assert inserted == 2
    assert await store.table_count("atm_term") == 2
    series = await store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000)
    assert len(series) == 1 and series[0]["atm_iv"] == 0.42

    # Same (ts, currency, expiry) is REPLACEd, not duplicated.
    rows[0]["atm_iv"] = 0.44
    await store.insert_atm_term(ts=1_000_000, currency="BTC", rows=rows[:1])
    assert await store.table_count("atm_term") == 2
    series = await store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000)
    assert series[0]["atm_iv"] == 0.44


async def test_funding_and_basis_round_trip(store: Store):
    await store.insert_funding(
        ts=1_000_000,
        rows=[
            {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.1095},
            {"venue": "bybit", "base": "BTC", "rate_per_interval": 0.00008, "interval_hours": 8.0, "annualized": 0.0876},
        ],
    )
    s = await store.funding_series("binanceusdm", "BTC", 0, 2_000_000)
    assert len(s) == 1 and s[0]["annualized"] == pytest.approx(0.1095)

    await store.insert_basis(
        ts=1_000_000,
        currency="BTC",
        rows=[{"expiry": "2026-12-25T08:00:00+00:00", "days": 76.0, "basis_pct": 0.011, "annualized_simple": 0.053}],
    )
    b = await store.basis_series("BTC", "2026-12-25T08:00:00+00:00", 0, 2_000_000)
    assert len(b) == 1 and b[0]["basis_pct"] == pytest.approx(0.011)


async def test_snapshot_json_round_trip(store: Store):
    sid = await store.insert_snapshot(ts=1_000_000, kind="iv_surface", payload={"currency": "BTC", "atm": 0.42})
    assert sid > 0
    assert await store.table_count("snapshots") == 1


async def test_migration_block_is_atomic_and_retried_after_a_failure(tmp_path, monkeypatch):
    from tradedesk.store import schema

    path = tmp_path / "half.sqlite"
    s = Store(path)
    await s.init()
    await s.aclose()

    v = SCHEMA_VERSION + 1
    monkeypatch.setitem(
        schema.MIGRATIONS,
        v,
        ["ALTER TABLE atm_term ADD COLUMN probe_a REAL", "ALTER TABLE no_such_table ADD COLUMN probe_b REAL"],
    )
    s = Store(path)
    with pytest.raises(Exception, match="no_such_table"):
        await s.init()
    assert s._conn is None  # failed init leaves no half-open connection behind

    s = Store(path)
    monkeypatch.setitem(schema.MIGRATIONS, v, ["SELECT 1"])
    await s.init()
    try:
        assert await s.version() == v
        cur = await s.conn.execute("PRAGMA table_info(atm_term)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "probe_a" not in cols  # the first ALTER of the failed block was rolled back

        monkeypatch.setitem(schema.MIGRATIONS, v + 1, ["ALTER TABLE atm_term ADD COLUMN probe_a REAL"])
        await s.aclose()
        await s.init()  # the same ALTER now applies cleanly: no "duplicate column"
        assert await s.version() == v + 1
    finally:
        await s.aclose()
