"""Unit files of the weekly ladder snapshot timer (deploy/user-units/tradedesk-ladder-weekly.*); no network."""

import re
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import checkpoint_report as cr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "user-units"


def _assert_units(unit: str, timer: str, ladder_server: str) -> None:
    """The service writes where checkpoint_report reads; the timer fires Sunday 12:00 Kyiv and catches up.

    Each assert message starts with its own tag (``exec_start:``, ``script:``, ...), so the negative tests
    below can pin which check a mutation trips, not just that some check did.
    """
    exec_start = re.findall(r"^ExecStart=(.+)$", unit, re.M)
    assert len(exec_start) == 1, f"exec_start: expected one ExecStart line, got {len(exec_start)}"
    argv = shlex.split(exec_start[0])
    assert "scripts/paper_ladder_snapshot.py" in argv, f"script: snapshot script missing from {argv}"
    assert argv.count("--out-dir") == 1, f"out_dir_count: expected one --out-dir, got {argv.count('--out-dir')}"
    out_dir = argv[argv.index("--out-dir") + 1]
    # one path in two places: unit --out-dir and checkpoint_report.LADDER_SERVER
    assert out_dir == ladder_server, f"out_dir_path: {out_dir!r} != LADDER_SERVER {ladder_server!r}"
    on_calendar = re.findall(r"^OnCalendar=(.+)$", timer, re.M)
    assert on_calendar == ["Sun *-*-* 12:00:00 Europe/Kyiv"], f"on_calendar: {on_calendar}"
    persistent = re.findall(r"^Persistent=(.+)$", timer, re.M)
    assert persistent == ["true"], f"persistent: {persistent}"


def test_ladder_weekly_units_match_checkpoint_report():
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    _assert_units(unit, timer, cr.LADDER_SERVER)


_OUT = "--out-dir /var/lib/tradedesk-mcp/ladder"
_EXEC = "ExecStart=/home/tradedesk/.local/bin/uv"
_CAL = "OnCalendar=Sun *-*-* 12:00:00 Europe/Kyiv"
_PERSIST = "Persistent=true\n"

# (target, old, new, tag): each case changes exactly one thing, and the check tagged `tag` must be the one
# that fails. Pinning the tag keeps a reordered or weakened check from being masked by another one.
_MUTATIONS = {
    "out_dir_path_changed": ("unit", _OUT, "--out-dir /var/lib/tradedesk-mcp/ladder2", "out_dir_path"),
    "out_dir_removed": ("unit", " " + _OUT, "", "out_dir_count"),
    "second_out_dir": ("unit", _OUT, _OUT + " --out-dir /tmp/ladder", "out_dir_count"),
    "script_renamed": (
        "unit",
        "python scripts/paper_ladder_snapshot.py",
        "python scripts/paper_ladder.py",
        "script",
    ),
    "exec_start_commented_out": ("unit", _EXEC, "#" + _EXEC, "exec_start"),
    "second_exec_start": ("unit", "TimeoutStartSec=", "ExecStart=/bin/true\nTimeoutStartSec=", "exec_start"),
    "on_calendar_saturday": ("timer", _CAL, _CAL.replace("Sun", "Sat"), "on_calendar"),
    "on_calendar_utc": ("timer", _CAL, _CAL.replace("Europe/Kyiv", "UTC"), "on_calendar"),
    "persistent_false": ("timer", _PERSIST, "Persistent=false\n", "persistent"),
    "persistent_removed": ("timer", _PERSIST, "", "persistent"),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_ladder_weekly_units_mutation_is_caught(name):
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    target, old, new, tag = _MUTATIONS[name]
    text = unit if target == "unit" else timer
    assert text.count(old) == 1  # the mutation must hit the real file, not be a silent no-op
    mutated = text.replace(old, new)
    if target == "unit":
        unit = mutated
    else:
        timer = mutated
    with pytest.raises(AssertionError, match=rf"^{tag}: "):
        _assert_units(unit, timer, cr.LADDER_SERVER)


def test_ladder_weekly_units_ladder_server_changed_is_caught():
    """A new LADDER_SERVER in checkpoint_report without the unit following it must fail."""
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match=r"^out_dir_path: "):
        _assert_units(unit, timer, cr.LADDER_SERVER + "-v2")
