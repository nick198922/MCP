"""Compare third-party imports of src/tradedesk/ and scripts/ with the dependencies declared in pyproject.toml.

Read-only, stdlib only (``ast`` + ``tomllib``). For each scope (``src`` = src/tradedesk/, ``scripts`` = scripts/,
recursively) the script collects top-level imported module names, drops the standard library
(``sys.stdlib_module_names``), relative imports, the own package ``tradedesk`` and the modules that live in scripts/ itself
(``import fixtures_check``; src/ reaches ``tg_send`` the same way), and maps the rest to distribution names: PEP 503 normalisation
(``-``/``_``/``.`` and case) plus the explicit table ``IMPORT_TO_DIST`` for packages whose import name differs
(``yaml`` -> ``PyYAML``). It prints:

- ``imported, not declared``: neither ``[project].dependencies`` nor any extra names the distribution. It may still
  work today through a transitive dependency, but a clean ``make install`` is not guaranteed to provide it;
- ``imported only conditionally``: every import of the module sits in ``try: ... except ImportError`` (or
  ``if TYPE_CHECKING:``), so the code runs without it (information only);
- ``src imports declared only in extras``: the package needs a dev-only distribution at runtime;
- ``declared, not imported``: a ``[project].dependencies`` entry that no file in src/ or scripts/ imports. Extras are
  not checked here: they are tools (pytest, ruff) used from the command line or from tests/.

Limits: dynamic imports (``importlib.import_module("x")``, ``__import__``) are not seen; a distribution whose import
name differs and is missing from ``IMPORT_TO_DIST`` shows up in both lists.

    uv run python scripts/deps_check.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/deps_check.md]

Exit code 1 when src/tradedesk/ imports an undeclared distribution unconditionally; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

OWN_PACKAGE = "tradedesk"
SCOPES = {"src": "src/tradedesk", "scripts": "scripts"}

# Import name -> distribution name, only where PEP 503 normalisation of the import name does not give the distribution.
IMPORT_TO_DIST = {
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "telegram": "python-telegram-bot",
    "sklearn": "scikit-learn",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "OpenSSL": "pyOpenSSL",
    "docx": "python-docx",
    "fitz": "PyMuPDF",
    "attr": "attrs",
    "websocket": "websocket-client",
    "socks": "PySocks",
    "serial": "pyserial",
    "pkg_resources": "setuptools",
    "_pytest": "pytest",
    "xdist": "pytest-xdist",
    "google": "protobuf",
    "mpl_toolkits": "matplotlib",
}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def normalize(name: str) -> str:
    """PEP 503 normalised distribution name. Pure."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(req: str) -> str | None:
    """Distribution name of a PEP 508 requirement string (``"httpx>=0.27"`` -> ``"httpx"``), normalised. Pure."""
    m = _NAME.match(req)
    return normalize(m.group(1)) if m else None


def dist_of(module: str) -> str:
    """Normalised distribution name expected to provide the top-level import ``module``. Pure."""
    return normalize(IMPORT_TO_DIST.get(module, module))


@dataclass(frozen=True)
class Use:
    file: str
    line: int
    conditional: bool  # inside try/except ImportError or `if TYPE_CHECKING:`


def _catches_import_error(node: ast.Try) -> bool:
    for h in node.handlers:
        if h.type is None:
            return True
        types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for t in types:
            name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
            if name in _IMPORT_ERRORS:
                return True
    return False


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def imports_of(source: str, file: str = "<src>") -> dict[str, list[Use]]:
    """{top-level module: uses} of absolute imports in one Python source. Raises SyntaxError. Pure."""
    out: dict[str, list[Use]] = {}

    def add(module: str, line: int, conditional: bool) -> None:
        top = module.split(".", 1)[0]
        if top:
            out.setdefault(top, []).append(Use(file, line, conditional))

    def visit(node: ast.AST, conditional: bool) -> None:
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name, node.lineno, conditional)
            return
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                add(node.module, node.lineno, conditional)
            return
        if isinstance(node, ast.Try | ast.TryStar):
            guarded = conditional or _catches_import_error(node)
            for child in node.body:
                visit(child, guarded)
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                visit(child, conditional)
            return
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.body:
                visit(child, True)
            for child in node.orelse:
                visit(child, conditional)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, conditional)

    visit(ast.parse(source, filename=file), False)
    return out


@dataclass
class Declared:
    main: set[str] = field(default_factory=set)
    extras: dict[str, set[str]] = field(default_factory=dict)  # extra name -> distributions

    @property
    def all(self) -> set[str]:
        return self.main.union(*self.extras.values())


def declared_from_pyproject(text: str) -> Declared:
    """Normalised distribution names of ``[project].dependencies`` and ``[project.optional-dependencies]``. Pure."""
    project = tomllib.loads(text).get("project", {})
    d = Declared(main={n for r in project.get("dependencies", []) if (n := requirement_name(r))})
    for extra, reqs in project.get("optional-dependencies", {}).items():
        d.extras[extra] = {n for r in reqs if (n := requirement_name(r))}
    return d


