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


def _slept(tmp_path: Path, merge_after: int, rounds: int = 1) -> int:
    """Seconds the review wait sleeps when one of two open branches is merged `merge_after` seconds into it."""
    functions = _between("work_tree() {", "}")  # the one-line work_tree plus idle_wait, up to its closing brace
    block = _between("  if ((n_open >= MAX_OPEN && leftovers == 0)); then", "  fi")
    script = f"""
set -uo pipefail
log() {{ :; }}
loop_state() {{ :; }}
announce() {{ echo told >>announced; }}
ensure_prs() {{ echo "$1" >>ensured; }}
at() {{ :; }}
git() {{ :; }}
open_branches() {{ if [[ -f merged ]]; then echo agent/a; else printf 'agent/a\\nagent/b\\n'; fi; }}
slept=0
sleep() {{ slept=$((slept + $1)); if ((slept >= {merge_after})); then : >merged; fi; }}
{functions}
REPO=. MAX_OPEN=2 WAIT_REVIEW=1800 leftovers=0 n_open=2 open="$(open_branches)" rounds=0 WAIT_SEEN="${{WAIT_SEEN:-}}"
while ((rounds < ${{ROUNDS:-1}})); do
  rounds=$((rounds + 1))
{block}
done
echo "$slept"
"""
    res = subprocess.run(
        ["bash", "-c", f"ROUNDS={rounds}\n" + script], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=60
    )
    return int(res.stdout.split()[-1])


def test_the_wait_ends_when_a_branch_is_merged(tmp_path):
    assert _slept(tmp_path, merge_after=60) <= 120


def test_the_wait_lasts_while_nothing_is_merged(tmp_path):
    assert _slept(tmp_path, merge_after=10**6) == 1800


def test_a_fresh_wait_is_not_announced_a_lasting_one_is(tmp_path):
    """A PR that merges after CI is routine: the owner hears only of branches still waiting a round later (27.11)."""
    _slept(tmp_path, merge_after=10**6, rounds=1)
    assert not (tmp_path / "announced").exists()
    _slept(tmp_path, merge_after=10**6, rounds=2)
    assert (tmp_path / "announced").read_text().count("told") == 1


def test_every_waiting_branch_gets_its_pull_request_ensured(tmp_path):
    """A branch whose PR was never opened (GitHub HTTP 500, 25.11) must not hold a slot for good."""
    _slept(tmp_path, merge_after=10**6)
    assert (tmp_path / "ensured").read_text().split() == ["agent/a", "agent/b"]
