"""Scripts behind forward reports and snapshots (items 68, 76 and 81 of IDEA-20261102-standing-improvements), on
fixtures only: ``traders_report.py`` (weekly smart-money board from a gzipped backup, opened read-only),
``paper_carry_once.py`` (one carry A / B cycle into a throwaway SQLite file), ``paper_ladder_snapshot.py`` (Bybit
risk-limit ladder, paginated), ``paper_bench_manifest.py`` (sha256 manifest of the carry benchmark cache) and
``stage0_condor_forward.py`` (stage-0 SD report of the condor forward 8j, on a synthetic put_skew cache). No network:
the live feed, the clock and the exchange call are replaced."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
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

import paper_bench_manifest as pbm  # noqa: E402
import paper_carry_once as poc  # noqa: E402
import paper_ladder_snapshot as pls  # noqa: E402
import stage0_condor_forward as s0  # noqa: E402
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


# ---------------------------------------------------------------------------------------------------------------------
# paper_bench_manifest.py

SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"  # FIPS 180-2 test vector for b"abc"
SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _run_manifest(cache: Path, out: Path, monkeypatch) -> list[str]:
    monkeypatch.setattr(sys, "argv", ["paper_bench_manifest.py", "--cache-dir", str(cache), "--out", str(out)])
    pbm.main()
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return text.splitlines()


def _bench_cache(root: Path, names: list[str]) -> Path:
    """A cache like paper_carry_bench: nested venue / kind folders of *.json, plus logs the manifest must skip."""
    for name in names:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"abc" if p.name == "abc.json" else name.encode())
    return root


def _sha256sum_check(manifest: Path, cache: Path) -> subprocess.CompletedProcess[str]:
    """`cd <cache> && sha256sum -c <manifest>`, as the script's docstring says; C locale for stable messages."""
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(["sha256sum", "-c", "--strict", str(manifest)], cwd=cache, env=env, capture_output=True, text=True)


def test_paper_bench_manifest_lines_pass_sha256sum_check(tmp_path, monkeypatch, capsys):
    big = b"x" * ((1 << 20) + 7)  # over one 1 MiB read chunk
    cache = _bench_cache(tmp_path / "cache", ["abc.json", "binance/funding/BTCUSDT.json", "fetch.log", "notes.txt"])
    (cache / "empty.json").write_bytes(b"")
    (cache / "bybit").mkdir()
    (cache / "bybit" / "big.json").write_bytes(big)
    (cache / "dir.json").mkdir()  # a directory matching *.json is not a file and is skipped
    out = tmp_path / "bench-data.sha256"  # outside the cache: the manifest never lists itself
    lines = _run_manifest(cache, out, monkeypatch)

    parsed = [ln.split("  ", 1) for ln in lines]  # `sha256sum` text mode: "<64 hex>  <path>"
    assert all(len(h) == 64 and set(h) <= set("0123456789abcdef") for h, _ in parsed)
    rels = [rel for _, rel in parsed]
    assert rels == ["abc.json", "binance/funding/BTCUSDT.json", "bybit/big.json", "empty.json"]
    assert not any(rel.startswith("/") or str(tmp_path) in rel for rel in rels)  # relative to the cache dir
    got = {rel: h for h, rel in parsed}
    assert got["abc.json"] == SHA256_ABC and got["empty.json"] == SHA256_EMPTY
    assert got["bybit/big.json"] == hashlib.sha256(big).hexdigest() == pbm.sha256_file(cache / "bybit" / "big.json")
    for h, rel in parsed:  # every line re-verifies from the cache, as `cd <cache> && sha256sum -c` would
        assert hashlib.sha256((cache / rel).read_bytes()).hexdigest() == h
    total_mb = (3 + len(b"binance/funding/BTCUSDT.json") + len(big)) / 1e6
    assert f"4 files, {total_mb:.1f} MB -> {out}" in capsys.readouterr().out
    if shutil.which("sha256sum"):  # coreutils itself, when present (Linux CI and the server)
        res = _sha256sum_check(out, cache)
        assert res.returncode == 0, res.stdout + res.stderr
        (cache / "abc.json").write_bytes(b"abd")  # a changed file is caught
        res = _sha256sum_check(out, cache)
        assert res.returncode == 1 and "abc.json: FAILED" in res.stdout


