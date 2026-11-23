"""Work handed over "stuck" gets one last run once the agents are idle (18-23.11: btc_charlie's registration, approved
by the critic, waited unmarked for 5 days while 150+ runs idled because its branch held the x-traders card)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
BR = "agent/2026-11-18-x-btc-charlie-sample-registration"


def _block() -> str:
    lines = NIGHTLY.splitlines()
    start = lines.index("park_add() { # park_add <branch> <not-before epoch> <runs> <first>: park a branch, replacing its earlier line")
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("done < <(git branch -r --no-merged"))
    return "\n".join(lines[start : end + 1])


def _run(tmp_path: Path, history: list[tuple[str, str]], branch: str = BR) -> str:
    rows = [f"2026-11-{i:02d}_000000\t{status}\t{text}" for i, (status, text) in enumerate(history, 1)]
    (tmp_path / "history.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    parked = tmp_path / "parked"
    script = (
        "set -euo pipefail\n"
        f"git() {{ echo '  origin/{branch}'; }}\n"
        f"LOG_DIR={tmp_path} PARKED={parked} WORK_BR='' MAX_RUNS=5 STAMP=now\n"
        f"{_block()}\n"
    )
    subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True)
    return parked.read_text() if parked.exists() else ""


STUCK = ("stuck", f"⚠️ **Агенти не завершили крок за 5 прогонів**. Віддаю вам як є: `{BR}`")
IDLE = ("idle", "💤 **Агенти: роботи немає.**")


def test_stuck_work_is_revived_for_the_last_run_when_idle(tmp_path):
    parked = _run(tmp_path, [STUCK, IDLE, IDLE])
    assert parked.split()[0] == BR and parked.split()[2] == "4"  # resumes as run 5 of 5: never `continue`


def test_not_revived_while_the_agents_have_work(tmp_path):
    assert _run(tmp_path, [STUCK, ("delivered", "✅ `agent/2026-11-23-other`")]) == ""


def test_revived_only_once(tmp_path):
    assert _run(tmp_path, [STUCK, IDLE, STUCK, IDLE]) == ""


def test_the_doctor_fix_branches_stay_with_the_owner(tmp_path):
    fix = "agent/2026-10-15-fix-continue-checkout"
    assert _run(tmp_path, [("stuck", f"`{fix}`"), IDLE], branch=fix) == ""


def test_the_revived_run_is_told_to_deliver():
    assert "is revived because the agents were idle" in NIGHTLY
