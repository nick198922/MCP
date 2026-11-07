"""Direct tests for the analytics helper behind the demo-trader version tags (card item 225, BACKLOG 6.9).

Recount on 07.11 (AST, public top-level functions of analytics/): every one of the 2173 names occurs in tests/ as a
word. Of the analytics functions that scripts/checkpoint_report.py, scripts/dashboard.py and scripts/demo_*.py import
(14) or reach through them (154 in the call graph), only `setup_versions.applies` had no direct test: its name occurs
in tests/ solely as the English word "applies" in comments. It decides which demo epochs (v1..v10) belong to a setup,
so it sets the version tag of every demo trade in the checkpoint report (`checkpoint._versioned`) and in the
per-version statistics (`setup_versions.version_of`, `version_stats`).

References: the rule from the `Epoch.setups` comment ("() = every setup") and the registry as described in the
change notes (v1..v3 are global; v4 / v7 / v9 S12, v5 S38 + S39, v6 S40, v8 S28, v10 S17), epoch starts recomputed
here from the UTC dates and times in those notes; no output of the code under test is reused as an expected value.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradedesk.analytics.checkpoint import DEMO, _versioned, demo_forward
from tradedesk.analytics.setup_versions import EPOCHS, Epoch, applies, version_of, version_stats

H = 3_600_000  # one hour, ms


def ms(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()) * 1000


V4 = ms(2026, 10, 25, 14, 30)  # S12 on the demo (docs/changes/2026-10-25-demo-s12-fvg.md)
V7 = ms(2026, 10, 27, 18, 0)  # S12 resting limit
V8 = ms(2026, 10, 29, 19, 50)  # S28 on the demo
V9 = ms(2026, 10, 31, 9, 30)  # S12 back to the touch-close entry; v10 (S17) starts the same minute
NOW = V9 + 24 * H
BY_TAG = {e.tag: e for e in EPOCHS}


def test_global_epoch_applies_to_every_setup():
    # setups=() means every setup, including a trade with no setup recorded
    e = Epoch("g", 0, "", "")
    for setup in ("S12", "S1", "S40", "", None, "anything"):
        assert applies(e, setup) is True


def test_restricted_epoch_is_an_exact_member_match():
    e = Epoch("r", 0, "", "", ("S12",))
    assert applies(e, "S12") is True
    # no setup, other setups, a prefix or a longer name, case and padding: all outside
    for setup in (None, "", "S1", "S120", "s12", " S12", "S12 "):
        assert applies(e, setup) is False


def test_multi_setup_epoch():
    e = Epoch("m", 0, "", "", ("S38", "S39"))
    assert applies(e, "S38") is True
    assert applies(e, "S39") is True
    assert applies(e, "S40") is False
    assert applies(e, "S38,S39") is False  # a joined string is not a member


@pytest.mark.parametrize(
    ("setup", "tags"),
    [
        ("S12", {"v1", "v2", "v3", "v4", "v7", "v9"}),
        ("S38", {"v1", "v2", "v3", "v5"}),
        ("S39", {"v1", "v2", "v3", "v5"}),
        ("S40", {"v1", "v2", "v3", "v6"}),
        ("S28", {"v1", "v2", "v3", "v8"}),
        ("S17", {"v1", "v2", "v3", "v10"}),
        ("S1", {"v1", "v2", "v3"}),
        (None, {"v1", "v2", "v3"}),
    ],
)
def test_real_registry_epochs_per_setup(setup, tags):
    assert {e.tag for e in EPOCHS if applies(e, setup)} == tags


def test_registry_starts_match_the_change_notes():
    # guards the fixed times the next tests rely on
    assert BY_TAG["v4"].from_ms == V4
    assert BY_TAG["v7"].from_ms == V7
    assert BY_TAG["v8"].from_ms == V8
    assert BY_TAG["v9"].from_ms == V9 == BY_TAG["v10"].from_ms


@pytest.mark.parametrize(
    ("setup", "tag"),
    [("S12", "v9"), ("S17", "v10"), ("S28", "v8"), ("S40", "v6"), ("S38", "v5"), ("S1", "v3"), (None, "v3")],
)
def test_version_of_picks_only_epochs_of_the_setup(setup, tag):
    # an hour after v9 / v10: each setup gets its own latest epoch, never another setup's
    e = version_of(setup, V9 + H, now_ms=NOW)
    assert e is not None and e.tag == tag


def test_version_stats_rejects_a_tag_of_another_setup():
    base = {"status": "closed", "r_multiple": 1.0, "pnl_usd": 5.0}
    rows = [
        # S28 tagged with S12's v7: the tag does not apply, the open time (after v8) decides
        dict(base, setup="S28", version="v7", opened_ts=V8 + H, closed_ts=V8 + 2 * H),
        # S12 tagged v7 and opened after v9: its own tag applies and is kept
        dict(base, setup="S12", version="v7", opened_ts=V9 + H, closed_ts=V9 + 2 * H),
    ]
    got = {(r["setup"], r["version"]) for r in version_stats(rows, now_ms=NOW)["rows"]}
    assert got == {("S28", "v8"), ("S12", "v7")}


def test_changed_inside_counts_only_later_epochs_of_the_setup():
    base = {"status": "closed", "r_multiple": 1.0, "pnl_usd": 5.0}
    rows = [
        # S28 opened under v8 and closed after v9 / v10: those are S12 / S17 epochs, not a change for S28
        dict(base, setup="S28", opened_ts=V8 + H, closed_ts=V9 + 2 * H),
        # S1 opened under v3 and closed after v4: v4 is S12 only, so no change either
        dict(base, setup="S1", opened_ts=V4 - H, closed_ts=V4 + H),
        # S12 opened under v4 and closed after v7: its own rules changed while it was open
        dict(base, setup="S12", opened_ts=V4 + H, closed_ts=V7 + H),
    ]
    inside = {(r["setup"], r["version"]): r["changed_inside"] for r in version_stats(rows, now_ms=NOW)["rows"]}
    assert inside == {("S28", "v8"): 0, ("S1", "v3"): 0, ("S12", "v4"): 1}


@pytest.mark.parametrize("setup", ["S12", " S12 ", "S12\n"])
@pytest.mark.parametrize("version", [None, "v9", "v7"])
def test_checkpoint_tag_matches_version_stats(setup, version):
    # card item 228: checkpoint._versioned must strip the setup name as version_stats does, so one trade gets one
    # version tag in both; a padded " S12 " opened after v9 is S12 under v9 (or under its own valid S12 tag)
    row = {"status": "closed", "setup": setup, "version": version, "opened_ts": V9 + H, "closed_ts": V9 + 2 * H}
    row["r_multiple"], row["pnl_usd"] = 1.0, 5.0
    (stats_row,) = version_stats([row], now_ms=NOW)["rows"]
    assert stats_row["setup"] == "S12"
    assert _versioned(row, NOW) == stats_row["version"] == (version or "v9")


@pytest.mark.parametrize(("setup", "tag"), [("S28", "v8"), ("S1", "v3"), (None, "v3"), ("", "v3")])
def test_checkpoint_tag_unchanged_for_clean_names(setup, tag):
    # clean names, no setup and an empty one keep their tags (an hour after v9, no own version column)
    assert _versioned({"setup": setup, "opened_ts": V9 + H}, NOW) == tag


def test_demo_forward_counts_a_padded_setup_name():
    # card item 229: demo_forward picks the trades of its setup by the stripped name, as _versioned and version_stats
    # do; a padded " S12 " closed under v9 is an S12 trade, while S1 / S120 / None stay outside
    (spec,) = (s for s in DEMO if s.setup == "S12")
    base = {"status": "closed", "opened_ts": V9 + H, "closed_ts": V9 + 2 * H}
    names = ["S12", " S12 ", "S12\n", "S1", "S120", None]
    trades = [dict(base, setup=name, link_id=str(i), r_multiple=0.5 + i) for i, name in enumerate(names)]
    row = demo_forward(spec, trades, NOW)[0]
    assert row["key"] == "demo:S12"
    assert row["n"] == 3
    assert row["metric"] == pytest.approx((0.5 + 1.5 + 2.5) / 3)
