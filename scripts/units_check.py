"""Check the systemd user units in deploy/user-units/ against the repository.

Read-only, stdlib only. For every ``*.service``: each script that an ``ExecStart``/``ExecStartPre``/``ExecStartPost``
line runs exists in the repository. For every ``*.timer``: the unit it starts (``Unit=`` or the ``.service`` of the same
name) is one of the units in the directory. A ``*.service`` that no timer starts is listed for information only
(long-running or manual units are legitimate).

How a command line is read (``command_refs``): systemd specifiers are expanded first (``%h`` is the home of the user the
units run as, ``%%`` is ``%``); the line is split like a shell would; the string after ``bash -c`` / ``sh -c`` is split
again, with ``|``, ``;``, ``&&``, ``{`` and ``}`` as separators. A token is a repository reference when it is:

- a script: it ends in ``.py`` or ``.sh`` (``scripts/x.py``, ``/home/tradedesk/tradedesk-mcp/scripts/backup.sh``);
- a module: the argument of ``python -m`` (``tradedesk.execution.demo_trader`` -> ``src/tradedesk/execution/
  demo_trader.py`` or the package's ``__main__.py``);
- a file: any other relative path with a suffix that is not the value of an option (``cat docs/CHECKPOINT.md``; not
  ``--json out/latest.json``).

An absolute path counts only when it lies under the deploy root (``/home/tradedesk/tradedesk-mcp``); system binaries and
data paths such as ``/var/lib/...`` are skipped. A relative path is resolved against ``WorkingDirectory=``; when that is
not the deploy root (user units start in the home directory by default), the reference is listed as unresolvable,
because it would not be found at run time either. ``EnvironmentFile=`` and ``Environment=`` values are never read.

    uv run python scripts/units_check.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/units.txt]

Exit code 1 when a reference is missing or unresolvable, or a timer has no unit to start; 0 otherwise.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

UNITS_DIR = "deploy/user-units"
HOME = "/home/tradedesk"
DEPLOY_ROOT = "/home/tradedesk/tradedesk-mcp"
EXEC_KEYS = ("ExecStart", "ExecStartPre", "ExecStartPost")
SCRIPT_SUFFIXES = (".py", ".sh")
SHELLS = {"bash", "sh", "dash", "zsh"}
SEPARATORS = {"|", "||", "&", "&&", ";", "{", "}", "(", ")"}

_REL_PATH = re.compile(r"^(?:\./)?[\w.-]+(?:/[\w.-]+)*$")
_SPECIFIER = re.compile(r"%(.)")


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """{section: {key: [values in order]}} of a unit file. Comments (``#``/``;``) and blank lines are skipped, a trailing
    backslash continues the line. Pure."""
    out: dict[str, dict[str, list[str]]] = {}
    section = ""
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line[0] in "#;"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            out.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            out.setdefault(section, {}).setdefault(key.strip(), []).append(value.strip())
    return out


def expand(value: str, home: str = HOME) -> str:
    """Expand the systemd specifiers that matter here: ``%h`` -> home, ``%%`` -> ``%``; others are left as is. Pure."""
    return _SPECIFIER.sub(lambda m: {"h": home, "%": "%"}.get(m.group(1), m.group(0)), value)


def _split(command: str, shell: bool) -> list[str]:
    if not shell:
        return shlex.split(command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}")
    lex.whitespace_split = True
    return list(lex)


def _tokens(command: str, shell: bool = False) -> list[list[str]]:
    """Simple commands of ``command`` (argv lists); ``<shell> -c '<script>'`` is split again. Pure."""
    try:
        words = _split(command, shell)
    except ValueError:
        return [command.split()]
    commands: list[list[str]] = [[]]
    for w in words:
        if shell and w in SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(w)
    out: list[list[str]] = []
    for argv in commands:
        if not argv:
            continue
        prog = PurePosixPath(argv[0].lstrip("-@:+!")).name
        if prog in SHELLS and "-c" in argv[1:]:
            i = argv.index("-c", 1)
            if i + 1 < len(argv):
                out.extend(_tokens(argv[i + 1], shell=True))
                continue
        out.append(argv)
    return out


@dataclass(frozen=True)
class Ref:
    unit: str
    kind: str  # script | module | file
    raw: str
    path: str | None  # repository-relative path, None when it cannot be resolved to the repository


def _repo_path(token: str, workdir: str | None, deploy_root: str) -> tuple[bool, str | None]:
    """(is a repository reference, repo-relative path or None if unresolvable). Pure."""
    root = deploy_root.rstrip("/") + "/"
    if token.startswith("/"):
        if token.startswith(root):
            return True, token[len(root) :]
        return False, None
    if not _REL_PATH.match(token):
        return False, None
    if workdir is not None and workdir.rstrip("/") == deploy_root.rstrip("/"):
        return True, str(PurePosixPath(token))
    return True, None


def command_refs(unit: str, command: str, workdir: str | None, home: str = HOME, deploy_root: str = DEPLOY_ROOT) -> list[Ref]:
    """Repository references of one Exec line. ``workdir`` is the expanded ``WorkingDirectory=`` or None. Pure."""
    refs: list[Ref] = []
    for argv in _tokens(expand(command, home)):
        argv = [argv[0].lstrip("-@:+!"), *argv[1:]]
        for i, tok in enumerate(argv):
            prev = argv[i - 1] if i else ""
            if prev == "-m" and any(PurePosixPath(a).name.startswith("python") for a in argv[: i - 1]):
                path = None
                if workdir is not None and workdir.rstrip("/") == deploy_root.rstrip("/"):
                    path = "src/" + tok.replace(".", "/")
                refs.append(Ref(unit, "module", tok, path))
                continue
            name = PurePosixPath(tok).name
            if tok.endswith(SCRIPT_SUFFIXES):
                kind = "script"
            elif "/" in tok and "." in name and not prev.startswith("-") and not tok.startswith("/"):
                kind = "file"
            elif tok.startswith("/") and "." in name and not prev.startswith("-"):
                kind = "file"
            else:
                continue
            is_ref, path = _repo_path(tok, workdir, deploy_root)
            if is_ref:
                refs.append(Ref(unit, kind, tok, path))
    return refs


@dataclass
class Report:
    services: int = 0
    timers: int = 0
    refs: list[Ref] = field(default_factory=list)
    missing: list[Ref] = field(default_factory=list)
    unresolved: list[Ref] = field(default_factory=list)
    no_exec: list[str] = field(default_factory=list)
    timer_orphans: list[tuple[str, str]] = field(default_factory=list)  # (timer, unit it would start)
    untimed_services: list[str] = field(default_factory=list)  # informational

    @property
    def failed(self) -> bool:
        return bool(self.missing or self.unresolved or self.no_exec or self.timer_orphans)


def module_exists(path: str, exists: Callable[[str], bool]) -> bool:
    """``src/a/b`` of ``python -m a.b`` runs ``src/a/b.py`` or ``src/a/b/__main__.py``. Pure given ``exists``."""
    return exists(path + ".py") or exists(path + "/__main__.py")


def check(units: dict[str, str], exists: Callable[[str], bool], home: str = HOME, deploy_root: str = DEPLOY_ROOT) -> Report:
    """Check unit texts ``{file name: text}`` against the repository (``exists(repo-relative path)``). No I/O itself."""
    r = Report()
    parsed = {name: parse_unit(text) for name, text in sorted(units.items())}
    started: set[str] = set()
    for name, unit in parsed.items():
        if not name.endswith(".timer"):
            continue
        r.timers += 1
        target = (unit.get("Timer", {}).get("Unit") or [name[: -len(".timer")] + ".service"])[-1]
        started.add(target)
        if target not in parsed:
            r.timer_orphans.append((name, target))
    for name, unit in parsed.items():
        if not name.endswith(".service"):
            continue
        r.services += 1
        svc = unit.get("Service", {})
        wd_raw = (svc.get("WorkingDirectory") or [None])[-1]
        workdir = None
        if wd_raw is not None:
            wd = expand(wd_raw.lstrip("-"), home)
            workdir = home if wd == "~" else wd
        lines = [v for k in EXEC_KEYS for v in svc.get(k, []) if v]
        if not svc.get("ExecStart"):
            r.no_exec.append(name)
        for line in lines:
            for ref in command_refs(name, line, workdir, home, deploy_root):
                r.refs.append(ref)
                if ref.path is None:
                    r.unresolved.append(ref)
                elif not (module_exists(ref.path, exists) if ref.kind == "module" else exists(ref.path)):
                    r.missing.append(ref)
        if name not in started:
            r.untimed_services.append(name)
    return r


def _section(title: str, items: list[str]) -> list[str]:
    return ["", f"{title} ({len(items)}):"] + ([f"  {n}" for n in items] or ["  -"])


def render(r: Report) -> str:
    lines = [
        f"{UNITS_DIR}: {r.services} services, {r.timers} timers, {len(r.refs)} repository references",
    ]
    lines += _section("referenced, missing in the repository", [f"{x.unit}: {x.kind} {x.raw} -> {x.path}" for x in r.missing])
    lines += _section(
        "relative reference, WorkingDirectory is not the deploy root",
        [f"{x.unit}: {x.kind} {x.raw}" for x in r.unresolved],
    )
    lines += _section("service without ExecStart", r.no_exec)
    lines += _section("timer whose unit is not in the directory", [f"{t} -> {u}" for t, u in r.timer_orphans])
    lines += _section("service that no timer starts (information only)", r.untimed_services)
    lines += _section("references checked", [f"{x.unit}: {x.kind} {x.path}" for x in r.refs if x.path])
    return "\n".join(lines) + "\n"


def load_units(directory: Path) -> dict[str, str]:
    """{file name: text} of the ``*.service`` and ``*.timer`` files in ``directory``."""
    return {
        p.name: p.read_text(encoding="utf-8") for p in sorted(directory.iterdir()) if p.is_file() and p.suffix in (".service", ".timer")
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--home", default=HOME, help="home of the user the units run as (for %%h)")
    ap.add_argument("--deploy-root", default=DEPLOY_ROOT, help="where the repository is checked out on the server")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    units = load_units(root / UNITS_DIR)
    report = check(units, lambda rel: (root / rel).is_file(), args.home, args.deploy_root)
    text = render(report)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
