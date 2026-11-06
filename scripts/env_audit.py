"""Audit ``TRADEDESK_*`` environment variable names: what the code reads vs what the deploy files and docs describe.

Read-only (stdlib only). Names only: the script never looks at ``os.environ`` and never parses a value, so no key or
token can end up in its output. Code side: every ``.py`` file under ``src/tradedesk/``; a name counts as read when it
is a string literal (the whole literal is the name) passed as the first argument of a call (``os.environ.get``,
``os.getenv``, ``_float`` in ``config.py`` ...) or used as a subscript of ``...environ[...]``. Names that only appear in
messages, docstrings or tables (``analytics/collector_health.py``) are not reads. Docs side: ``deploy/env.example``
(the template), ``deploy/features.env``, ``.env.example`` (local stdio template), ``README.md``, ``deploy/README.md``
and ``docs/ARCHITECTURE.md``; a name
counts as documented wherever it is written, commented-out lines included. Journals (``docs/changes``, ``docs/agents``,
``docs/reviews``, ``docs/history``, idea cards) are not documentation and are not scanned.

Prints three lists: read but documented nowhere; read but missing from ``deploy/env.example``; documented but not read
by the code (with the files that mention it). ``--out FILE`` also writes the report. Exit code 1 when the first or the
third list is not empty, 0 otherwise.

    uv run python scripts/env_audit.py [--root .] [--out /tmp/env_audit.txt]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIR = "src/tradedesk"
TEMPLATE = "deploy/env.example"
DOC_SOURCES = (TEMPLATE, "deploy/features.env", ".env.example", "README.md", "deploy/README.md", "docs/ARCHITECTURE.md")

_FULL_NAME = re.compile(r"^TRADEDESK_[A-Z0-9_]*[A-Z0-9]$")
# A whole name in free text: not glued to word characters, and not a prefix pattern such as TRADEDESK_COLLECT_* or
# TRADEDESK_{COLLECTOR,...} (a trailing underscore, star or brace means a family of names, not one name).
_TEXT_NAME = re.compile(r"(?<![A-Za-z0-9_])TRADEDESK_[A-Z0-9_]*[A-Z0-9](?![A-Za-z0-9_*{])")


def _name_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _FULL_NAME.match(node.value):
        return node.value
    return None


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (isinstance(node, ast.Name) and node.id == "environ")


def names_read(source: str) -> set[str]:
    """``TRADEDESK_*`` names a Python module reads: first argument of a call or ``environ[...]`` subscript. Pure."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            name = _name_const(node.args[0])
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            name = _name_const(node.slice)
        else:
            name = None
        if name:
            out.add(name)
    return out


def names_written(text: str) -> set[str]:
    """Whole ``TRADEDESK_*`` names written in a document or an env file (prefix patterns left out). Pure."""
    return set(_TEXT_NAME.findall(text))


@dataclass
class Audit:
    read: set[str] = field(default_factory=set)
    undocumented: list[str] = field(default_factory=list)  # read by the code, written in no doc source
    not_in_template: list[str] = field(default_factory=list)  # read by the code, absent from deploy/env.example
    unread: dict[str, list[str]] = field(default_factory=dict)  # documented name -> doc sources, code never reads it


def audit(code: dict[str, str], docs: dict[str, str], template: str = TEMPLATE) -> Audit:
    """Compare names read by ``code`` (path -> Python source) with names written in ``docs`` (path -> text). No I/O."""
    read: set[str] = set()
    for src in code.values():
        read |= names_read(src)
    where: dict[str, list[str]] = {}
    for path in sorted(docs):
        for name in names_written(docs[path]):
            where.setdefault(name, []).append(path)
    in_template = names_written(docs.get(template, ""))
    return Audit(
        read=read,
        undocumented=sorted(read - where.keys()),
        not_in_template=sorted(read - in_template),
        unread={name: where[name] for name in sorted(where.keys() - read)},
    )


def collect(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(code: repo path -> source under src/tradedesk, docs: repo path -> text of the doc sources that exist)."""
    code = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((root / CODE_DIR).rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    docs = {rel: (root / rel).read_text(encoding="utf-8", errors="replace") for rel in DOC_SOURCES if (root / rel).is_file()}
    return code, docs


def render(a: Audit) -> str:
    lines = [f"names read by {CODE_DIR}: {len(a.read)}", "", f"read but documented nowhere ({len(a.undocumented)}):"]
    lines += [f"  {n}" for n in a.undocumented] or ["  -"]
    lines += ["", f"read but missing from {TEMPLATE} ({len(a.not_in_template)}):"]
    lines += [f"  {n}" for n in a.not_in_template] or ["  -"]
    lines += ["", f"documented but not read by the code ({len(a.unread)}):"]
    lines += [f"  {n}  ({', '.join(src)})" for n, src in a.unread.items()] or ["  -"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    a = audit(*collect(root))
    text = render(a)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if a.undocumented or a.unread else 0


if __name__ == "__main__":
    sys.exit(main())