def test_paper_bench_manifest_order_is_deterministic(tmp_path, monkeypatch):
    names = ["okx/z.json", "binance/b.json", "abc.json", "binance/a.json", "bybit/funding/ETH.json", "a.json"]
    one = _run_manifest(_bench_cache(tmp_path / "c1", names), tmp_path / "one.sha256", monkeypatch)
    _run_manifest(_bench_cache(tmp_path / "c2", list(reversed(names))), tmp_path / "two.sha256", monkeypatch)
    # Same content, files created in the opposite order (and in another cache dir) -> byte-identical manifest.
    assert (tmp_path / "one.sha256").read_bytes() == (tmp_path / "two.sha256").read_bytes()
    rels = [ln.split("  ", 1)[1] for ln in one]
    assert rels == ["a.json", "abc.json", "binance/a.json", "binance/b.json", "bybit/funding/ETH.json", "okx/z.json"]


def test_paper_bench_manifest_empty_cache(tmp_path, monkeypatch, capsys):
    # `sha256sum -c` rejects a manifest with no checksum lines (an empty file too), so an empty cache is an error:
    # exit code 1, a message on stderr, and no manifest written or overwritten.
    cache = _bench_cache(tmp_path / "cache", ["fetch.log"])  # only logs: nothing to list
    out = tmp_path / "bench-data.sha256"
    monkeypatch.setattr(sys, "argv", ["paper_bench_manifest.py", "--cache-dir", str(cache), "--out", str(out)])
    with pytest.raises(SystemExit) as exc:
        pbm.main()
    assert exc.value.code != 0 and "cache is empty" in str(exc.value.code)
    assert not out.exists()

    out.write_text("previous manifest\n", encoding="utf-8")  # an earlier good manifest is left untouched
    missing = tmp_path / "no-such-cache"  # a missing cache dir is the same error
    monkeypatch.setattr(sys, "argv", ["paper_bench_manifest.py", "--cache-dir", str(missing), "--out", str(out)])
    with pytest.raises(SystemExit) as exc:
        pbm.main()
    assert exc.value.code != 0 and str(missing) in str(exc.value.code)
    assert out.read_text(encoding="utf-8") == "previous manifest\n"
    assert capsys.readouterr().out == ""  # no "0 files" summary

    # As a real process: the message goes to stderr with exit code 1.
    script = Path(pbm.__file__)
    res = subprocess.run(
        [sys.executable, str(script), "--cache-dir", str(cache), "--out", str(tmp_path / "x.sha256")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and "cache is empty" in res.stderr and res.stdout == ""
    assert not (tmp_path / "x.sha256").exists()


# ---------------------------------------------------------------------------------------------------------------------
# stage0_condor_forward.py

DAY_MS, HOUR_MS = 86_400_000, 3_600_000
IS_FRIDAYS = ("2023-11-03", "2023-11-10", "2023-11-17", "2023-11-24")  # before vom.OOS (2024-01-01)
OOS_FRIDAYS = ("2024-01-05", "2024-01-12", "2024-01-19", "2024-01-26", "2024-02-02")
# delivery / entry spot - 1: inside the condor, between the strikes and through either wing, so the weekly P&L varies
MOVES = (0.01, -0.12, 0.03, 0.14, -0.02, 0.0, -0.09, 0.05, 0.11)
# DVOL candle of the day before each entry (known at entry): far below / above the trailing year -> percentile ~0 / ~1
# (size 0.5 / 1.5), 0.55 inside the base range 0.30-0.70 -> percentile ~0.6 (size 1.0); both samples mix the sizes.
DVOL_AT_ENTRY = (0.05, 2.0, 0.55, 2.0, 0.05, 0.55, 2.0, 0.05, 2.0)
H1_ROWS = [f"H1 {m} band {b:.1%}" for m in ("side", "mark") for b in (0.015, 0.03, 0.05)]
STAGE0_HDR = (
    "| series | n hist | SD | MDE 16 | MDE 26 | MDE 52 | a priori | power 16 / 26 / 52 | weeks for MDE <= a priori "
    "| ref. alpha 0.025: power 52 / weeks |"
)


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def _surface(day: int, spot: float) -> tuple[int, dict[str, Any]]:
    """One serialized `put_skew.build_day` surface (the snapshots_v2.json form: expiries and strikes as [key, value]
    pairs) with only the next-Friday 08:00 expiry: a smile on a 500 USD grid from 0.70 to 1.30 of spot."""
    t_ms, expiry = day + 9 * HOUR_MS, day + 7 * DAY_MS + 8 * HOUR_MS
    fwd = spot * 1.0002
    strikes = []
    for k in range(int(0.70 * spot / 500), int(1.30 * spot / 500) + 1):
        x = math.log(500.0 * k / fwd)
        iv = 0.50 - 0.3 * x + 1.5 * x * x  # put skew plus smile, decimals
        row = {"mark_iv": iv, "mark_age_h": 0.5, "n": 4, "n_put": 2, "sell_iv": iv - 0.01, "buy_iv": iv + 0.01}
        strikes.append([500.0 * k, row])
    exp = {"ratio": 1.0002, "F": fwd, "T": (expiry - t_ms) / (365 * DAY_MS), "n_prints": 200, "strikes": strikes}
    return expiry, {"day": day, "t_ms": t_ms, "spot": spot, "expiries": [[expiry, exp]]}


def _put_skew_cache(root: Path) -> Path:
    """A tiny put-skew-v2 cache for `verify_option_modules.load`: snapshots_v2.json (9 Friday surfaces), the delivery
    prices of their expiries and a daily DVOL series from 2022-10-01. No trades24/ folder: its file count (0) matches
    ``n_files``, so `verify_put_skew.load_snapshots` takes the saved surfaces and never rebuilds them from trades."""
    root.mkdir()
    days, delivery = {}, []
    dvol = {}
    t = _ms("2022-10-01")
    while t <= _ms("2024-02-02"):
        dvol[t] = 0.30 + ((t // DAY_MS) * 37 % 100) / 250.0  # 0.30-0.70, no trend
        t += DAY_MS
    for i, (day, move, dv) in enumerate(zip(IS_FRIDAYS + OOS_FRIDAYS, MOVES, DVOL_AT_ENTRY, strict=True)):
        d, spot = _ms(day), 30_000.0 + 2_500.0 * i
        expiry, snap = _surface(d, spot)
        days[str(d)] = snap
        delivery.append({"date": datetime.fromtimestamp(expiry / 1000, UTC).strftime("%Y-%m-%d"), "delivery_price": spot * (1 + move)})
        dvol[d - DAY_MS] = dv
    days[str(_ms("2023-11-04"))] = None  # an empty day of the real cache: skipped by the loader
    meta = {"pages": 0, "collisions": 0, "trades": 0, "empty_days": [_ms("2023-11-04")]}
    (root / "snapshots_v2.json").write_text(json.dumps({"n_files": 0, "days": days, "meta": meta}))
    (root / "delivery_btc_usd.json").write_text(json.dumps(delivery))
    (root / "dvol_BTC.json").write_text(json.dumps(sorted(dvol.items())))
    return root


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def test_stage0_condor_forward_report_on_synthetic_cache(tmp_path, monkeypatch, capsys):
    src = _put_skew_cache(tmp_path / "put_skew")
    out = tmp_path / "condor_forward" / "stage0.md"
    monkeypatch.setattr(sys, "argv", ["stage0_condor_forward.py", "--src-dir", str(src), "--out", str(out)])
    s0.main()

    rep = out.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "loaded 9 surfaces, 9 delivery" in printed and rep in printed
    lines = rep.splitlines()
    assert lines[0].startswith("## Stage 0: SD per week (no means), MDE = 2.8 SD / sqrt(n)")
    assert STAGE0_HDR in lines and lines.count("|---|---|---|---|---|---|---|---|---|---|") == 1
    rows = [ln for ln in lines if ln.startswith(("| H1 ", "| H4 "))]
    # 6 H1 series (2 cost models x 3 leg bands), then 4 H4 variants x 3 samples; n = 9 weeks, 5 of them OOS.
    assert [_cells(r)[0].split(" (ac1")[0] for r in rows[:6]] == H1_ROWS
    assert len(rows) == 6 + 12
    for r in rows:
        c = _cells(r)
        assert len(c) == 10, r
        n, sd = int(c[1]), float(c[2])
        assert n == (5 if c[0].startswith("H4") and "OOS" in c[0] else 9), r
        assert sd > 0, r
        for h, cell in zip((16, 26, 52), c[3:6], strict=True):  # MDE = 2.8 SD / sqrt(h), both rounded to 0.01
            assert abs(float(cell) - 2.8 * sd / math.sqrt(h)) <= 0.011, r
        assert c[6] == ("0.10" if c[0].startswith("H1") else "0.13")
        assert all(0.0 <= float(p) <= 1.0 for p in c[7].split(" / ")), r
        assert int(c[8]) >= 1 and int(c[9].split(" / ")[1]) >= int(c[8]), r  # alpha 0.025 needs more weeks
    # Cost prior from the saved live Deribit answer (repo fixture), both currencies, the four H1 legs.
    assert "## Cost prior: live answer 2026-11-01T06:08:39.034811+00:00 (expiry 2026-11-06)" in rep
    for cur in ("BTC", "ETH"):
        assert [_cells(ln)[1] for ln in lines if ln.startswith(f"| {cur} | ") and "condor" not in ln] == ["lp", "sp10", "sc10", "lc"]
    assert lines[-1].startswith("peak RSS ")
    # Stage 0 prints SDs only: no weekly P&L, mean of it, credit or delivery, and no Thursday buy-back (hold_*) leg,
    # whose quotes ~23 h before expiry nearly fix the week's P&L (8j registration).
    low = rep.lower()
    for word in ("hold_", "pnl", "p&l", "credit", "delivery", "premium", "| mean"):
        assert word not in low, word
    # The SD column is the SD of the weekly H1 series itself; its (signed) mean is not printed anywhere.
    capsys.readouterr()
    w = s0.pnl_weeks(s0.vom.load(src), "side", 0.015)
    v = [w[d] for d in sorted(w)]
    assert len(v) == 9 and len(set(v)) == 9
    assert _cells(rows[0])[2] == f"{100 * statistics.stdev(v):.2f}"
    assert f"{100 * statistics.mean(v):+.2f}" not in rep and f"{100 * statistics.mean(v):+.3f}" not in rep


def test_stage0_condor_forward_empty_cache_fails_without_report(tmp_path):
    # Known defect (BACKLOG 6.14): an empty or missing put_skew cache ends in a FileNotFoundError traceback, not a
    # one-line message, and an empty cache dir gets an empty snapshots_v2.json written into it by
    # `verify_put_skew.load_snapshots`. Pinned here so that a fix updates this test; what must hold either way: a
    # non-zero exit, nothing on stdout, the missing file named, no report written.
    script = Path(s0.__file__)
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "stage0.md"
    for src, missing in ((empty, "delivery_btc_usd.json"), (tmp_path / "no-such-cache", "snapshots_v2.json")):
        res = subprocess.run([sys.executable, str(script), "--src-dir", str(src), "--out", str(out)], capture_output=True, text=True)
        assert res.returncode == 1 and res.stdout == "", res.stderr
        assert f"FileNotFoundError: [Errno 2] No such file or directory: '{src / missing}'" in res.stderr
        assert "Traceback (most recent call last)" in res.stderr  # the defect; a fix flips this line
        assert not out.exists()
    assert sorted(p.name for p in empty.iterdir()) == ["snapshots_v2.json"]  # the side effect on the empty cache
    assert json.loads((empty / "snapshots_v2.json").read_text())["days"] == {}
