"""scripts/unused_funcs.py (item 148 of docs/ideas/IDEA-20261102-standing-improvements.md): unused analytics funcs."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import unused_funcs as uf  # noqa: E402

LIB = '''"""Analytics module."""

__all__ = ["exported_only", "used_elsewhere"]


def used_elsewhere(x):
    return own_only(x)


def own_only(x):
    return x


async def tests_only(x):
    return x


def exported_only(x):
    return x


def aliased(x):
    return x


def _private(x):
    return used_elsewhere(x)


class Model:
    def method(self):
        return 1
'''

TOOL = """from tradedesk.analytics import lib
from tradedesk.analytics.lib import aliased as al, exported_only

__all__ = ["exported_only", "own_only"]

print(lib.used_elsewhere(1), al(2), "own_only")
"""

TEST = """from tradedesk.analytics.lib import tests_only


def test_it():
    assert tests_only
"""


def _write(root: Path) -> None:
    (root / "src/tradedesk/analytics/__pycache__").mkdir(parents=True)
    (root / "src/tradedesk/analytics/lib.py").write_text(LIB, encoding="utf-8")
    (root / "src/tradedesk/analytics/__pycache__/x.py").write_text("def ghost():\n    pass\n", encoding="utf-8")
    (root / "src/tradedesk/tools").mkdir(parents=True)
    (root / "src/tradedesk/tools/tool.py").write_text(TOOL, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_lib.py").write_text(TEST, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/notes.py").write_text("own_only()\n", encoding="utf-8")  # outside scanned dirs


def test_candidates_public_module_level_only():
    fs = uf.candidates("lib.py", ast.parse(LIB))
    assert [(f.name, f.line, f.in_all) for f in fs] == [
        ("used_elsewhere", 6, True),
        ("own_only", 10, False),
        ("tests_only", 14, False),
        ("exported_only", 18, True),
        ("aliased", 22, False),
    ]


def test_references_names_attrs_aliases_not_strings():
    refs = uf.references(ast.parse(TOOL))
    assert {"used_elsewhere", "aliased", "al", "lib", "print"} <= refs
    assert "own_only" not in refs  # only a string / __all__ entry
    assert "exported_only" not in refs  # plain import without use


def test_analyse_on_tree(tmp_path):
    _write(tmp_path)
    out = tmp_path / "out/unused.md"
    assert uf.main(["--root", str(tmp_path), "--out", str(out)]) == 0
    r = uf.analyse(uf.load_sources(tmp_path))
    assert r.files == 3
    assert [f.name for f in r.funcs] == ["used_elsewhere", "own_only", "tests_only", "exported_only", "aliased"]
    # own_only is called only inside lib.py; exported_only is only in __all__ and a bare import
    assert [f.name for f in r.unused] == ["own_only", "exported_only"]
    assert [f.name for f in r.dead] == ["exported_only"]  # own_only is still called by used_elsewhere
    assert [f.name for f in r.tests_only] == ["tests_only"]
    text = out.read_text(encoding="utf-8")
    assert "public functions: 5; unused outside own file: 2 (of them not referenced even in own file: 1); " in text
    assert "referenced only from tests/: 1." in text
    assert "- `src/tradedesk/analytics/lib.py:10` `own_only`\n" in text
    assert "- `src/tradedesk/analytics/lib.py:18` `exported_only` (in own `__all__`; no reference even in own file)" in text


def test_unparseable_and_empty():
    r = uf.analyse({"src/tradedesk/analytics/bad.py": "def f(:\n"})
    assert r.unreadable == ["src/tradedesk/analytics/bad.py"] and r.funcs == []
    assert "- none" in uf.render(uf.analyse({}))
