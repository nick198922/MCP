"""The owner-only note reaches the coordinator, and an idle owner-only run does not repeat every half hour (28.11: the
launcher looked for "BUDGET:" in the prompt before adding the note, .claude/nightly.md already holds that word, so below
80 % the standing card spent about 18 points of the week while the loop logged "taking only those")."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
LOOP = (ROOT / "scripts" / "agents_loop.sh").read_text(encoding="utf-8")
SKIP = "skipping cards with the line «Темп: за графіком»"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _between(text: str, first: str, last: str) -> str:
    """Lines from the one starting with `first` to the next line equal to `last`."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    end = next(i for i in range(start, len(lines)) if lines[i] == last)
    return "\n".join(lines[start : end + 1])


def _bash(tmp_path: Path, script: str) -> str:
    res = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=60, encoding="utf-8")
    return res.stdout


def _prompt(tmp_path: Path, week: int, owner_only: str = "", boost: bool = False) -> str:
    if boost:
        (tmp_path / "boost-until").write_text("9999999999\n")
    section = NIGHTLY[NIGHTLY.index("OWNER_ONLY_AT=") : NIGHTLY.index("# The owner's cards with open items")]
    script = f"""
set -euo pipefail
usage_now() {{ echo "10 {week}"; }}
LOG_DIR=. TRADEDESK_AGENTS_OWNER_ONLY='{owner_only}'
PROMPT="$(cat '{ROOT / ".claude" / "nightly.md"}')"
{section}
printf '%s' "$PROMPT"
"""
    return _bash(tmp_path, script)


def test_ahead_of_schedule_below_80_percent_gets_the_note(tmp_path):
    prompt = _prompt(tmp_path, 56, owner_only="ahead of the weekly schedule (56.0% 30%)")
    assert prompt.count(SKIP) == 1 and "ahead of the weekly schedule (56.0% 30%)" in prompt


def test_past_80_percent_gets_one_note(tmp_path):
    prompt = _prompt(tmp_path, 83, owner_only="ahead of the weekly schedule (83.0% 46%)")
    assert prompt.count(SKIP) == 1 and "weekly plan usage is 83%" in prompt


@pytest.mark.parametrize(("week", "boost"), [(40, False), (90, True)])
def test_no_note_on_schedule_or_under_a_boost(tmp_path, week, boost):
    assert SKIP not in _prompt(tmp_path, week, boost=boost)


def _idle_sleep(tmp_path: Path, week: int, owner_only: str = "", boost_after: int = 10**6) -> int:
    """Seconds the loop sleeps after a run that found nothing (exit code 3)."""
    owner_only_mode = _between(LOOP, "owner_only_mode() {", "}")
    idle_wait = _between(LOOP, "work_tree() {", "}")
    waits = "\n".join(line for line in LOOP.splitlines() if line.startswith(("WAIT_IDLE=", "WAIT_OWNER_IDLE=")))
    case = _between(LOOP, "    3)", "      ;;").removeprefix("    3)\n").removesuffix("      ;;")
    script = f"""
set -uo pipefail
log() {{ :; }}
loop_state() {{ :; }}
at() {{ :; }}
git() {{ :; }}
idle_check() {{ :; }}
open_branches() {{ :; }}
boost_on() {{ [[ -f boost ]]; }}
slept=0
sleep() {{ slept=$((slept + $1)); if ((slept >= {boost_after})); then : >boost; fi; }}
{waits}
{owner_only_mode}
{idle_wait}
REPO=. OWNER_ONLY_AT=80 week={week} TRADEDESK_AGENTS_OWNER_ONLY='{owner_only}'
{case}
echo "$slept"
"""
    return int(_bash(tmp_path, script).split()[-1])


def test_a_normal_idle_run_waits_half_an_hour(tmp_path):
    assert _idle_sleep(tmp_path, week=40) == 1800


@pytest.mark.parametrize(("week", "owner_only"), [(56, "ahead of the weekly schedule"), (83, "")])
def test_an_owner_only_idle_run_waits_three_hours(tmp_path, week, owner_only):
    assert _idle_sleep(tmp_path, week, owner_only) == 10800


def test_the_boost_ends_the_long_wait(tmp_path):
    assert _idle_sleep(tmp_path, week=83, boost_after=600) == 600
