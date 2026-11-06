"""scripts/fix_run_links.py: run-file links one directory short are repaired, everything else is left alone."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fix_run_links as fx  # noqa: E402


def test_short_links_are_repaired_and_good_or_unknown_ones_kept(tmp_path):
    (tmp_path / "docs/agents/runs").mkdir(parents=True)
    (tmp_path / "docs/reviews").mkdir()
    (tmp_path / "docs/reviews/r.md").write_text("x")
    (tmp_path / "docs/agents/runs/other.md").write_text("x")
    here = tmp_path / "docs/agents/runs"
    text = (
        "[rev](../reviews/r.md#top) · [ok](../../reviews/r.md) · [same dir](other.md) · "
        "[missing](../reviews/none.md) · [web](https://x.y/../reviews/r.md) · [<a>](<../reviews/r.md>)"
    )
    new, n = fx.fix_text(text, here)
    assert n == 2
    assert "[rev](../../reviews/r.md#top)" in new and "[<a>](<../../reviews/r.md>)" in new
    assert "[ok](../../reviews/r.md)" in new and "[missing](../reviews/none.md)" in new
    assert "https://x.y/../reviews/r.md" in new


def test_main_rewrites_given_files_and_prints_them(tmp_path, capsys):
    (tmp_path / "docs/agents/runs").mkdir(parents=True)
    (tmp_path / "docs/changes").mkdir()
    (tmp_path / "docs/changes/c.md").write_text("x")
    run = tmp_path / "docs/agents/runs/2026-11-06-x.md"
    run.write_text("**Результат:** [нотатка](../changes/c.md)\n")
    assert fx.main([str(run)]) == 0
    assert run.read_text() == "**Результат:** [нотатка](../../changes/c.md)\n"
    assert capsys.readouterr().out.strip() == str(run)
    assert fx.main([str(run)]) == 0 and capsys.readouterr().out == ""
