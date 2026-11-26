"""A delivered branch without a pull request is told to the owner: nothing else would merge it (25.11: one HTTP 500
from GitHub, the outcome counted as routine, and agent/2026-11-25-deribit-collector-gaps waited unseen for 15 hours)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
PR = "https://github.com/o/r/pull/7"
COMPARE = "https://github.com/o/r/compare/main...agent/2026-11-25-x?expand=1"


def _handover(tmp_path: Path, url: str) -> str:
    lines = NIGHTLY.splitlines()
    start = lines.index("handover() {")
    body = "\n".join(lines[start : lines.index("}", start) + 1])
    script = f"""set -euo pipefail
REPO={tmp_path} WORK={tmp_path}/work LOG_DIR={tmp_path} SUMMARY={tmp_path}/summary AUTO_DELIVERY=
git() {{ :; }}
python3() {{ echo auto; }}
head_utf8() {{ :; }}
open_pr() {{ echo '{url}'; }}
finish() {{ printf 'auto=%s\\n%s\\n' "$AUTO_DELIVERY" "$3"; }}
{body}
handover agent/2026-11-25-x delivered head
"""
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True, capture_output=True, text=True).stdout


def test_an_opened_pull_request_stays_a_routine_delivery(tmp_path):
    out = _handover(tmp_path, PR)
    assert out.startswith("auto=1\n") and "Зіллється сам" in out


def test_a_missing_pull_request_is_not_routine(tmp_path):
    out = _handover(tmp_path, COMPARE)
    assert out.startswith("auto=\n") and "Pull request не створився" in out and "Зіллється сам" not in out
