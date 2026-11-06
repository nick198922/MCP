"""scripts/deps_check.py (item 156 of docs/ideas/IDEA-20261102-standing-improvements.md): imports vs pyproject dependencies."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deps_check as dc  # noqa: E402

PYPROJECT = """
[project]
name = "demo"
dependencies = ["httpx>=0.27", "PyYAML>=6", "Typing_Extensions[x]>=4 ; python_version < '3.12'", "unused-pkg>=1"]

[project.optional-dependencies]
dev = ["pytest>=8"]
"""

SOURCE = """
from __future__ import annotations
import os, json as _json
import httpx
import yaml.constants
from typing_extensions import Self
from . import sibling
from .sub import thing
import tradedesk.core.http
import localhelper
import requests.adapters

try:
    import orjson
except ImportError:
    orjson = None

try:
    import ujson
except ValueError:
    pass

def lazy():
    import pytest
    from TYPE_CHECKING_FAKE import x  # a name, not a guard

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas
"""


def test_normalize_and_requirement_name():
    assert dc.normalize("Typing_Extensions") == dc.normalize("typing.extensions") == "typing-extensions"
    assert dc.requirement_name("Typing_Extensions[x]>=4 ; python_version < '3.12'") == "typing-extensions"
    assert dc.requirement_name("  ") is None
    assert dc.dist_of("yaml") == "pyyaml"  # explicit table
    assert dc.dist_of("pytest_asyncio") == "pytest-asyncio"  # normalisation is enough


def test_imports_of_tops_and_conditions():
    found = dc.imports_of(SOURCE, "m.py")
    assert set(found) == {
        "__future__",
        "os",
        "json",
        "httpx",
        "yaml",
        "typing_extensions",
        "tradedesk",
        "localhelper",
        "requests",
        "orjson",
        "ujson",
        "pytest",
        "TYPE_CHECKING_FAKE",
        "typing",
        "pandas",
    }  # relative imports are dropped
    assert found["orjson"][0].conditional  # try/except ImportError
    assert not found["ujson"][0].conditional  # except ValueError does not guard an import
    assert found["pandas"][0].conditional  # if TYPE_CHECKING
    assert not found["pytest"][0].conditional  # lazy import inside a function still runs
    assert found["yaml"][0] == dc.Use("m.py", 5, False)


def test_check_scope_classifies():
    declared = dc.declared_from_pyproject(PYPROJECT)
    assert declared.main == {"httpx", "pyyaml", "typing-extensions", "unused-pkg"}
    assert declared.extras == {"dev": {"pytest"}}
    s = dc.check_scope({"m.py": SOURCE, "bad.py": "def (:\n"}, declared, local={"localhelper"})
    # stdlib, own package and local modules are not third-party
    assert set(s.third_party) == {
        "httpx",
        "yaml",
        "typing_extensions",
        "requests",
        "orjson",
        "ujson",
        "pytest",
        "TYPE_CHECKING_FAKE",
        "pandas",
    }
    assert s.undeclared == ["requests", "TYPE_CHECKING_FAKE", "ujson"]
    assert s.conditional_undeclared == ["orjson", "pandas"]
    assert s.extras_only == ["pytest"]
    assert s.unparsable == ["bad.py"]
    # one unconditional use makes the module required
    s2 = dc.check_scope({"a.py": "try:\n    import orjson\nexcept ImportError:\n    pass\n", "b.py": "import orjson\n"}, declared)
    assert (s2.undeclared, s2.conditional_undeclared) == (["orjson"], [])


def test_check_unused_and_failed():
    declared = dc.declared_from_pyproject(PYPROJECT)
    rep = dc.check({"src": ({"m.py": "import httpx\n"}, set()), "scripts": ({"s.py": "import yaml\nimport bs4\n"}, set())}, declared)
    assert rep.unused == ["typing-extensions", "unused-pkg"]
    assert not rep.failed  # undeclared imports in scripts/ only are reported, not failed
    assert rep.scopes["scripts"].undeclared == ["bs4"]
    text = dc.render(rep, declared)
    assert "bs4 -> beautifulsoup4: s.py:2" in text
    assert "## declared, not imported in any scope (2):\n  typing-extensions\n  unused-pkg" in text
    assert dc.check({"src": ({"m.py": "import requests\n"}, set())}, declared).failed


def test_main_on_tmp_tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    pkg = tmp_path / "src" / "tradedesk"
    pkg.mkdir(parents=True)
    (pkg / "server.py").write_text("import httpx\nfrom tradedesk.core import x\nimport helper\n", encoding="utf-8")
    scripts = tmp_path / "scripts" / "research"
    scripts.mkdir(parents=True)
    (tmp_path / "scripts" / "helper.py").write_text("import yaml\n", encoding="utf-8")
    (scripts / "r.py").write_text("import helper\nimport numpy\n", encoding="utf-8")
    out = tmp_path / "out" / "deps.md"
    assert dc.main(["--root", str(tmp_path), "--out", str(out)]) == 0  # helper is repository code, also for src
    text = out.read_text(encoding="utf-8")
    assert "numpy -> numpy: research/r.py:2" in text
    (pkg / "extra.py").write_text("import requests\n", encoding="utf-8")
    assert dc.main(["--root", str(tmp_path)]) == 1


def test_main_on_repository(tmp_path):
    out = tmp_path / "deps.md"
    assert dc.main(["--root", str(ROOT), "--out", str(out)]) == 0  # src/tradedesk declares everything it imports
    assert out.read_text(encoding="utf-8").startswith("# deps_check:")
