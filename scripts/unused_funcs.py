"""Inventory of public functions in src/tradedesk/analytics/ with no reference outside their own file.

Read-only, stdlib ``ast`` only. Rules:

- Candidates: module-level ``def`` / ``async def`` in ``src/tradedesk/analytics/**/*.py`` whose name does not start
  with ``_``. Methods, nested functions and private ``_names`` are not candidates.
- References are collected from every ``*.py`` under ``src/``, ``scripts/`` and ``tests/`` (``__pycache__`` skipped):
  an ``ast.Name`` with that id, an ``ast.Attribute`` with that attr (``mod.f``), or ``from m import f as g``
  (the alias hides the original name, so the import itself counts). A plain ``from m import f`` does not count
  by itself: a use of ``f`` later is a Name. Strings never count, so a name listed in ``__all__`` (own or another
  file's) is not a use; candidates listed in their own ``__all__`` are flagged in the report.
- Matching is by name only, not by module: a call to another module's ``f`` "uses" every candidate called ``f``.
  This biases toward "used", so the unused list is a lower bound. Dynamic access (``getattr(m, "f")``) is not seen.
- A candidate is "unused" when no other file references it; "tests only" when only files under ``tests/`` do.
  References inside the function's own file never count; an unused candidate that is not referenced even in its
  own file (no call, no recursion) is flagged separately ("dead"): an unused one that is referenced there is
  usually a helper that is public by name only.

    uv run python scripts/unused_funcs.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/unused.md]

Exit code 0 always (an inventory, not a gate). Code is never changed.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

TARGET = "src/tradedesk/analytics"
SCAN_DIRS = ("src", "scripts", "tests")


@dataclass(frozen=True)
class Func:
    path: str  # relative to the repository root, "/" separators
    line: int
    name: str
    in_all: bool = False  # listed in the own module's __all__


def candidates(path: str, tree: ast.Module) -> list[Func]:
    """Public module-level functions of one parsed module. Pure."""
    exported: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and node.value is not None:
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return [
        Func(path, n.lineno, n.name, n.name in exported)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
    ]


def references(tree: ast.Module) -> set[str]:
    """Names referenced in one parsed module (see module docstring for what counts). Pure."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            out |= {a.name for a in node.names if a.asname and a.asname != a.name}
    return out


@dataclass
class Report:
    files: int = 0
    funcs: list[Func] = field(default_factory=list)
    unused: list[Func] = field(default_factory=list)
    dead: list[Func] = field(default_factory=list)  # subset of unused: not referenced even in its own file
    tests_only: list[Func] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def analyse(sources: dict[str, str], target: str = TARGET) -> Report:
    """``sources`` = {relative path: text}. No I/O itself."""
    r = Report()
    refs: dict[str, set[str]] = {}
    prefix = target.rstrip("/") + "/"
    for path in sorted(sources):
        r.files += 1
        try:
            tree = ast.parse(sources[path], filename=path)
        except (SyntaxError, ValueError):
            r.unreadable.append(path)
            continue
        refs[path] = references(tree)
        if path.startswith(prefix):
            r.funcs += candidates(path, tree)
    for f in r.funcs:
        users = [p for p, names in refs.items() if p != f.path and f.name in names]
        if not users:
            r.unused.append(f)
            if f.name not in refs[f.path]:
                r.dead.append(f)
        elif all(p.startswith("tests/") for p in users):
            r.tests_only.append(f)
    return r


def _fmt(f: Func, dead: list[Func] | None = None) -> str:
    notes = (["in own `__all__`"] if f.in_all else []) + (["no reference even in own file"] if dead and f in dead else [])
    return f"- `{f.path}:{f.line}` `{f.name}`" + (f" ({'; '.join(notes)})" if notes else "")


def render(r: Report, target: str = TARGET) -> str:
    lines = [
        f"# Public functions in {target} without references outside their own file",
        "",
        f"Files parsed: {r.files}; public functions: {len(r.funcs)}; "
        f"unused outside own file: {len(r.unused)} (of them not referenced even in own file: {len(r.dead)}); "
        f"referenced only from tests/: {len(r.tests_only)}.",
        "",
        "## Unused outside own file (src/, scripts/, tests/)",
        *([_fmt(f, r.dead) for f in r.unused] or ["- none"]),
        "",
        "## Referenced only from tests/",
        *([_fmt(f) for f in r.tests_only] or ["- none"]),
    ]
    if r.unreadable:
        lines += ["", "## Unparseable by ast (not scanned)", *[f"- `{p}`" for p in r.unreadable]]
    return "\n".join(lines) + "\n"


def load_sources(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if p.is_file() and "__pycache__" not in p.parts:
                out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    text = render(analyse(load_sources(args.root.resolve())))
    print(text, end="")
    if args.out:
        out = args.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
