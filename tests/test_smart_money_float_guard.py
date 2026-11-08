"""Corrupt live fields in the smart-money code (item 276 of IDEA-20261102-standing-improvements, BACKLOG 6.53):
Hyperliquid ``portfolio`` points and ``userFillsByTime`` fills with a 10**400 / NaN / ±inf / None / text field never
raise (``int(inf)`` and ``float(10**400)`` raised OverflowError before), the point is skipped and named (trader, field)
in the warnings, and normal input gives the same numbers as the previous parser. No network."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tradedesk.analytics.smart_money import dropped_note, finite_num, parse_portfolio, realized_pnl_windows

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_smart_money as vsm  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
BAD = [10**400, -(10**400), float("nan"), float("inf"), float("-inf"), None, "x", "inf", "NaN", [1], {"a": 1}]


def _old_parse_portfolio(raw: Any) -> dict[str, dict[str, Any]]:
    """The parser before item 276, for the identity check on normal input."""
    out: dict[str, dict[str, Any]] = {}
    for name, body in raw:
        rows = {}
        for key in ("pnlHistory", "accountValueHistory"):
            rows[key] = sorted((int(p[0]), float(p[1])) for p in body.get(key) or [])
        out[str(name)] = {"pnl": rows["pnlHistory"], "value": rows["accountValueHistory"], "vlm": float(body.get("vlm") or 0.0)}
    return out


def _all_finite(p: dict[str, dict[str, Any]]) -> bool:
    return all(math.isfinite(w["vlm"]) and all(math.isfinite(v) and abs(t) < 2**63 for t, v in w["pnl"] + w["value"]) for w in p.values())


def test_finite_num() -> None:
    assert finite_num("1.5") == 1.5 and finite_num(3) == 3.0 and finite_num(0) == 0.0
    for x in BAD:
        assert finite_num(x) is None


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize("slot", ["time", "value"])
def test_portfolio_corrupt_point_skipped_and_named(bad: Any, slot: str) -> None:
    point = [bad, "2.5"] if slot == "time" else [1_700_000_000_500, bad]
    raw = [["month", {"pnlHistory": [[1_700_000_000_000, "1.0"], point], "accountValueHistory": [[1_700_000_000_000, "9"]], "vlm": "7"}]]
    dropped: list[str] = []
    p = parse_portfolio(raw, dropped)
    assert p["month"]["pnl"] == [(1_700_000_000_000, 1.0)]
    assert p["month"]["value"] == [(1_700_000_000_000, 9.0)]
    assert dropped == [f"month.pnlHistory.{slot}"]
    assert _all_finite(p)


@pytest.mark.parametrize("bad", [b for b in BAD if b is not None], ids=repr)
def test_portfolio_corrupt_vlm_reads_zero_and_named(bad: Any) -> None:
    dropped: list[str] = []
    p = parse_portfolio([["week", {"pnlHistory": [], "accountValueHistory": [], "vlm": bad}]], dropped)
    assert p["week"]["vlm"] == 0.0 and dropped == ["week.vlm"]


def test_portfolio_malformed_points_named() -> None:
    dropped: list[str] = []
    raw = [["allTime", {"pnlHistory": [{"0": 1}, [5], "12", None, [3, "4"]], "accountValueHistory": None, "vlm": None}]]
    p = parse_portfolio(raw, dropped)  # a dict point raised KeyError before
    assert p["allTime"] == {"pnl": [(3, 4.0)], "value": [], "vlm": 0.0}
    assert dropped == ["allTime.pnlHistory"] * 4  # absent vlm reads 0 silently, as before
    assert parse_portfolio(raw) == p  # without a collector


def test_portfolio_live_sample_unchanged() -> None:
    raw = json.loads((FIX / "hyperliquid_portfolio_2026-10-17.json").read_text())
    dropped: list[str] = []
    assert parse_portfolio(raw, dropped) == _old_parse_portfolio(raw)
    assert dropped == []


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize("field", ["time", "closedPnl", "fee"])
def test_fills_corrupt_field_skipped_and_named(bad: Any, field: str) -> None:
    good = [{"time": 5, "closedPnl": "10", "fee": "1"}, {"time": 15, "closedPnl": "-4", "fee": "0.5"}]
    corrupt = {"time": 6, "closedPnl": "100", "fee": "0", field: bad}
    dropped: list[str] = []
    wins = realized_pnl_windows(good + [corrupt], [0, 10, 20], 0, dropped=dropped)
    if bad is None and field != "time":  # an absent amount counts as 0, as before
        assert dropped == [] and wins[0] == pytest.approx(9.0 + (0 if field == "closedPnl" else 100))
        return
    assert wins == [pytest.approx(9.0), pytest.approx(-4.5)]
    assert dropped == [field]


def test_fills_not_a_mapping_named_and_normal_unchanged() -> None:
    fills = [{"time": 5, "closedPnl": "10", "fee": "1"}, {"time": "15", "closedPnl": -4, "fee": 0.5}, {"time": 25}]
    old = [0.0, 0.0, 0.0]
    for f in fills:  # the formula before item 276
        t, v = int(f["time"]), float(f.get("closedPnl") or 0.0) - float(f.get("fee") or 0.0)
        old[t // 10] += v
    dropped: list[str] = []
    assert realized_pnl_windows(fills, [0, 10, 20, 30], 0, dropped=dropped) == old
    assert dropped == []
    assert realized_pnl_windows([*fills, "junk", None], [0, 10, 20, 30], 0, dropped=dropped) == old
    assert dropped == ["fill", "fill"]


def test_dropped_note() -> None:
    assert dropped_note("portfolio 0xabc", []) is None
    note = dropped_note("portfolio 0xabc", ["month.vlm", "week.pnlHistory.time", "month.vlm"])
    assert note == "portfolio 0xabc: 3 point(s) with a field that is not a finite number dropped (month.vlm ×2, week.pnlHistory.time ×1)"


# ---------------------------------------------------------------------------------------------------------------------
# scripts/verify_smart_money.py


def test_retro_fills_names_trader_and_field(tmp_path: Path) -> None:
    (tmp_path / "fills").mkdir()
    a, b = "0xaaa", "0xbbb"
    fills = [
        {"time": vsm.E0 - 5 * vsm.DAY_MS, "closedPnl": "10", "fee": "1"},
        {"time": vsm.E0 - vsm.DAY_MS, "closedPnl": 10**400, "fee": "0"},
    ]
    (tmp_path / "fills" / f"{a}.json").write_text(json.dumps({"start": 0, "capped": False, "fills": fills}))
    capped = [{"time": "inf", "closedPnl": "1", "fee": "0"}]  # capped, and no fill with a finite time: no coverage end
    (tmp_path / "fills" / f"{b}.json").write_text(json.dumps({"start": 0, "capped": True, "fills": capped}))
    rows, stats = vsm.retro_fills({}, {a: "directional", b: "directional"}, tmp_path, [a, b])
    assert len(rows) == vsm.N_MONTHS
    assert stats["warnings"] == [
        "fills 0xaaa: 1 point(s) with a field that is not a finite number dropped (closedPnl ×1)",
        "fills 0xbbb: capped history without a fill of finite time, account skipped",
    ]


def test_last_fill_time() -> None:
    assert vsm.last_fill_time([{"time": 5}, {"time": 7}, {"time": float("inf")}, "x"]) == 7
    assert vsm.last_fill_time([{"time": None}]) is None and vsm.last_fill_time([]) is None


def test_classes_corrupt_position_dropped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "state").mkdir()
    st = {"assetPositions": [{"position": {"szi": 10**400, "positionValue": "5"}}, {"position": {"szi": "2", "positionValue": "nan"}}]}
    (tmp_path / "state" / "0xaaa.json").write_text(json.dumps(st))
    out = vsm.classes({"0xaaa": {"account_value": 1e6, "vlm_month": 1e6}}, tmp_path)
    assert out == {"0xaaa": "idle"}
    text = capsys.readouterr().out
    assert "state 0xaaa: position szi is not a finite number" in text and "positionValue is not a finite number" in text


async def test_fetch_corrupt_portfolio_and_fill_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE trader_snapshot (venue, day, account, name, account_value, pnl_day, pnl_week, pnl_month, pnl_all, roi_month, vlm_month)"
    )
    conn.execute("INSERT INTO trader_snapshot VALUES ('hyperliquid', ?, '0xaaa', 'n', 1e6, 0, 0, 5, 0, 0, 0)", (vsm.SNAP_DAY,))
    conn.execute("CREATE TABLE trader_position_state (venue, account)")
    conn.execute("INSERT INTO trader_position_state VALUES ('hyperliquid', '0xaaa')")
    portfolio = [["month", {"pnlHistory": [[1, "1"], [float("inf"), "2"], [3, 10**400]], "accountValueHistory": [], "vlm": "1"}]]
    page = [{"time": 1, "tid": 1, "closedPnl": "1", "fee": "0"}, {"time": "inf", "tid": 2, "closedPnl": "1", "fee": "0"}]

    async def info(payload: dict[str, Any], ttl: int = 0) -> SimpleNamespace:
        return SimpleNamespace(data={"portfolio": portfolio, "clearinghouseState": {}, "userFillsByTime": page}[payload["type"]])

    async def aclose() -> None:
        return None

    rt = SimpleNamespace(hyperliquid=SimpleNamespace(_info=info), aclose=aclose)
    monkeypatch.setattr("tradedesk.server.build_server", lambda: (None, rt))
    monkeypatch.setattr(vsm, "WEIGHT_PER_MIN", 1e9)
    monkeypatch.setattr(vsm, "FILLS_PAGE", 2)
    await vsm.fetch(conn, tmp_path, None)
    text = capsys.readouterr().out
    assert "warning: portfolio 0xaaa: 2 point(s) with a field that is not a finite number dropped" in text
    assert "month.pnlHistory.time ×1, month.pnlHistory.value ×1" in text
    assert json.loads((tmp_path / "portfolio" / "0xaaa.json").read_text())["month"]["pnl"] == [[1, 1.0]]
    assert "warning: fills 0xaaa: the last fill of a page has no finite time" in text
    assert not (tmp_path / "fills" / "0xaaa.json").exists()
