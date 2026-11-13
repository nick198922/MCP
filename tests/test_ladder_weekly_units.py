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
    """The service writes where checkpoint_report reads; the timer fires Sunday 12:00 Kyiv and catches up."""
    exec_start = re.findall(r"^ExecStart=(.+)$", unit, re.M)
    assert len(exec_start) == 1
    argv = shlex.split(exec_start[0])
    assert "scripts/paper_ladder_snapshot.py" in argv
    assert argv.count("--out-dir") == 1
    out_dir = argv[argv.index("--out-dir") + 1]
    assert out_dir == ladder_server  # one path in two places: unit --out-dir and checkpoint_report.LADDER_SERVER
    assert re.findall(r"^OnCalendar=(.+)$", timer, re.M) == ["Sun *-*-* 12:00:00 Europe/Kyiv"]
    assert re.findall(r"^Persistent=(.+)$", timer, re.M) == ["true"]


def test_ladder_weekly_units_match_checkpoint_report():
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    _assert_units(unit, timer, cr.LADDER_SERVER)


_OUT = "--out-dir /var/lib/tradedesk-mcp/ladder"
_EXEC = "ExecStart=/home/tradedesk/.local/bin/uv"
_CAL = "OnCalendar=Sun *-*-* 12:00:00 Europe/Kyiv"
_PERSIST = "Persistent=true\n"

# (target, old, new): each case changes exactly one thing the helper must catch.
_MUTATIONS = {
    "out_dir_path_changed": ("unit", _OUT, "--out-dir /var/lib/tradedesk-mcp/ladder2"),
    "out_dir_removed": ("unit", " " + _OUT, ""),
    "second_out_dir": ("unit", _OUT, _OUT + " --out-dir /tmp/ladder"),
    "script_renamed": ("unit", "python scripts/paper_ladder_snapshot.py", "python scripts/paper_ladder.py"),
    "exec_start_commented_out": ("unit", _EXEC, "#" + _EXEC),
    "second_exec_start": ("unit", "TimeoutStartSec=", "ExecStart=/bin/true\nTimeoutStartSec="),
    "on_calendar_saturday": ("timer", _CAL, _CAL.replace("Sun", "Sat")),
    "on_calendar_utc": ("timer", _CAL, _CAL.replace("Europe/Kyiv", "UTC")),
    "persistent_false": ("timer", _PERSIST, "Persistent=false\n"),
    "persistent_removed": ("timer", _PERSIST, ""),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_ladder_weekly_units_mutation_is_caught(name):
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    target, old, new = _MUTATIONS[name]
    text = unit if target == "unit" else timer
    assert text.count(old) == 1  # the mutation must hit the real file, not be a silent no-op
    mutated = text.replace(old, new)
    if target == "unit":
        unit = mutated
    else:
        timer = mutated
    with pytest.raises(AssertionError):
        _assert_units(unit, timer, cr.LADDER_SERVER)


def test_ladder_weekly_units_ladder_server_changed_is_caught():
    """A new LADDER_SERVER in checkpoint_report without the unit following it must fail."""
    unit = (UNITS / "tradedesk-ladder-weekly.service").read_text(encoding="utf-8")
    timer = (UNITS / "tradedesk-ladder-weekly.timer").read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_units(unit, timer, cr.LADDER_SERVER + "-v2")
