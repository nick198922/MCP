"""Unit files of the weekly ladder snapshot timer (deploy/user-units/tradedesk-ladder-weekly.*); no network."""

import re
import shlex
import sys
from pathlib import Path

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
