"""scripts/docs_paths.py (item 130 of docs/ideas/IDEA-20261102-standing-improvements.md): repo paths in backticks."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docs_paths  # noqa: E402


def test_extract_paths_keeps_repo_paths_and_skips_templates_home_and_cache():
    text = (
        "Run `uv run python scripts/verify_x.py --stage 1 --out docs/verification/x.md` now.\n"
        "Not in code: scripts/plain.py and [link](../scripts/linked.py).\n"
        "`tests/test_a.py::test_b` `src/tradedesk/analytics/odds.py:12` `deploy/features.env` `tests/fixtures/a.json`\n"
        "Templates: `scripts/*.py` `docs/verification/YYYY-MM-DD-x.md` `docs/agents/runs/РРРР-ММ-ДД.md`\n"
        "`docs/verification/<date>-x.md` `scripts/verify_….py` `docs/verification/2026-11-0X-y.md` `tests/{a,b}.py`\n"
        "Home and cache: `~/.cache/tradedesk-agents/docs/x.json` `/home/tradedesk/tradedesk-agents/scripts/h.py`\n"
        "`~/private/docs/p.md` `../tests/rel.py` `docs/.cache/c.json` `scripts/__pycache__/m.py`\n"
        "Local-only: `docs/RESULTS.md` `docs/library/courses/l.md` `docs/library/img/a/b.md`\n"
        "No extension or other: `scripts/server/` `docs/x.txt` `scripts/a.pyc` `docs/a.json.gz` `notes/a.md`\n"
        "```bash\n"
        "uv run python scripts/fenced.py\n"
        "```\n"
        "Anchor: `docs/HANDOFF.md#стан` and data `data/x.csv`, `tests/fixtures/b.csv`.\n"
    )
    assert docs_paths.extract_paths(text) == [
        (1, "scripts/verify_x.py"),
        (1, "docs/verification/x.md"),
        (3, "tests/test_a.py"),
        (3, "src/tradedesk/analytics/odds.py"),
        (3, "deploy/features.env"),
        (3, "tests/fixtures/a.json"),
        (11, "scripts/fenced.py"),
        (13, "docs/HANDOFF.md"),
        (13, "tests/fixtures/b.csv"),
    ]


def test_card_status_selects_accepted_testing_forward():
    assert docs_paths.is_active_card("# T\n\nСтатус: forward (2026-10-29, ...)\n")
    assert docs_paths.is_active_card("# T\n\n**Статус:** testing\n")
    assert docs_paths.is_active_card("# T\n\nСтатус: accepted (рішення власника)\n")
    assert not docs_paths.is_active_card("# T\n\nСтатус: rejected (раніше testing)\n")
    assert not docs_paths.is_active_card("# T\n\nСтатус: inconclusive\n")
    assert not docs_paths.is_active_card("# T\n\nбез статусу\n")


def test_check_reports_missing_only():
    sources = {"docs/HANDOFF.md": "`scripts/ok.py` `scripts/gone.py`\n`docs/YYYY.md`\n"}
    missing, checked = docs_paths.check(sources, tracked={"scripts/ok.py"})
    assert checked == 2
    assert [(m.source, m.line, m.target) for m in missing] == [("docs/HANDOFF.md", 1, "scripts/gone.py")]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_check_tree_on_a_git_fixture(tmp_path, capsys):
    (tmp_path / "docs" / "ideas").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "present.py").write_text("")
    (tmp_path / "scripts" / "untracked.py").write_text("")  # exists on disk but not in git -> missing
    (tmp_path / "docs" / "HANDOFF.md").write_text(
        "Продовжити з: `uv run python scripts/present.py --stage 2`\n"
        "`scripts/renamed.py` `scripts/untracked.py`\n"
        "`scripts/verify_*.py` `~/.cache/tradedesk-agents/docs/x.json` `/home/tradedesk/x/scripts/y.py`\n"
    )
    (tmp_path / "docs" / "AGENTS.md").write_text("`docs/HANDOFF.md`\n")
    (tmp_path / "docs" / "ideas" / "IDEA-1-active.md").write_text("# A\n\nСтатус: forward\n\n`tests/test_gone.py`\n")
    (tmp_path / "docs" / "ideas" / "IDEA-2-closed.md").write_text("# B\n\nСтатус: rejected\n\n`tests/test_old.py`\n")
    (tmp_path / "docs" / "other.md").write_text("`scripts/not_scanned.py`\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "scripts/present.py", "docs/HANDOFF.md", "docs/AGENTS.md", "docs/ideas", "docs/other.md"],
        cwd=tmp_path,
        check=True,
    )

    missing, checked, n = docs_paths.check_tree(tmp_path)
    assert n == 3  # HANDOFF, AGENTS, the forward card; the rejected card and other.md are not scanned
    assert checked == 5
    assert [(m.source, m.line, m.target) for m in missing] == [
        ("docs/HANDOFF.md", 2, "scripts/renamed.py"),
        ("docs/HANDOFF.md", 2, "scripts/untracked.py"),
        ("docs/ideas/IDEA-1-active.md", 5, "tests/test_gone.py"),
    ]

    out = tmp_path / "report" / "paths.txt"
    assert docs_paths.main(["--root", str(tmp_path), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed == out.read_text(encoding="utf-8")
    assert "docs/HANDOFF.md:2: missing scripts/renamed.py" in printed
    assert printed.endswith("3 missing path(s) in 2 file(s); 5 path(s) checked in 3 file(s)\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_clean_tree_exits_zero(tmp_path, capsys):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "HANDOFF.md").write_text("`docs/HANDOFF.md`\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "docs/HANDOFF.md"], cwd=tmp_path, check=True)
    assert docs_paths.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "0 missing path(s) in 0 file(s); 1 path(s) checked in 1 file(s)\n"
