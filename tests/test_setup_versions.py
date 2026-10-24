"""analytics/setup_versions.py: demo trades per (setup, version of the rules), no conclusion below 30 closed trades."""

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics import setup_versions as sv
from tradedesk.analytics.demo_vs_backtest import BE_SWITCH_MS, MIN_TRADES

LIVE = json.loads((Path(__file__).parent / "fixtures" / "demo_trades_live_2026-10-24.json").read_text())
NOW = int(LIVE["now_ms"])
H = 3_600_000


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def _t(setup, opened, r=1.0, *, status="closed", closed=None, pnl=None) -> dict:
    return {
        "setup": setup,
        "status": status,
        "opened_ts": opened,
        "closed_ts": (opened + H if opened is not None else NOW - H) if closed is None else closed,
        "r_multiple": r,
        "pnl_usd": 100.0 * r if pnl is None and r is not None else pnl,
    }


def _row(s: dict, setup, version) -> dict:
    (x,) = [x for x in s["rows"] if x["setup"] == setup and x["version"] == version]
    return x


def test_epochs_are_the_dated_rule_changes():
    # commit times of de3540f, 8ec1f37, 8d82847 (git log, +03:00); v3 is the break-even switch of the replay
    assert [(e.tag, e.from_ms) for e in sv.EPOCHS] == [
        ("v1", _ms(2026, 10, 21, 18, 39, 17)),
        ("v2", _ms(2026, 10, 22, 6, 13, 19)),
        ("v3", _ms(2026, 10, 22, 7, 12, 39)),
    ]
    assert sv.EPOCHS[-1].from_ms == BE_SWITCH_MS
    assert all(e.setups == () and e.source and e.what for e in sv.EPOCHS)
    assert [e.from_ms for e in sv.EPOCHS] == sorted(e.from_ms for e in sv.EPOCHS)
    first_open = min(t["opened_ts"] for t in LIVE["trades"])
    assert sv.EPOCHS[0].from_ms <= first_open < sv.EPOCHS[1].from_ms  # the first live trade (21.10 20:02 UTC) is v1


def test_live_sample_per_setup_and_version():
    s = sv.version_stats(LIVE["trades"], now_ms=NOW)
    assert [(x["setup"], x["version"]) for x in s["rows"]] == [("S16", "v3"), ("S3", "v1"), ("S3", "v3")]
    v1 = _row(s, "S3", "v1")  # LINK, LTC, ONDO, MNT: opened before the maker orders, all at the target
    rs1 = [2.2650609228611103, 2.225265890241667, 2.103368462188928, 2.6880272035984536]
    assert (v1["closed"], v1["n_r"], v1["wins"], v1["win_share"]) == (4, 4, 4, 1.0)
    assert v1["mean_r"] == pytest.approx(statistics.fmean(rs1), abs=1e-9)
    assert v1["pnl_usd"] == pytest.approx(226.46538255 + 222.38448844 + 210.32994696 + 268.80105215, abs=1e-6)
    assert v1["changed_inside"] == 4  # all four were still open when v2 / v3 began
    v3 = _row(s, "S3", "v3")
    assert (v3["closed"], v3["n_r"], v3["wins"], v3["changed_inside"]) == (8, 8, 2, 0)  # ATOM (break-even, +0.0005 R) and DOT
    assert v3["win_share"] == pytest.approx(0.25)
    s16 = _row(s, "S16", "v3")
    assert (s16["closed"], s16["mean_r"]) == (1, pytest.approx(-1.2284071657526532))
    for x in s["rows"]:
        assert not x["enough"] and x["verdict"] == f"замало для висновку ({x['n_r']} з {MIN_TRADES})"
    assert s["unversioned"] == 0 and s["min_n"] == MIN_TRADES == 30
    assert [e["tag"] for e in s["epochs"]] == ["v1", "v2", "v3"]
    assert sum(x["closed"] for x in s["rows"]) == sum(1 for t in LIVE["trades"] if t["status"] == "closed") == 13


