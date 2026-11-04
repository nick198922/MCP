"""Scripts behind forward reports and snapshots (item 68 of IDEA-20261102-standing-improvements), on fixtures only:
``traders_report.py`` (weekly smart-money board from a gzipped backup, opened read-only), ``paper_carry_once.py`` (one
carry A / B cycle into a throwaway SQLite file) and ``paper_ladder_snapshot.py`` (Bybit risk-limit ladder, paginated).
No network: the live feed, the clock and the exchange call are replaced."""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carry_synth import DAY, T0, markets_a, markets_b
from tradedesk.collector.carry_paper_engine import CarryParams, FeedData
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import paper_carry_once as poc  # noqa: E402
import paper_ladder_snapshot as pls  # noqa: E402
import traders_report as tr  # noqa: E402

# ---------------------------------------------------------------------------------------------------------------------
# traders_report.py

T1 = 1_792_972_800  # 2026-10-26 00:00 UTC, first run of the positions log
T2 = T1 + 3 * 86_400  # first run with a 'closed' row (schema v12 deltas)
T3 = T1 + 6 * 86_400  # last run
A1, A2, A3, SYS = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20, "0x" + "ff" * 20
W1 = "0x" + "b1" * 20
EVIL = "Will X | win? [link](http://evil) <b>`x`</b>"


def _hl_snap(day: str, account: str, *, value: float, pnl_week: float, pnl_month: float, roi: float = 0.1, name: str = "") -> tuple:
    return (day, "hyperliquid", account, T3, name, value, 1_000.0, pnl_week, pnl_month, 5e6, roi, 1.0, value, None)


async def _make_traders_db(path: Path, with_positions: bool = True) -> None:
    store = Store(path)  # the collector's own schema, so a renamed column breaks this test
    await store.init()
    await store.aclose()
    conn = sqlite3.connect(path)
    snaps = [
        _hl_snap("2026-11-01", A1, value=1e6, pnl_week=50_000, pnl_month=900_000, name="Alpha"),
        _hl_snap("2026-10-25", A1, value=1e6, pnl_week=-10_000, pnl_month=100_000),
        _hl_snap("2026-11-01", A2, value=1e6, pnl_week=1_000, pnl_month=400_000),
        _hl_snap("2026-11-01", A3, value=1e6, pnl_week=1_000, pnl_month=300_000, roi=15.0),
        _hl_snap("2026-11-01", SYS, value=2e9, pnl_week=1e6, pnl_month=5e6, name="HLP"),
        *[_hl_snap("2026-11-01", f"0x{k:040x}", value=1e6, pnl_week=0, pnl_month=1_000.0 * k) for k in range(1, 4)],
        ("2026-11-01", "polymarket", W1, T3, "whale", None, 10.0, 20.0, 3_000.0, 9_000.0, None, None, 50_000.0, 1),
    ]
    conn.executemany(
        "INSERT INTO trader_snapshot (day, venue, account, ts, name, account_value, pnl_day, pnl_week, pnl_month, pnl_all, "
        "roi_month, roi_all, vlm_month, rank_month) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        snaps,
    )
    if with_positions:
        pos = [
            # (ts, venue, account, market, side, size, entry_px, value_usd, upnl, leverage, liq_px, title)
            (T1, "hyperliquid", A1, "BTC", "long", 5.0, 95_000.0, 500_000.0, 0.0, 2.0, None, None),
            (T1, "hyperliquid", A2, "ETH", "long", 100.0, 3_000.0, 300_000.0, 0.0, 2.0, None, None),
            (T1, "polymarket", W1, "tok1", "Yes", 1_000.0, 0.6, 600.0, 0.0, None, None, EVIL),
            (T2, "hyperliquid", A2, "ETH", "closed", 100.0, 3_000.0, 300_000.0, 0.0, None, None, None),
            (T3, "hyperliquid", A1, "BTC", "long", 8.0, 95_000.0, 800_000.0, 0.0, 2.0, None, None),
            (T3, "hyperliquid", A1, "SOL", "short", 1_000.0, 210.0, 200_000.0, 0.0, 2.0, None, None),
            (T3, "polymarket", W1, "tok1", "Yes", 2_000.0, 0.6, 1_200.0, 0.0, None, None, EVIL),
            (T3, "polymarket", W1, "tok2", "No", 500.0, 0.2, 100.0, 0.0, None, None, "Rain in Kyiv?"),
        ]
        conn.executemany("INSERT INTO trader_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", pos)
    conn.commit()
    conn.close()


