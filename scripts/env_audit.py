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

``--scripts`` adds a second section for the names only the Python scripts read (``scripts/**/*.py``, ``__pycache__``
left out; same AST rule; names the server code also reads are left to the first section). A name is *set* when a unit or shell file assigns it: ``Environment=NAME=...``
in ``deploy/user-units/*`` or in a unit written by ``scripts/server/*.sh``, ``export NAME=...`` / ``NAME=...`` in
``scripts/server/*.sh`` and ``scripts/*.sh`` (``agents_loop.sh`` exports names to the Python scripts it starts), and
env-file lines those scripts write. Comment lines are skipped. ``EnvironmentFile=`` points outside the repository
(``/etc``, ``~/.config``) and is not opened. The section prints: read by a script but neither set anywhere nor written
in a doc source (``deploy/user-units/README.md`` and ``docs/AGENTS.md`` added to the list above); set by a unit or
shell file but read by nobody (server code, the Python scripts, ``$NAME`` / ``${NAME...}`` in any ``*.sh`` under
``scripts/`` or ``deploy/``). Either list not empty also makes the exit code 1.

    uv run python scripts/env_audit.py [--root .] [--scripts] [--out /tmp/env_audit.txt]
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
SCRIPTS_DIR = "scripts"
UNITS_DIR = "deploy/user-units"
SCRIPT_DOC_SOURCES = (*DOC_SOURCES, f"{UNITS_DIR}/README.md", "docs/AGENTS.md")

_NAME = r"TRADEDESK_[A-Z0-9_]*[A-Z0-9]"
_FULL_NAME = re.compile(rf"^{_NAME}$")
# A whole name in free text: not glued to word characters, and not a prefix pattern such as TRADEDESK_COLLECT_* or
# TRADEDESK_{COLLECTOR,...} (a trailing underscore, star or brace means a family of names, not one name).
_TEXT_NAME = re.compile(rf"(?<![A-Za-z0-9_]){_NAME}(?![A-Za-z0-9_*{{])")
# An assignment in a unit or shell file: NAME= at the start, after a blank, a quote, ';', '=' (Environment=NAME=) or
# a literal \n inside a printf format. Not '^NAME=' / '|NAME)=' inside a grep pattern, not '=='.
_SET_NAME = re.compile(rf"(?:^|(?<=[\s'\"=;])|(?<=\\n))({_NAME})=(?!=)")
# A shell expansion: $NAME or ${NAME...}.
_SHELL_READ = re.compile(rf"\$\{{?({_NAME})(?![A-Za-z0-9_])")


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


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def names_set(text: str) -> set[str]:
    """Names a unit or shell file assigns (``Environment=NAME=``, ``export NAME=``, ``NAME=`` lines). Comments skipped,
    values never captured. Pure."""
    return {m for line in _code_lines(text) for m in _SET_NAME.findall(line)}


def names_shell_read(text: str) -> set[str]:
    """Names a shell file expands (``$NAME``, ``${NAME:-default}``). Comments skipped. Pure."""
    return {m for line in _code_lines(text) for m in _SHELL_READ.findall(line)}


def _where(texts: dict[str, str], extract) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(texts):
        for name in extract(texts[path]):
            out.setdefault(name, []).append(path)
    return out


@dataclass
class ScriptsAudit:
    read: dict[str, list[str]] = field(default_factory=dict)  # name -> scripts reading it (server code does not)
    set_by: dict[str, list[str]] = field(default_factory=dict)  # name -> unit / shell files assigning it
    undocumented: dict[str, list[str]] = field(default_factory=dict)  # read by a script, set nowhere, in no doc source
    set_unread: dict[str, list[str]] = field(default_factory=dict)  # set by a unit / shell file, read by nobody


def audit_scripts(
    py: dict[str, str], setters: dict[str, str], shell: dict[str, str], docs: dict[str, str], code_read: set[str]
) -> ScriptsAudit:
    """Names the Python scripts ``py`` read vs names assigned in ``setters`` (units, shell scripts) and written in
    ``docs``; ``shell`` (path -> shell text) and ``code_read`` (names the server code reads) only count as readers.
    All inputs are path -> text; no I/O."""
    py_read = _where(py, names_read)
    read = {n: f for n, f in sorted(py_read.items()) if n not in code_read}
    set_by = dict(sorted(_where(setters, names_set).items()))
    documented = set().union(*(names_written(t) for t in docs.values())) if docs else set()
    readers = code_read | py_read.keys() | set().union(*(names_shell_read(t) for t in shell.values()))
    return ScriptsAudit(
        read=read,
        set_by=set_by,
        undocumented={n: f for n, f in read.items() if n not in set_by and n not in documented},
        set_unread={n: f for n, f in set_by.items() if n not in readers},
    )


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


def _texts(root: Path, paths) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(paths)
        if p.is_file() and "__pycache__" not in p.parts
    }


def collect_scripts(root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """(py: scripts/**/*.py, setters: deploy/user-units/* + scripts/server/*.sh + scripts/*.sh, shell: every *.sh
    under scripts/ and deploy/, docs: SCRIPT_DOC_SOURCES that exist), each repo path -> text."""
    scripts, units = root / SCRIPTS_DIR, root / UNITS_DIR
    py = _texts(root, scripts.rglob("*.py"))
    unit_files = [p for p in units.glob("*") if p.name != "README.md"] if units.is_dir() else []
    setters = _texts(root, [*unit_files, *(scripts / "server").glob("*.sh"), *scripts.glob("*.sh")])
    shell = _texts(root, [*scripts.rglob("*.sh"), *(root / "deploy").rglob("*.sh")])
    docs = {rel: (root / rel).read_text(encoding="utf-8", errors="replace") for rel in SCRIPT_DOC_SOURCES if (root / rel).is_file()}
    return py, setters, shell, docs


def render_scripts(s: ScriptsAudit) -> str:
    lines = [
        "",
        f"names read only by {SCRIPTS_DIR}/**/*.py: {len(s.read)}; set by units / shell scripts: {len(s.set_by)}",
        "",
        f"read by a script, set nowhere, documented nowhere ({len(s.undocumented)}):",
    ]
    lines += [f"  {n}  ({', '.join(f)})" for n, f in s.undocumented.items()] or ["  -"]
    lines += ["", f"set by a unit / shell script, read by nobody ({len(s.set_unread)}):"]
    lines += [f"  {n}  ({', '.join(f)})" for n, f in s.set_unread.items()] or ["  -"]
    return "\n".join(lines) + "\n"


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
    ap.add_argument("--scripts", action="store_true", help="also audit names only scripts/ read vs units and shell scripts")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    a = audit(*collect(root))
    text = render(a)
    bad = bool(a.undocumented or a.unread)
    if args.scripts:
        s = audit_scripts(*collect_scripts(root), code_read=a.read)
        text += render_scripts(s)
        bad = bad or bool(s.undocumented or s.set_unread)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
