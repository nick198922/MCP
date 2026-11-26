"""A delivery that holds only the run's notes is an idle run, not a pull request (19-24.11: 236 empty `idle-*` /
`p-idle-notes-*` pull requests, each counted "delivered", so the loop never slowed down or called the idle doctor)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
GUARD = 'if [[ $DRY -eq 0 && "$NEXT" != continue && "$NEXT" != park ]] && only_run_notes; then'
NOTES = ("docs/agents/runs/2026-11-24-p-idle-notes.md", "docs/changes/2026-11-24-p-idle-notes.md")


def _block(first: str, end: str) -> str:
    lines = NIGHTLY.splitlines()
    start = lines.index(first)
    stop = next(i for i in range(start + 1, len(lines)) if lines[i] == end)
    return "\n".join(lines[start : stop + 1])


def _requested(tmp_path: Path, staged: tuple[str, ...], nxt: str = "owner", untracked: tuple[str, ...] = ()) -> bool:
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text(".agent/\n")
    git("add", ".gitignore")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    for name in (*staged, *untracked, ".agent/branch"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n")
    git("add", *staged)
    script = f"set -euo pipefail\nWT={tmp_path} DRY=0 NEXT='{nxt}'\n{_block('only_run_notes() {', '}')}\n{_block(GUARD, 'fi')}\n"
    subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True)
    return (tmp_path / ".agent/branch").exists()


def test_guard_runs_before_delivery():
    assert NIGHTLY.index(GUARD) < NIGHTLY.index('deliver "$SUMMARY"')


@pytest.mark.parametrize("nxt", ["owner", ""])
def test_notes_alone_are_not_delivered(tmp_path, nxt):
    assert not _requested(tmp_path, NOTES, nxt)


def test_a_throwaway_helper_does_not_make_notes_a_step(tmp_path):
    assert not _requested(tmp_path, NOTES, untracked=("scripts/_probe.py",))


@pytest.mark.parametrize("path", ["docs/ideas/IDEA-1.md", "docs/calls/REPORT.md", "docs/QUESTIONS.md"])
def test_a_step_with_a_result_is_delivered(tmp_path, path):
    assert _requested(tmp_path, (*NOTES, path))


@pytest.mark.parametrize("nxt", ["continue", "park"])
def test_unfinished_work_keeps_its_request(tmp_path, nxt):
    assert _requested(tmp_path, NOTES, nxt)