def _backup_gz(src: Path, dst_dir: Path) -> Path:
    """What deploy/backup-sqlite.sh does: an online `.backup` copy, then gzip."""
    dst_dir.mkdir()
    copy = dst_dir / "tradedesk-2026-11-02T000000Z.sqlite"
    a, b = sqlite3.connect(src), sqlite3.connect(copy)
    a.backup(b)
    a.close()
    b.close()
    gz = copy.with_suffix(".sqlite.gz")
    with open(copy, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    copy.unlink()
    return gz


async def test_traders_report_renders_board_from_gzipped_backup(tmp_path, monkeypatch, capsys):
    await _make_traders_db(tmp_path / "live.sqlite")
    gz = _backup_gz(tmp_path / "live.sqlite", tmp_path / "backups")
    out, cache = tmp_path / "docs" / "REPORT.md", tmp_path / "cache"
    argv = ["traders_report.py", "--db", str(gz), "--out", str(out), "--cache-dir", str(cache), "--days", "7"]
    monkeypatch.setattr(sys, "argv", argv)
    tr.main()

    rep = out.read_text(encoding="utf-8")
    assert f"lines -> {out}" in capsys.readouterr().out
    assert "база `tradedesk-2026-11-02T000000Z.sqlite.gz`" in rep
    assert (cache / "tradedesk-2026-11-02T000000Z.sqlite").exists()  # decompressed once into the cache
    # Hyperliquid: 7 accounts on the latest day; without the system one 6 remain and A1 is the only "strong" one
    # (top quintile of 6 by PnL 30 days = 1 account).
    assert "Знімок лідерборду 2026-11-01: 7 акаунтів" in rep and "«сильних» з них: 1." in rep
    assert "(лише 144 год: колектор пише з 2026-10-16)" in rep
    top = [ln for ln in rep.splitlines() if ln.startswith("| 1 | ")]
    assert top[0].startswith(f"| 1 | {tr.short(A1)} | Alpha | напрямний | $1.00 млн |") and top[0].endswith("| 1/2 |")
    assert tr.short(SYS) not in rep  # system account is not ranked
    assert "| +1500.0 % ⚠ |" in rep  # ROI > 1000 %: leaderboard artefact flag
    # Changes: BTC increased (3 coins at the 100k mark), SOL opened, ETH closed through a v12 'closed' delta row.
    assert "| BTC | збільшено | long | 5 → 8 | $300 тис. | $800 тис. |" in rep
    assert "| SOL | відкрито | short | 0 → 1 000 | $200 тис. | $200 тис. |" in rep
    assert "| ETH | закрито | long | 100 → 0 | $300 тис. | $300 тис. |" in rep
    # Net book of the strong accounts and the flow over the window, valued at the last mark.
    assert "| BTC | $800 тис. | $800 тис. | $0 | 1 | $300 тис. |" in rep
    assert "| SOL | −$200 тис. | $0 | $200 тис. | 1 | −$200 тис. |" in rep
    # Polymarket: only the general board has a snapshot; untrusted titles cannot break the table or inject links / HTML.
    assert "## Polymarket — загальний рейтинг" in rep and "крипто-рейтинг" not in rep
    safe = tr.text(EVIL)
    assert safe == "Will X / win? \\[link\\](http://evil) ‹b›'x'‹/b›"
    assert f"| {tr.short(W1)} | {safe} | Yes | збільшено | 1 000 → 2 000 | $600 | $1 тис. |" in rep
    assert f"| {tr.short(W1)} | Rain in Kyiv? | No | відкрито | 0 → 500 | $100 | $100 |" in rep
    assert f"| {safe} | Yes | 1 | $1 тис. |" in rep
    assert "[link](" not in rep and "<b>" not in rep


async def test_traders_report_opens_backup_read_only(tmp_path):
    await _make_traders_db(tmp_path / "live.sqlite")
    gz = _backup_gz(tmp_path / "live.sqlite", tmp_path / "backups")
    conn, source = tr.open_db(str(gz), tmp_path / "cache")
    try:
        assert source == gz.name
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM trader_positions")
    finally:
        conn.close()
    plain, _ = tr.open_db(str(tmp_path / "live.sqlite"), tmp_path / "cache2")  # an uncompressed copy too
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            plain.execute("DELETE FROM trader_snapshot")
    finally:
        plain.close()


async def test_traders_report_without_positions_says_so(tmp_path):
    await _make_traders_db(tmp_path / "live.sqlite", with_positions=False)
    conn, source = tr.open_db(str(tmp_path / "live.sqlite"), tmp_path / "cache")
    try:
        rep = tr.render(conn, source, 7, datetime(2026, 11, 2, tzinfo=UTC))
    finally:
        conn.close()
    assert rep.rstrip().endswith("Даних позицій ще немає.")
    assert "## Hyperliquid" not in rep


# ---------------------------------------------------------------------------------------------------------------------
# paper_carry_once.py

NOW_MS = T0 + 40 * DAY + 3_600_000  # inside the synthetic history (60 days from T0), 1 h after a funding time


class SynthFeed:
    """The carry feed contract (load / book) over carry_synth data; A gets spot pairs, B two perp venues."""

    def __init__(self) -> None:
        self.mk = {"A": markets_a(), "B": markets_b()}
        self.loads: list[str] = []

    async def load(self, params: CarryParams, decision_ts: int, open_positions: list[dict[str, Any]]) -> FeedData:
        self.loads.append(params.strategy)
        mk = self.mk[params.strategy]
        spot = {s: (s.replace("USDT:USDT", "USDT"), 1) for s in mk} if params.strategy == "A" else {}
        return FeedData(markets=mk, spot_symbols=spot, universe_size=len(mk))

    async def book(self, venue: str, symbol: str) -> dict[str, Any] | None:
        return {"bids": [[99.9, 50.0], [99.0, 500.0]], "asks": [[100.1, 50.0], [101.0, 500.0]], "timestamp": None}


async def test_paper_carry_once_records_both_cycles_in_throwaway_db(tmp_path, monkeypatch, capsys):
    feed = SynthFeed()
    monkeypatch.setattr(poc, "LiveFeed", lambda rt: feed)
    monkeypatch.setattr(poc, "time", SimpleNamespace(time=lambda: NOW_MS / 1000))
    db = tmp_path / "check.sqlite"
    await poc.main(str(db), "both")

    printed = capsys.readouterr().out
    cycle = poc.cycle_grid(NOW_MS)
    assert feed.loads == ["A", "B"]
    assert f"== A (cc-paper) cycle {cycle}: {{'status': 'done'" in printed
    assert f"== B (pp-paper) cycle {cycle}: {{'status': 'done'" in printed
    assert "universe 12, with data 12" in printed and "universe 10, with data 10" in printed
    assert printed.count("cost realism") == 2
    assert printed.count("  side ") == 20  # 5 opens x 2 legs per strategy, each with its book spread and cost
    store = Store(db)  # the file outlives the run and holds both cycles, the open positions and their leg sides
    await store.init()
    try:
        for key in ("A", "B"):
            c = await store.cp_cycle_get(key, cycle)
            assert c is not None and c["payload"]["universe_size"] == (12 if key == "A" else 10)
            assert len(await store.cp_positions(key, "open")) == 5
            assert len(await store.cp_logs(key, "side")) == 10
    finally:
        await store.aclose()


async def test_paper_carry_once_single_strategy_and_rerun_is_idempotent(tmp_path, monkeypatch, capsys):
    feed = SynthFeed()
    monkeypatch.setattr(poc, "LiveFeed", lambda rt: feed)
    monkeypatch.setattr(poc, "time", SimpleNamespace(time=lambda: NOW_MS / 1000))
    db = str(tmp_path / "check.sqlite")
    await poc.main(db, "B")
    await poc.main(db, "B")
    printed = capsys.readouterr().out
    assert feed.loads == ["B"]  # the second run finds the cycle and does not load the feed again
    assert "== A " not in printed
    assert "{'status': 'exists'" in printed


# ---------------------------------------------------------------------------------------------------------------------
# paper_ladder_snapshot.py


def _tier(sym: str, limit: float, mmr: float, lev: float) -> dict[str, str]:
    return {"symbol": sym, "riskLimitValue": str(limit), "maintenanceMargin": str(mmr), "maxLeverage": str(lev)}


PAGES = {
    "": {
        "result": {
            "list": [_tier("BTCUSDT", 2_000_000, 0.005, 100), _tier("BTCUSDT", 10_000_000, 0.01, 50), _tier("BTCPERP", 1e6, 0.005, 100)],
            "nextPageCursor": "c1",
        }
    },
    "c1": {"result": {"list": [_tier("TINYUSDT", 1_000, 0.05, 10), _tier("TINYUSDT", 50_000, 0.10, 1)], "nextPageCursor": ""}},
}


class FakeExchanges:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def public_get(self, venue: str, method: str, params: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((venue, method, dict(params)))
        return SimpleNamespace(data=PAGES[params.get("cursor", "")])


async def test_paper_ladder_fetch_walks_the_cursor_and_closes_runtime(monkeypatch):
    ex = FakeExchanges()
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    rt = SimpleNamespace(exchanges=ex, aclose=aclose)
    monkeypatch.setattr(pls, "build_server", lambda: (None, rt))
    rows = await pls.fetch()
    assert [c[2].get("cursor") for c in ex.calls] == [None, "c1"]
    assert all(c[:2] == ("bybit", "publicGetV5MarketRiskLimit") and c[2]["category"] == "linear" for c in ex.calls)
    assert len(rows) == 5 and closed == [True]


def test_paper_ladder_main_writes_summary_and_raw(tmp_path, monkeypatch, capsys):
    async def fake_fetch() -> list[dict[str, Any]]:
        return PAGES[""]["result"]["list"] + PAGES["c1"]["result"]["list"]

    monkeypatch.setattr(pls, "fetch", fake_fetch)
    out, raw = tmp_path / "ladder" / "bybit.json", tmp_path / "raw" / "bybit-raw.json"
    monkeypatch.setattr(sys, "argv", ["paper_ladder_snapshot.py", "--out", str(out), "--raw-out", str(raw)])
    pls.main()

    got = json.loads(out.read_text())
    assert got["leg_notional_usd"] == 2000.0 and got["leverage"] == 2.0
    assert sorted(got["symbols"]) == ["BTCUSDT", "TINYUSDT"]  # non-USDT contracts are skipped
    assert got["symbols"]["BTCUSDT"] == {"mmr": 0.005, "max_position_usd": 10_000_000.0, "fits": True, "n_tiers": 2}
    # TINY: the $2000 leg needs the 50k tier (MMR 10 %), and only the 1k tier allows 2x -> it does not fit.
    assert got["symbols"]["TINYUSDT"] == {"mmr": 0.1, "max_position_usd": 1_000.0, "fits": False, "n_tiers": 2}
    raw_got = json.loads(raw.read_text())
    assert [t["limit_usd"] for t in raw_got["symbols"]["TINYUSDT"]] == [1_000.0, 50_000.0]
    assert "symbols 2; $2000 leg fits at 2x: 1" in capsys.readouterr().out
