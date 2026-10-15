"""scripts/agents_rm.py: the agents may delete only their own untracked files in the clone or the cache directory."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agents_rm  # noqa: E402


@pytest.fixture
def clone(tmp_path, monkeypatch):
    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("TRADEDESK_AGENTS_CACHE_DIR", str(cache))
    monkeypatch.chdir(repo)
    return repo, cache


def test_deletes_untracked_file_in_clone_and_cache(clone, capsys):
    repo, cache = clone
    (repo / "scripts").mkdir()
    helper = repo / "scripts" / "_inspect_json.py"
    helper.write_text("print(1)\n")
    scratch = cache / "slug" / "out.txt"
    scratch.parent.mkdir()
    scratch.write_text("tmp\n")
    assert agents_rm.main(["scripts/_inspect_json.py", str(scratch)]) == 0
    assert not helper.exists() and not scratch.exists()
    assert agents_rm.main([str(scratch.parent)]) == 0  # now an empty directory
    assert not scratch.parent.exists()
    assert "deleted: scripts/_inspect_json.py" in capsys.readouterr().out


def test_refuses_tracked_git_outside_symlink_and_full_directory(clone, tmp_path, capsys):
    repo, cache = clone
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (repo / "link.py").symlink_to(outside)
    (repo / "full").mkdir()
    (repo / "full" / "a.txt").write_text("a\n")
    args = ["tracked.py", ".git/HEAD", str(outside), "link.py", "full", "missing.txt", str(cache), "."]
    assert agents_rm.main(args) == 1
    out = capsys.readouterr().out
    assert out.count("refused:") == len(args)
    assert "tracked by git" in out and "inside .git" in out and "symbolic link" in out
    assert (repo / "tracked.py").exists() and (repo / ".git" / "HEAD").exists() and outside.exists()
    assert (repo / "full" / "a.txt").exists() and cache.exists()


def test_no_arguments_prints_usage(clone, capsys):
    assert agents_rm.main([]) == 1
    assert "agents_rm.py PATH" in capsys.readouterr().out
