"""Check tool names in the Markdown tables of README.md and docs/PARITY.md against the tools build_server() registers.

Read-only. The parsing below is pure stdlib; only ``registered_tools()`` imports the server to list its tools (no
lifespan is entered, so no store, scheduler or network). Only Markdown tables count: a header row, a ``|---|`` separator,
then rows that start with ``|``; a backticked name in prose outside a table is ignored, and so are rows that a line of
prose has cut off from their header (GitHub renders them as text). A name is a backticked snake_case identifier with at
least one underscore (``market_ticker``; not ``probe=true``, ``.A`` or ``COINALYZE_API_KEY``).

README.md: every name in the ``Інструмент`` column is a tool. Lists: in the README but not registered; registered but
not in the README; and the count in the ``## Інструменти (N)`` heading against the registry.

docs/PARITY.md maps DOUBLETOP tools (first column ``DOUBLETOP``) to ours (second column). Its second column also names
DOUBLETOP tools, fields and planned tools, so only status claims count there: a name right after ✅, 🟡 or 🔜, plus
names chained to it by ``,``, ``/``, ``+``, ``і`` or ``та``; names that stand in the first column are DOUBLETOP's and
are dropped. Lists: claimed as present (✅/🟡) but not registered; marked planned (🔜) but already registered. Registered
tools that PARITY never names in a table are listed for information only: PARITY is a map of DOUBLETOP, not a catalogue.

    uv run python scripts/docs_tools_check.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/docs_tools_check.txt]

Exit code 1 when any list except the informational one is not empty, 0 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

README = "README.md"
PARITY = "docs/PARITY.md"
README_COLUMN = "Інструмент"
PARITY_COLUMN = "DOUBLETOP"

_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_NAME = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_CLAIM = re.compile(r"(✅|🟡|🔜)\s*(`[a-z0-9_]+`(?:\s*(?:,|/|\+|і|та)\s*`[a-z0-9_]+`)*)")
_HEADING = re.compile(r"^##\s+Інструменти\s+\((\d+)\)", re.M)


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)

    def column(self, title: str) -> list[str]:
        """Cells of the column whose header is ``title`` (empty if there is none)."""
        if title not in self.header:
            return []
        i = self.header.index(title)
        return [r[i] for r in self.rows if i < len(r)]


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_tables(text: str) -> list[Table]:
    """Markdown tables of ``text``: header + separator + the ``|`` rows right after. Pure."""
    lines = text.splitlines()
    out: list[Table] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1].strip()):
            table = Table(_cells(lines[i]))
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                table.rows.append(_cells(lines[i]))
                i += 1
            out.append(table)
        else:
            i += 1
    return out


def names(cell: str) -> list[str]:
    """Backticked snake_case names in a cell, in order. Pure."""
    return _NAME.findall(cell)


def readme_names(text: str) -> set[str]:
    """Names in the ``Інструмент`` column of every table. Pure."""
    return {n for t in parse_tables(text) for cell in t.column(README_COLUMN) for n in names(cell)}


def heading_count(text: str) -> int | None:
    """N of the ``## Інструменти (N)`` heading, None if absent. Pure."""
    m = _HEADING.search(text)
    return int(m.group(1)) if m else None


def parity_names(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """({mark: names claimed with it}, every name in the second column) of the DOUBLETOP tables. Pure."""
    claims: dict[str, set[str]] = {}
    mentioned: set[str] = set()
    theirs: set[str] = set()
    for t in parse_tables(text):
        if not t.header or t.header[0] != PARITY_COLUMN or len(t.header) < 2:
            continue
        for row in t.rows:
            theirs.update(names(row[0]) if row else [])
            ours = row[1] if len(row) > 1 else ""
            mentioned.update(names(ours))
            for mark, chain in _CLAIM.findall(ours):
                claims.setdefault(mark, set()).update(names(chain))
    claims = {m: ns - theirs for m, ns in claims.items()}
    return claims, mentioned - theirs


@dataclass
class Report:
    registered: int
    readme_count: int | None
    readme_unregistered: list[str]
    readme_missing: list[str]
    parity_unregistered: list[str]
    parity_stale_planned: list[str]
    parity_unmentioned: list[str]  # informational

    @property
    def failed(self) -> bool:
        return bool(
            self.readme_unregistered
            or self.readme_missing
            or self.parity_unregistered
            or self.parity_stale_planned
            or self.readme_count != self.registered
        )


def check(readme: str, parity: str, registry: set[str]) -> Report:
    """Compare the two documents with the registered tool names. No I/O."""
    in_readme = readme_names(readme)
    claims, mentioned = parity_names(parity)
    present = claims.get("✅", set()) | claims.get("🟡", set())
    return Report(
        registered=len(registry),
        readme_count=heading_count(readme),
        readme_unregistered=sorted(in_readme - registry),
        readme_missing=sorted(registry - in_readme),
        parity_unregistered=sorted(present - registry),
        parity_stale_planned=sorted(claims.get("🔜", set()) & registry),
        parity_unmentioned=sorted(registry - mentioned),
    )


def _section(title: str, items: list[str]) -> list[str]:
    return ["", f"{title} ({len(items)}):"] + ([f"  {n}" for n in items] or ["  -"])


def render(r: Report) -> str:
    lines = [f"registered tools: {r.registered}", f"{README} heading count: {r.readme_count}"]
    lines += _section(f"in {README} tables but not registered", r.readme_unregistered)
    lines += _section(f"registered but not in {README} tables", r.readme_missing)
    lines += _section(f"claimed present (✅/🟡) in {PARITY} but not registered", r.parity_unregistered)
    lines += _section(f"marked planned (🔜) in {PARITY} but registered", r.parity_stale_planned)
    lines += _section(f"registered, not named in {PARITY} tables (information only)", r.parity_unmentioned)
    return "\n".join(lines) + "\n"


async def registered_tools() -> set[str]:
    """Names of the tools build_server() registers (no lifespan, no network)."""
    from tradedesk.server import build_server  # noqa: PLC0415

    server, rt = build_server()
    try:
        return {t.name for t in await server.list_tools()}
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None, registry: set[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if registry is None:
        registry = asyncio.run(registered_tools())
    readme = (root / README).read_text(encoding="utf-8")
    parity = (root / PARITY).read_text(encoding="utf-8")
    report = check(readme, parity, registry)
    text = render(report)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
