"""scripts/todo_inventory.py (item 147 of docs/ideas/IDEA-20261102-standing-improvements.md): TODO inventory."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import todo_inventory as ti  # noqa: E402

TOOLS = '''"""Docstring with a TODO that is not a comment."""

x = 1  # TODO: retry the venue call on 429 responses
s = "FIXME inside a string literal"
todo = [1, 2]  # lower-case variable name, not a marker
# TODOS and xxxx and XXXL are not markers
# FIXME pagination stops at the first page
'''

SCRIPT = """# XXX: hard-coded symbol list
print("ok")
"""

BACKLOG = "- [ ] 6.99 Something about `tools/market.py` and more.\n- Note: Pagination stops at the first page here.\n"


def _write(root: Path) -> None:
    (root / "src/tradedesk/tools").mkdir(parents=True)
    (root / "src/tradedesk/tools/market.py").write_text(TOOLS, encoding="utf-8")
    (root / "src/tradedesk/tools/other.py").write_text(TOOLS, encoding="utf-8")
    (root / "scripts/__pycache__").mkdir(parents=True)
    (root / "scripts/run.py").write_text(SCRIPT, encoding="utf-8")
    (root / "scripts/__pycache__/run.py").write_text(SCRIPT, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_x.py").write_text(SCRIPT, encoding="utf-8")  # not scanned
    (root / "docs").mkdir()
    (root / "docs/BACKLOG.md").write_text(BACKLOG, encoding="utf-8")


def test_scan_only_comment_markers():
    items = ti.scan_source("a.py", TOOLS)
    assert [(i.line, i.marker) for i in items] == [(3, "TODO"), (7, "FIXME")]
    assert items[0].text == "TODO: retry the venue call on 429 responses"
    assert items[0].snippet == "retry the venue call on 429 responses"
    assert ti.scan_source("b.py", "a = 'TODO'  # TODOS\n") == []


def test_mentioned_by_path_or_snippet():
    by_path = ti.Item("src/tradedesk/tools/market.py", 3, "TODO", "TODO: anything")
    by_text = ti.Item("src/tradedesk/tools/other.py", 7, "FIXME", "FIXME pagination stops at the first page")
    neither = ti.Item("scripts/run.py", 1, "XXX", "XXX: hard-coded symbol list")
    short = ti.Item("src/tradedesk/tools/other.py", 1, "TODO", "TODO: first")  # snippet < 10 chars
    assert ti.mentioned(by_path, BACKLOG)
    assert ti.mentioned(by_text, BACKLOG)
    assert not ti.mentioned(neither, BACKLOG)
    assert not ti.mentioned(short, "first")


def test_inventory_on_tree(tmp_path):
    _write(tmp_path)
    out = tmp_path / "out/todo.md"
    assert ti.main(["--root", str(tmp_path), "--out", str(out)]) == 0
    r = ti.inventory(ti.load_sources(tmp_path), BACKLOG)
    assert r.files == 3
    assert [(i.path, i.line, i.marker) for i in r.items] == [
        ("scripts/run.py", 1, "XXX"),
        ("src/tradedesk/tools/market.py", 3, "TODO"),
        ("src/tradedesk/tools/market.py", 7, "FIXME"),
        ("src/tradedesk/tools/other.py", 3, "TODO"),
        ("src/tradedesk/tools/other.py", 7, "FIXME"),
    ]
    # market.py is named in BACKLOG; other.py FIXME matches by text; the rest is not mentioned
    assert [(i.path, i.line) for i in r.unmentioned] == [("scripts/run.py", 1), ("src/tradedesk/tools/other.py", 3)]
    text = out.read_text(encoding="utf-8")
    assert "comments found: 5; not mentioned in docs/BACKLOG.md: 2" in text
    assert "- `scripts/run.py:1`: XXX: hard-coded symbol list" in text


def test_unreadable_and_empty(tmp_path):
    r = ti.inventory({"bad.py": 'x = """unterminated\n'}, "")
    assert r.unreadable == ["bad.py"] and r.items == []
    assert "- none" in ti.render(ti.inventory({}, ""))
