"""The wait for unmerged agent branches ends once one of them is merged (26-27.11: five times a delivered PR merged
within seconds of the check and the loop still slept the full half hour). The limit itself stays: with nothing merged
the whole wait passes."""

import shutil
import subprocess
from pathlib import Path

import pytest

LOOP = (Path(__file__).resolve().parent.parent / "scripts" / "agents_loop.sh").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _between(first: str, last: str) -> str:
    """Lines of the loop from the one starting with `first` to the next line equal to `last`."""
    lines = LOOP.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    end = next(i for i in range(start, len(lines)) if lines[i] == last)
    return "\n".join(lines[start : end + 1])


def _slept(tmp_path: Path, merge_after: int) -> int:
    """Seconds the review wait sleeps when one of two open branches is merged `merge_after` seconds into it."""
    functions = _between("work_tree() {", "}")  # the one-line work_tree plus idle_wait, up to its closing brace
    block = _between("  if ((n_open >= MAX_OPEN && leftovers == 0)); then", "  fi")
    script = f"""
set -uo pipefail
log() {{ :; }}
loop_state() {{ :; }}
announce() {{ :; }}
at() {{ :; }}
git() {{ :; }}
open_branches() {{ if [[ -f merged ]]; then echo agent/a; else printf 'agent/a\\nagent/b\\n'; fi; }}
slept=0
sleep() {{ slept=$((slept + $1)); if ((slept >= {merge_after})); then : >merged; fi; }}
{functions}
REPO=. MAX_OPEN=2 WAIT_REVIEW=1800 leftovers=0 n_open=2 open="$(open_branches)" rounds=0
while ((rounds < 1)); do
  rounds=1
{block}
done
echo "$slept"
"""
    res = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=60)
    return int(res.stdout.split()[-1])


def test_the_wait_ends_when_a_branch_is_merged(tmp_path):
    assert _slept(tmp_path, merge_after=60) <= 120


def test_the_wait_lasts_while_nothing_is_merged(tmp_path):
    assert _slept(tmp_path, merge_after=10**6) == 1800
