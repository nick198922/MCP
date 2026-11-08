"""Inventory of TODO / FIXME / XXX comments in src/ and scripts/, tied to docs/BACKLOG.md.

Read-only, stdlib only. Rules:

- Only ``*.py`` files under ``src/`` and ``scripts/`` are scanned; ``__pycache__`` is skipped.
- Only comment tokens (``tokenize.COMMENT``) count, so a marker inside a string literal or docstring is ignored.
  A file that ``tokenize`` cannot read is listed under "unreadable" and not scanned.
- A marker is the upper-case word ``TODO``, ``FIXME`` or ``XXX`` with word boundaries: ``TODOS``, ``xxxx``,
  ``todo`` (a variable name) do not match.
- An item counts as mentioned in BACKLOG when docs/BACKLOG.md contains either
  (a) its short path: the path relative to the repository with a leading ``src/tradedesk/`` dropped
      (``tools/history.py``, ``scripts/sieve.py``), the form BACKLOG uses; any mention of the file counts, so a
      file named in BACKLOG for another reason also "covers" its TODOs (coarse on purpose); or
  (b) its text snippet: the comment text after the marker (leading ``:``/``-``/spaces stripped), first 40 characters,
      case-insensitive, when it is at least 10 characters long.

    uv run python scripts/todo_inventory.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/todo.md]

Exit code 0 always (an inventory, not a gate). Code is never changed.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

SCAN_DIRS = ("src", "scripts")
BACKLOG = "docs/BACKLOG.md"
SNIPPET_LEN = 40
SNIPPET_MIN = 10

_MARKER = re.compile(r"(?<![A-Za-z0-9_])(TODO|FIXME|XXX)(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class Item:
    path: str  # relative to the repository root, "/" separators
    line: int
    marker: str
    text: str  # the whole comment, "#" stripped

    @property
    def snippet(self) -> str:
        """Comment text after the marker, first SNIPPET_LEN characters."""
        m = _MARKER.search(self.text)
        rest = self.text[m.end() :] if m else self.text
        return rest.lstrip(" :-()").strip()[:SNIPPET_LEN]


def scan_source(path: str, source: str) -> list[Item]:
    """TODO/FIXME/XXX in comment tokens of one Python source. Raises tokenize.TokenError / SyntaxError
    on unreadable source. Pure."""
    out: list[Item] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        m = _MARKER.search(tok.string)
        if m:
            out.append(Item(path, tok.start[0], m.group(1), tok.string.lstrip("#").strip()))
    return out


def short_path(path: str) -> str:
    """Path as BACKLOG usually writes it: ``src/tradedesk/`` prefix dropped. Pure."""
    return path.removeprefix("src/tradedesk/")


def mentioned(item: Item, backlog: str) -> bool:
    """Rule (a) short path or (b) text snippet appears in BACKLOG (see module docstring). Pure."""
    if short_path(item.path) in backlog:
        return True
    snip = item.snippet
    return len(snip) >= SNIPPET_MIN and snip.lower() in backlog.lower()


@dataclass
class Report:
    files: int = 0
    items: list[Item] = field(default_factory=list)
    unmentioned: list[Item] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def inventory(sources: dict[str, str], backlog: str) -> Report:
    """``sources`` = {relative path: text}. No I/O itself."""
    r = Report()
    for path in sorted(sources):
        r.files += 1
        try:
            found = scan_source(path, sources[path])
        except (tokenize.TokenError, SyntaxError):
            r.unreadable.append(path)
            continue
        for it in found:
            r.items.append(it)
            if not mentioned(it, backlog):
                r.unmentioned.append(it)
    return r


def _fmt(it: Item) -> str:
    return f"- `{it.path}:{it.line}`: {it.text}"


def render(r: Report) -> str:
    lines = [
        f"# TODO / FIXME / XXX in {', '.join(SCAN_DIRS)} (*.py)",
        "",
        f"Files scanned: {r.files}; comments found: {len(r.items)}; not mentioned in {BACKLOG}: {len(r.unmentioned)}.",
        "",
        "## All",
        *([_fmt(it) for it in r.items] or ["- none"]),
        "",
        f"## Not mentioned in {BACKLOG}",
        *([_fmt(it) for it in r.unmentioned] or ["- none"]),
    ]
    if r.unreadable:
        lines += ["", "## Unreadable by tokenize (not scanned)", *[f"- `{p}`" for p in r.unreadable]]
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
    root = args.root.resolve()
    backlog_path = root / BACKLOG
    backlog = backlog_path.read_text(encoding="utf-8") if backlog_path.is_file() else ""
    text = render(inventory(load_sources(root), backlog))
    print(text, end="")
    if args.out:
        out = args.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