def test_boundary_at_the_exact_epoch_minute():
    v2, v3 = sv.EPOCHS[1].from_ms, sv.EPOCHS[2].from_ms
    assert sv.version_of("S3", v3, now_ms=NOW).tag == "v3"  # opened at the switch: the new rules
    assert sv.version_of("S3", v3 - 1, now_ms=NOW).tag == "v2"
    assert sv.version_of("S3", v2, now_ms=NOW).tag == "v2"
    assert sv.version_of("S3", sv.EPOCHS[0].from_ms - 1, now_ms=NOW) is None  # before the first version
    assert sv.version_of("S3", None, now_ms=NOW) is None
    s = sv.version_stats([_t("S3", v3), _t("S3", v3 - 1, closed=v3 + H)], now_ms=NOW)
    assert _row(s, "S3", "v3")["closed"] == 1 and _row(s, "S3", "v2")["changed_inside"] == 1
    assert _row(s, "S3", "v3")["changed_inside"] == 0
    # a trade closed exactly at the next epoch is not counted as changed inside
    s = sv.version_stats([_t("S3", v3 - 60_000, closed=v3)], now_ms=NOW)
    assert _row(s, "S3", "v2")["changed_inside"] == 0


def test_trade_without_setup_or_open_time():
    v3 = sv.EPOCHS[2].from_ms
    trades = [_t(None, v3 + H), _t("", v3 + H), _t("  ", v3 + H, -1.0), _t("S3", None), _t("S3", sv.EPOCHS[0].from_ms - H)]
    s = sv.version_stats(trades, now_ms=NOW)
    assert [(x["setup"], x["version"]) for x in s["rows"]] == [("S3", None), (None, "v3")]  # no setup sorts last
    none = _row(s, None, "v3")
    assert (none["closed"], none["wins"], none["mean_r"]) == (3, 2, pytest.approx(1 / 3))
    assert _row(s, "S3", None)["closed"] == 2 and _row(s, "S3", None)["from_ms"] is None
    assert s["unversioned"] == 2


def test_small_sample_flag_counts_closed_trades_with_r():
    v3 = sv.EPOCHS[2].from_ms
    full = [_t("S14", v3 + i * H, 1.0 if i % 2 else -0.5) for i in range(30)]
    s = _row(sv.version_stats(full, now_ms=NOW + 100 * H), "S14", "v3")
    assert s["enough"] and s["verdict"] == "достатньо угод для висновку" and s["n_r"] == 30
    assert s["mean_r"] == pytest.approx(0.25) and s["win_share"] == pytest.approx(0.5)
    short = [*full[:29], _t("S14", v3 + 40 * H, None, pnl=12.0)]  # 30 closed, one without R
    s = _row(sv.version_stats(short, now_ms=NOW + 100 * H), "S14", "v3")
    assert (s["closed"], s["n_r"], s["enough"]) == (30, 29, False)
    assert s["verdict"] == "замало для висновку (29 з 30)"
    assert s["pnl_usd"] == pytest.approx(sum(100.0 * t["r_multiple"] for t in full[:29]) + 12.0)


def test_empty_open_only_and_odd_values():
    s = sv.version_stats([], now_ms=NOW)
    assert s["rows"] == [] and s["unversioned"] == 0 and len(s["epochs"]) == 3
    assert sv.version_stats([_t("S3", NOW - H, status="open")], now_ms=NOW)["rows"] == []
    odd = [{**_t("S3", NOW - 2 * H), "r_multiple": "x", "pnl_usd": float("nan")}, {**_t("S3", NOW - 2 * H), "opened_ts": True}]
    s = sv.version_stats(odd, now_ms=NOW)
    assert _row(s, "S3", "v3")["n_r"] == 0 and _row(s, "S3", "v3")["pnl_usd"] == 0.0 and _row(s, "S3", "v3")["mean_r"] is None
    assert _row(s, "S3", None)["closed"] == 1  # a bool is not a timestamp


def test_time_is_a_parameter_and_setup_specific_epochs():
    v3 = sv.EPOCHS[2].from_ms
    later = sv.Epoch("v4", v3 + 10 * H, "S14 only", "test", ("S14",))
    eps = (*sv.EPOCHS, later)
    trades = [_t("S14", v3 + 11 * H), _t("S3", v3 + 11 * H), _t("S3", v3 + H, closed=v3 + 20 * H)]
    s = sv.version_stats(trades, now_ms=v3 + 30 * H, epochs=eps)
    assert [(x["setup"], x["version"]) for x in s["rows"]] == [("S14", "v4"), ("S3", "v3")]
    assert _row(s, "S3", "v3")["changed_inside"] == 0  # v4 does not apply to S3
    # before v4 starts it does not exist; a trade closed after now is not counted yet
    s = sv.version_stats(trades, now_ms=v3 + 5 * H, epochs=eps)
    assert s["rows"] == [] and [e["tag"] for e in s["epochs"]] == ["v1", "v2", "v3"]
