"""scripts/deps_check.py (item 156 of docs/ideas/IDEA-20261102-standing-improvements.md): imports vs pyproject dependencies."""

import importlib
import sys
from pathlib import Path

import pytest

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


def test_main_on_repository(tmp_path, monkeypatch):
    """src/tradedesk declares everything it imports; BACKLOG 6.38: scripts/ imports nothing undeclared unconditionally,
    media scripts stop with their `uv run --no-project --with ...` command instead of an ImportError.

    The scripts/ scope is read from the report `main` builds (the same `load_scope` sources and local names, the same
    pyproject), so the repository is parsed once, not once more for that check."""
    reports = []
    real_check = dc.check
    monkeypatch.setattr(dc, "check", lambda scopes, declared: reports.append(real_check(scopes, declared)) or reports[-1])
    out = tmp_path / "deps.md"
    assert dc.main(["--root", str(ROOT), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# deps_check:")
    [rep] = reports
    s = rep.scopes["scripts"]
    assert s.files == len(dc.load_scope(ROOT / "scripts")[0]) > 200
    assert s.undeclared == []
    media = {"bs4", "faster_whisper", "imageio_ffmpeg", "mlx_whisper", "PIL", "youtube_transcript_api", "matplotlib"}
    assert media <= set(s.conditional_undeclared)


FIND_SPEC = """
import importlib.util
from importlib.util import find_spec

if importlib.util.find_spec("mlx_whisper"):
    import mlx_whisper
    import other_pkg
else:
    import faster_whisper
if find_spec("av.codec") is not None:
    from av import open
if importlib.util.find_spec(name):
    import dynamic_pkg
"""


def test_find_spec_guard_is_conditional_for_its_module_only():
    found = dc.imports_of(FIND_SPEC, "f.py")
    assert found["mlx_whisper"][0].conditional
    assert found["av"][0].conditional  # `is not None`, dotted name -> top module
    assert not found["other_pkg"][0].conditional  # the guard checked another module
    assert not found["faster_whisper"][0].conditional  # else branch
    assert not found["dynamic_pkg"][0].conditional  # not a literal module name


NESTED = """
import contextlib

def f():
    import in_def

async def g():
    import in_async_def
    async with ctx():
        import in_async_with
    async for _ in it():
        import in_async_for

class C:
    import in_class
    def m(self):
        try:
            import in_method_try
        except ImportError:
            pass

try:
    pass
except ValueError:
    import in_handler
else:
    import in_try_else
finally:
    import in_finally

try:
    pass
except* OSError:
    import in_try_star_handler

if x:
    import in_if
elif y:
    import in_elif
else:
    import in_if_else

with contextlib.suppress(Exception):
    import in_with

for _ in range(1):
    import in_for
else:
    import in_for_else

while False:
    import in_while
else:
    import in_while_else

match x:
    case 1:
        import in_case
    case _:
        if find_spec("in_case_spec"):
            import in_case_spec
"""


def test_imports_nested_in_statements_are_found():
    """Item 192: only statement bodies are walked; every place an import statement can sit is still covered."""
    found = dc.imports_of(NESTED, "n.py")
    expected = {
        "in_def",
        "in_async_def",
        "in_async_with",
        "in_async_for",
        "in_class",
        "in_method_try",
        "in_handler",
        "in_try_else",
        "in_finally",
        "in_try_star_handler",
        "in_if",
        "in_elif",
        "in_if_else",
        "in_with",
        "in_for",
        "in_for_else",
        "in_while",
        "in_while_else",
        "in_case",
        "in_case_spec",
    }
    assert set(found) == expected | {"contextlib"}
    assert {m for m in expected if found[m][0].conditional} == {"in_method_try", "in_case_spec"}
    assert found["in_case"][0] == dc.Use("n.py", 58, False)


@pytest.mark.parametrize(
    ("script", "missing", "call", "needs"),
    [
        ("tg_export", "bs4", lambda m, p: m.parse(p), "beautifulsoup4"),
        ("tg_videos", "imageio_ffmpeg", lambda m, p: m.duration(p / "v.mp4"), "imageio-ffmpeg"),
        ("yt_asr", "imageio_ffmpeg", lambda m, p: m.decode(p / "a.m4a"), "imageio-ffmpeg"),
        ("yt_frames", "PIL", lambda m, p: m.differences([p / "f.jpg"], 6.0), "pillow"),
        ("yt_frames", "imageio_ffmpeg", lambda m, p: m.extract_at(p / "v.mp4", p, [1.0]), "imageio-ffmpeg"),
        ("yt_transcript", "youtube_transcript_api", lambda m, p: m.fetch("dQw4w9WgXcQ"), "youtube-transcript-api"),
    ],
)
def test_media_script_without_its_package_exits_with_run_command(script, missing, call, needs, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, missing, None)  # import -> ImportError
    mod = importlib.import_module(script)
    with pytest.raises(SystemExit) as e:
        call(mod, tmp_path)
    msg = str(e.value.code)
    assert needs in msg and "uv run --no-project --with" in msg


def test_lesson_sheets_without_pillow_exits_with_run_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.delitem(sys.modules, "lesson_sheets", raising=False)
    with pytest.raises(SystemExit) as e:
        importlib.import_module("lesson_sheets")
    assert "uv run --no-project --with pillow" in str(e.value.code)
