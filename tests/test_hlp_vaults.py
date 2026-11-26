"""Item 471 of the standing card: the positions job polls every HLP vault (parent and children from ``vaultDetails``)
whatever its leaderboard rank, under venue 'hyperliquid_hlp', without changing what the registered forwards read
(fixture: the real HLP vaultDetails of 2026-11-26, portfolio and followers emptied)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics import collector_health as ch
from tradedesk.collector.scheduler import HLP_VENUE, _collect_trader_positions, _collect_traders
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.providers.hyperliquid import HLP_VAULT, MAX_VAULT_CHILDREN, parse_vault_children
from tradedesk.runtime import Runtime

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
VAULT_DETAILS = json.loads((FIX / "hyperliquid_vault_details_hlp_2026-11-26.json").read_text(encoding="utf-8"))
HL_BOARD = json.loads((FIX / "hyperliquid_leaderboard_sample_2026-10-15.json").read_text(encoding="utf-8"))
CHILDREN = VAULT_DETAILS["relationship"]["data"]["childAddresses"]
STRATEGY_A, LIQUIDATOR = "0x010461c14e146ac35fe42271bdc1134ee31c703a", "0x2e3d94f0562703b25c83308a05046ddaf9a8dd14"
HL_A, HL_B = "0x" + "0" * 39 + "1", "0x" + "0" * 39 + "2"  # the leaderboard fixture's two watched accounts

sys.path.insert(0, str(ROOT / "scripts"))
import traders_report as tr  # noqa: E402
import verify_hl_liq_magnet_step0 as step0  # noqa: E402
import verify_smart_money as vsm  # noqa: E402

_spec = importlib.util.spec_from_file_location("verify_smart_money_slice1", ROOT / "scripts" / "verify_smart_money_slice1.py")
slice1 = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(slice1)


def _pos(coin: str, szi: float, px: float) -> dict:
    return {
        "type": "oneWay",
        "position": {
            "coin": coin, "szi": str(szi), "entryPx": str(px), "positionValue": str(abs(szi) * px), "unrealizedPnl": "0",
            "leverage": {"type": "cross", "value": 5}, "liquidationPx": None,
        },
    }  # fmt: skip


def _state(*positions: dict) -> dict:
    return {"assetPositions": list(positions), "marginSummary": {}}


# ------------------------------------------------------------------------------------------------- provider


def test_the_real_answer_gives_the_seven_children_in_order():
    assert VAULT_DETAILS["vaultAddress"] == HLP_VAULT and VAULT_DETAILS["relationship"]["type"] == "parent"
    kids = parse_vault_children(VAULT_DETAILS, HLP_VAULT)
    assert kids == CHILDREN and len(kids) == 7 and STRATEGY_A in kids and LIQUIDATOR in kids


def test_untrusted_children_are_validated_deduplicated_and_capped():
    bad = ["0xNOT-AN-ADDRESS", "0x" + "a" * 39, None, 42, {"a": 1}, HLP_VAULT, "0x" + "g" * 40, " " + CHILDREN[0]]
    raw = dict(
        VAULT_DETAILS,
        relationship={
            "type": "parent",
            "data": {"childAddresses": [*bad, CHILDREN[0].upper().replace("0X", "0x"), CHILDREN[0], CHILDREN[1]]},
        },
    )
    assert parse_vault_children(raw, HLP_VAULT) == CHILDREN[:2]  # upper-case hex lower-cased, the copy dropped
    many = [f"0x{i:040x}" for i in range(1, 100)]
    raw = dict(VAULT_DETAILS, relationship={"type": "parent", "data": {"childAddresses": many}})
    assert parse_vault_children(raw, HLP_VAULT) == many[:MAX_VAULT_CHILDREN]
    for broken in (
        [],
        {"error": "x"},
        dict(VAULT_DETAILS, vaultAddress="0x" + "1" * 40),  # an answer about another vault
        dict(VAULT_DETAILS, relationship={"type": "child", "data": {"parent": HLP_VAULT}}),
        dict(VAULT_DETAILS, relationship=None),
        dict(VAULT_DETAILS, relationship={"type": "parent", "data": {"childAddresses": "0x" + "1" * 40}}),
        dict(VAULT_DETAILS, relationship={"type": "parent", "data": {"childAddresses": bad}}),
    ):
        with pytest.raises(ProviderError):
            parse_vault_children(broken, HLP_VAULT)


# ------------------------------------------------------------------------------------------------- collector


def _runtime(state, db: str, book: dict[str, dict], *, vault_status: int = 200, failing: set[str] | None = None) -> Runtime:
    """HL mock: the leaderboard fixture, ``vaultDetails`` of HLP (or HTTP ``vault_status``), ``clearinghouseState`` from
    ``book`` (read per request, flat when absent; HTTP 500 for addresses in ``failing``). Polymarket is down."""
    calls: list[str] = []
    base = make_transports(state)
    inner = base["hyperliquid"]

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("type") == "vaultDetails":
            calls.append(f"vault:{body['vaultAddress']}")
            return httpx.Response(vault_status, json=VAULT_DETAILS if vault_status == 200 else {"error": "down"})
        if body.get("type") == "clearinghouseState":
            calls.append(f"state:{body['user']}")
            if failing and body["user"] in failing:
                return httpx.Response(500, text="down")
            return httpx.Response(200, json=book.get(body["user"], _state()))
        return inner.handler(req)

    base["hyperliquid:stats"] = httpx.MockTransport(lambda req: httpx.Response(200, json=HL_BOARD))
    base["hyperliquid"] = httpx.MockTransport(hl)
    base["polymarket:data"] = httpx.MockTransport(lambda req: httpx.Response(500, text="down"))
    s = Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=db, collector_enabled=True, traders_top_n=5)
    rt = Runtime(s, transports=base, exchange_factory=FakeEx)
    for h in (rt.hyperliquid.http, rt.hyperliquid.stats, rt.polymarket.data):  # production limits only make the test wait
        h._limiter = TokenBucket(1_000.0, 1_000)
    rt.now = lambda: NOW
    rt._calls = calls  # type: ignore[attr-defined]
    return rt


async def _markers(rt, venue: str = HLP_VENUE) -> dict[str, int]:
    cur = await rt.store.conn.execute("SELECT account, ok FROM trader_polls WHERE venue = ? AND ts = ?", (venue, int(rt.now().timestamp())))
    return {a: ok for a, ok in await cur.fetchall()}


def _book() -> dict[str, dict]:
    return {
        HL_A: _state(_pos("BTC", 70.0, 80_000.0), _pos("ETH", -1_000.0, 3_000.0)),
        HL_B: _state(_pos("SOL", 500.0, 150.0)),
        STRATEGY_A: _state(_pos("BTC", -2.0, 80_000.0), _pos("DOGE", 1e6, 0.2)),
        LIQUIDATOR: _state(_pos("HYPE", 100.0, 30.0)),
    }


async def test_every_hlp_vault_is_polled_whatever_its_rank(state, tmp_path):
    rt = _runtime(state, str(tmp_path / "a.sqlite"), _book())
    try:
        await rt.store.init()
        await _collect_traders(rt)
        watched = await rt.store.tracked_traders("hyperliquid", 5, rt.settings.traders_min_account_usd)
        assert sorted(watched) == [HL_A, HL_B] and not set(CHILDREN) & set(watched)  # no HLP vault ranks here
        assert await _collect_trader_positions(rt) == 3 + 3  # HL_A 2 + HL_B 1; Strategy A 2 + Liquidator 1 (others flat)
        polled = [c.removeprefix("state:") for c in rt._calls if c.startswith("state:")]  # type: ignore[attr-defined]
        assert polled == [*watched, HLP_VAULT, *CHILDREN]  # watched first, then the parent and every child
        assert await _markers(rt) == dict.fromkeys([HLP_VAULT, *CHILDREN], 1)
        assert await _markers(rt, "hyperliquid") == {HL_A: 1, HL_B: 1}  # the watched list is untouched
        cur = await rt.store.conn.execute(
            "SELECT account, market, side FROM trader_positions WHERE venue = ? ORDER BY account, market", (HLP_VENUE,)
        )
        assert [tuple(r) for r in await cur.fetchall()] == [
            (STRATEGY_A, "BTC", "short"), (STRATEGY_A, "DOGE", "long"), (LIQUIDATOR, "HYPE", "long"),
        ]  # fmt: skip
        cur = await rt.store.conn.execute("SELECT COUNT(*) FROM trader_position_state WHERE venue = ?", (HLP_VENUE,))
        assert (await cur.fetchone())[0] == 8  # flat vaults keep a 'flat' state, so their later opens are changes
    finally:
        await rt.aclose()


async def test_vault_details_down_polls_the_last_known_list_or_nobody(state, tmp_path):
    book = _book()
    rt = _runtime(state, str(tmp_path / "a.sqlite"), book, vault_status=500)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        assert await _collect_trader_positions(rt) == 3  # no list known yet: the watched accounts only, no error
        assert await _markers(rt) == {}
        assert await _markers(rt, "hyperliquid") == {HL_A: 1, HL_B: 1}
    finally:
        await rt.aclose()

    rt = _runtime(state, str(tmp_path / "b.sqlite"), book)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        await _collect_trader_positions(rt)  # vaultDetails answers: the list is known from this run's markers
    finally:
        await rt.aclose()
    book[LIQUIDATOR] = _state()  # the liquidator went flat
    rt = _runtime(state, str(tmp_path / "b.sqlite"), book, vault_status=500, failing={CHILDREN[3]})
    rt.now = lambda: NOW + timedelta(hours=1)
    try:
        await rt.store.init()
        assert await _collect_trader_positions(rt) == 2 + 1  # HL_A's two positions >= $2.5M repeat; the liquidator's close
        assert await _markers(rt) == dict.fromkeys([HLP_VAULT, *CHILDREN], 1) | {CHILDREN[3]: 0}  # one child failed alone
        cur = await rt.store.conn.execute(
            "SELECT side FROM trader_positions WHERE venue = ? AND account = ? AND ts = ?",
            (HLP_VENUE, LIQUIDATOR, int(rt.now().timestamp())),
        )
        assert [r[0] for r in await cur.fetchall()] == ["closed"]
    finally:
        await rt.aclose()


async def test_no_hlp_row_at_a_ts_without_another_row(state, tmp_path):
    """The HLP rows never add a run time of their own: a run that wrote nothing else does not poll HLP."""
    rt = _runtime(state, str(tmp_path / "a.sqlite"), {STRATEGY_A: _state(_pos("BTC", -2.0, 80_000.0))})
    try:
        await rt.store.init()
        await _collect_traders(rt)  # HL_A / HL_B flat at every run: a baseline state, no row
        assert await _collect_trader_positions(rt) == 0
        assert await rt.store.table_count("trader_positions") == 0 and await _markers(rt) == {}
    finally:
        await rt.aclose()


async def test_a_store_error_in_the_hlp_part_does_not_fail_the_job(state, tmp_path):
    rt = _runtime(state, str(tmp_path / "a.sqlite"), _book())
    try:
        await rt.store.init()
        await _collect_traders(rt)
        mark_ok = rt.store.set_trader_poll_ok

        async def broken(ts: int, venue: str, account: str) -> None:
            if venue == HLP_VENUE:
                raise sqlite3.OperationalError("database is locked")
            await mark_ok(ts, venue, account)

        rt.store.set_trader_poll_ok = broken
        assert await _collect_trader_positions(rt) == 3  # the watched accounts' rows count, the job does not raise
        assert await _markers(rt, "hyperliquid") == {HL_A: 1, HL_B: 1}
        assert set((await _markers(rt)).values()) == {0}  # health reads trader_polls.ok = 1: the HLP series goes stale
    finally:
        await rt.aclose()


# ------------------------------------------------------------------------------------------------- registered readers


async def _three_runs(state, db: str, *, hlp: bool) -> None:
    """Baseline, a run with changes and closes in both the watched log and HLP, a quiet run. Strategy A is also in the
    top list (ranked by an extra leaderboard row), so it is logged under both venues."""
    book = _book()
    rt = _runtime(state, db, book, vault_status=200 if hlp else 500)
    try:
        await rt.store.init()
        await _collect_traders(rt)
        day = NOW.strftime("%Y-%m-%d")
        top = {"account": STRATEGY_A, "account_value": 9e7, "pnl_month": 1e12, "pnl_all": 1e12}
        await rt.store.insert_trader_snapshot(day, int(NOW.timestamp()), "hyperliquid", [top])
        for hour in range(3):
            rt.now = lambda h=hour: NOW + timedelta(hours=h)
            if hour == 1:
                book[HL_B] = _state()  # the watched account closes SOL
                book[STRATEGY_A] = _state(_pos("BTC", -3.0, 80_000.0))  # DOGE closed, BTC grown
                book[LIQUIDATOR] = _state()
            await _collect_trader_positions(rt)
    finally:
        await rt.aclose()


def _readers(db: str) -> dict:
    """Everything the registered forwards read from the positions tables, through their own functions."""
    end = int((NOW + timedelta(days=1)).timestamp())
    out: dict = {
        "step0_store": step0.load_store(db, end),
        "step0_polls_from": step0.load_polls_from(db),
        "step0_recount": step0.load_recount_store(db, int(NOW.timestamp()), end),
    }
    out["step0_groups"] = {a: step0.account_groups(db, a, end) for a in out["step0_store"]["accounts"]}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out["vsm_runs"] = {v: vsm.position_runs(con, v) for v in ("hyperliquid", "polymarket")}
        out["vsm_tracked"] = vsm.tracked_accounts(con)
        out["s1_runs"] = slice1.hl_runs(con, end)
        out["s1_polls"] = {t: slice1.polls_at(con, t) for t in out["s1_runs"]}
        out["s1_rows"] = list(slice1.position_rows(con, "hyperliquid", end))
        out["s1_delta"] = slice1.delta_start_of(con)
        out["report"] = tr.render(con, "db", 7, NOW + timedelta(hours=3))
        out["max_ts"] = con.execute("SELECT MAX(ts) FROM trader_positions").fetchone()[0]  # dashboard freshness
        for t in ("trader_positions", "trader_polls", "trader_position_state"):
            cols = "venue, account, digest, ts, positions_json" if t == "trader_position_state" else "*"
            out[t] = sorted(map(tuple, con.execute(f"SELECT {cols} FROM {t} WHERE venue != ?", (HLP_VENUE,))))
    finally:
        con.close()
    return out


async def test_registered_readers_see_the_same_data_with_and_without_hlp(state, tmp_path):
    without, with_hlp = str(tmp_path / "without.sqlite"), str(tmp_path / "with.sqlite")
    await _three_runs(state, without, hlp=False)
    await _three_runs(state, with_hlp, hlp=True)
    con = sqlite3.connect(with_hlp)
    try:
        hlp_ts = {r[0] for r in con.execute("SELECT DISTINCT ts FROM trader_positions WHERE venue = ?", (HLP_VENUE,))}
        hlp_closed = con.execute("SELECT COUNT(*) FROM trader_positions WHERE venue = ? AND side = 'closed'", (HLP_VENUE,)).fetchone()[0]
        both = con.execute("SELECT COUNT(DISTINCT venue) FROM trader_positions WHERE account = ?", (STRATEGY_A,)).fetchone()[0]
    finally:
        con.close()
    assert len(hlp_ts) == 2 and hlp_closed == 2 and both == 2  # HLP really wrote rows and closes, Strategy A in both
    a, b = _readers(without), _readers(with_hlp)
    assert a["s1_delta"] is not None and a["step0_store"]["closed"]  # the cut-over and closes are exercised
    assert set(a) == set(b)
    for k in a:
        assert a[k] == b[k], k


# ------------------------------------------------------------------------------------------------- collector health


def test_health_counts_hlp_on_its_own_and_never_for_the_watched_accounts(tmp_path):
    import collector_health as script  # noqa: PLC0415  # scripts/ is on sys.path above

    from tradedesk.store import Store  # noqa: PLC0415

    db = str(tmp_path / "h.sqlite")

    async def init() -> None:
        st = Store(db)
        await st.init()
        await st.aclose()

    import asyncio  # noqa: PLC0415

    asyncio.run(init())
    now = int(NOW.timestamp()) - int(NOW.timestamp()) % 3600 + 1800
    con = sqlite3.connect(db)
    for k in range(1, 8 * 24):
        ts = now - k * 3600
        con.execute(
            "INSERT INTO trader_positions (ts, venue, account, market, side) VALUES (?,?,?,?,?)",
            (ts, HLP_VENUE, STRATEGY_A, "BTC", "short"),
        )
        con.execute("INSERT INTO trader_polls (venue, ts, account, ok) VALUES (?,?,?,1)", (HLP_VENUE, ts, STRATEGY_A))
        if k > 10:  # the watched accounts stopped 10 h ago, HLP did not
            con.execute(
                "INSERT INTO trader_positions (ts, venue, account, market, side) VALUES (?,?,?,?,?)",
                (ts, "hyperliquid", HL_A, "BTC", "long"),
            )
    con.commit()
    con.close()
    only = {"TRADEDESK_COLLECTOR_ENABLED": "0", "TRADEDESK_COLLECT_SUPPLY_ENABLED": "0"}
    res = {r["label"]: r for r in script.collect(db, only, now)}
    assert res["trader_positions:hyperliquid"]["status"] == "stale"  # not masked by the HLP rows of the same hours
    assert res["trader_positions:hyperliquid_hlp"]["status"] == "ok"
    con = sqlite3.connect(db)
    con.execute("UPDATE trader_polls SET ok = 0 WHERE ts > ?", (now - 6 * 3600,))  # HLP polls failing for 5 h
    con.commit()
    con.close()
    res = {r["label"]: r for r in script.collect(db, only, now)}
    assert res["trader_positions:hyperliquid_hlp"]["status"] == "stale"
    assert {s.groups for s in ch.SPECS if s.name == "trader_positions"} == {("hyperliquid", "polymarket"), ("hyperliquid_hlp",)}