@dataclass
class ScopeReport:
    files: int = 0
    third_party: dict[str, list[Use]] = field(default_factory=dict)  # module -> uses
    undeclared: list[str] = field(default_factory=list)  # modules, unconditional
    conditional_undeclared: list[str] = field(default_factory=list)
    extras_only: list[str] = field(default_factory=list)  # declared only in an extra
    unparsable: list[str] = field(default_factory=list)


@dataclass
class Report:
    scopes: dict[str, ScopeReport] = field(default_factory=dict)
    unused: list[str] = field(default_factory=list)  # [project].dependencies imported nowhere

    @property
    def failed(self) -> bool:
        src = self.scopes.get("src")
        return bool(src and src.undeclared)


def check_scope(sources: dict[str, str], declared: Declared, local: set[str] | frozenset[str] = frozenset()) -> ScopeReport:
    """``sources`` = {relative path: Python text}; ``local`` = module names provided by the scope itself. Pure."""
    r = ScopeReport(files=len(sources))
    skip = set(sys.stdlib_module_names) | set(sys.builtin_module_names) | {OWN_PACKAGE} | set(local)
    for path in sorted(sources):
        try:
            found = imports_of(sources[path], path)
        except SyntaxError:
            r.unparsable.append(path)
            continue
        for module, uses in found.items():
            if module not in skip:
                r.third_party.setdefault(module, []).extend(uses)
    for module in sorted(r.third_party, key=str.lower):
        dist = dist_of(module)
        if dist in declared.main:
            continue
        if dist in declared.all:
            r.extras_only.append(module)
        elif all(u.conditional for u in r.third_party[module]):
            r.conditional_undeclared.append(module)
        else:
            r.undeclared.append(module)
    return r


def check(scopes: dict[str, tuple[dict[str, str], set[str]]], declared: Declared) -> Report:
    """``scopes`` = {scope name: (sources, local module names)}. No I/O itself."""
    rep = Report(scopes={name: check_scope(src, declared, local) for name, (src, local) in scopes.items()})
    used = {dist_of(m) for s in rep.scopes.values() for m in s.third_party}
    rep.unused = sorted(declared.main - used)
    return rep


def _where(uses: list[Use]) -> str:
    first = min(uses, key=lambda u: (u.file, u.line))
    files = len({u.file for u in uses})
    return f"{first.file}:{first.line}" + (f" (+{files - 1} files)" if files > 1 else "")


def render(rep: Report, declared: Declared) -> str:
    extras = ", ".join(f"{k} {len(v)}" for k, v in sorted(declared.extras.items())) or "none"
    lines = [f"# deps_check: declared {len(declared.main)} dependencies, extras: {extras}"]
    for name, s in rep.scopes.items():
        lines += ["", f"## {name} ({SCOPES.get(name, name)}): {s.files} files, {len(s.third_party)} third-party modules"]
        lines.append("third-party: " + (", ".join(sorted(s.third_party, key=str.lower)) or "-"))
        for title, mods in (
            ("imported, not declared", s.undeclared),
            ("imported only conditionally, not declared (information only)", s.conditional_undeclared),
            ("declared only in extras", s.extras_only),
        ):
            lines += ["", f"{title} ({len(mods)}):"] + ([f"  {m} -> {dist_of(m)}: {_where(s.third_party[m])}" for m in mods] or ["  -"])
        if s.unparsable:
            lines += ["", f"unparsable ({len(s.unparsable)}):"] + [f"  {p}" for p in s.unparsable]
    lines += ["", f"## declared, not imported in any scope ({len(rep.unused)}):"] + ([f"  {d}" for d in rep.unused] or ["  -"])
    return "\n".join(lines) + "\n"


def load_scope(directory: Path) -> tuple[dict[str, str], set[str]]:
    """({relative path: text}, local module names: .py stems and package directories) of a directory tree."""
    files = [p for p in sorted(directory.rglob("*.py")) if "__pycache__" not in p.parts]
    sources = {p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in files}
    local = {p.stem for p in files} | {p.parent.name for p in files if p.name == "__init__.py"}
    return sources, local


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    declared = declared_from_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))
    loaded = {name: load_scope(root / rel) if (root / rel).is_dir() else ({}, set()) for name, rel in SCOPES.items()}
    # Modules living in scripts/ are repository code for both scopes (src imports scripts/tg_send.py via sys.path);
    # src/tradedesk is the own package, its submodules are imported as tradedesk.x.
    repo_local = loaded["scripts"][1]
    scopes = {name: (sources, repo_local) for name, (sources, _) in loaded.items()}
    report = check(scopes, declared)
    text = render(report, declared)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
