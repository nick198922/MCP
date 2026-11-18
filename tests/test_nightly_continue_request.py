"""A CONTINUE run that wrote .agent/next but no .agent/branch still requests delivery on its work branch (18.11: run 5
of btc_charlie wrote `owner`, staged its summary, skipped .agent/branch, and approved work was handed over as
"stuck"), and the CONTINUE prompt names the run limit."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
GUARD = 'if [[ $MODE == continue && -n "$WORK_BR" && -n "$NEXT" && ! -f .agent/branch ]]; then'


def _guard_block() -> str:
    lines = NIGHTLY.splitlines()
    start = lines.index(GUARD)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start : end + 1])


def _run(tmp_path: Path, mode: str, work: str, nxt: str) -> subprocess.CompletedProcess:
    (tmp_path / ".agent").mkdir(exist_ok=True)
    script = f"set -euo pipefail\nMODE={mode} WORK_BR={work} NEXT={nxt} RUNS=5 MAX_RUNS=5\n{_guard_block()}\n"
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True)


def test_guard_runs_before_delivery():
    assert NIGHTLY.index(GUARD) < NIGHTLY.index('deliver "$SUMMARY"')


def test_next_without_branch_requests_the_work_branch(tmp_path):
    _run(tmp_path, "continue", "agent/2026-11-18-x", "owner")
    assert (tmp_path / ".agent/branch").read_text().strip() == "agent/2026-11-18-x"
    msg = (tmp_path / ".agent/commit-msg.txt").read_text()
    assert msg.splitlines()[0] == "Agents: agent/2026-11-18-x"
    assert "Co-Authored-By:" in msg


def test_own_commit_message_is_kept(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent/commit-msg.txt").write_text("Own subject\n")
    _run(tmp_path, "continue", "agent/2026-11-18-x", "continue")
    assert (tmp_path / ".agent/commit-msg.txt").read_text() == "Own subject\n"


def test_no_request_without_next_or_work_branch(tmp_path):
    _run(tmp_path, "continue", "agent/2026-11-18-x", "''")
    _run(tmp_path, "new", "''", "owner")
    assert not (tmp_path / ".agent/branch").exists()


def test_continue_prompt_names_the_limit():
    assert "$limit$refused" in NIGHTLY
    assert "This is the last run on this work: never \\`continue\\`" in NIGHTLY


def test_script_parses():
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "nightly_agents.sh")], check=True)
