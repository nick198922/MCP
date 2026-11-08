"""Check repository paths written in backticks in the docs the 30.11 review follows: report those not in git.

Read-only (stdlib only). Scans ``docs/CHECKPOINT-2026-11-30.md``, ``docs/HANDOFF.md``, ``docs/AGENTS.md`` and idea
cards ``docs/ideas/IDEA-*.md`` whose ``Статус:`` line begins with ``accepted``, ``testing`` or ``forward``. In inline
code spans and fenced code blocks it looks for repo paths (``scripts/``, ``src/``, ``tests/``, ``docs/``, ``deploy/``
ending in ``.py``, ``.md``, ``.sh``, ``.env``, ``.json`` or ``.csv``) and prints, one per line,
``path:line: missing target`` for each one that ``git ls-files`` does not list. ``--out FILE`` also writes the report.
Exit code 1 when anything is missing, 0 otherwise.

Skipped: paths under ``~/`` or ``/home/`` (or any absolute or ``../`` prefix), templates (``*``, ``<...>``, ``{...}``,
``YYYY`` / ``РРРР``, ``...``, dates like ``2026-11-0X``), cache directories (``.cache``, ``cache``, ``__pycache__`` segments) and local-only files that are
not in git by design (``docs/library/courses``, ``docs/library/img``, ``docs/RESULTS.md``).

    uv run python scripts/docs_paths.py [--root .] [--out /tmp/docs_paths.txt]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FIXED_SOURCES = ("docs/CHECKPOINT-2026-11-30.md", "docs/HANDOFF.md", "docs/AGENTS.md")
CARD_STATUSES = ("accepted", "testing", "forward")

# Not in git by design (same list as LOCAL_ONLY in tests/test_docs_links.py).
LOCAL_ONLY = ("docs/library/courses", "docs/library/img", "docs/RESULTS.md")
CACHE_SEGMENTS = {".cache", "cache", "__pycache__"}
# Template markers: glob, <placeholder>, {placeholder}, ellipsis, YYYY / РРРР, dates such as 2026-11-0X or 2026-11-XX.
_TEMPLATE = re.compile(r"[*<>{}]|…|\.\.\.|YYYY|РРРР|\d{4}-\d{2}-\d?[XxХх]")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)((?:(?!\1).)+?)\1")
# A repo path: not glued to a preceding path character (so ``~/x/docs/a.md``, ``/home/.../scripts/a.py`` and
# ``../tests/a.py`` do not match), one of the top-level directories, then anything but whitespace, quotes,
# brackets and separators, ending in a known extension that is not followed by more path characters.
_PATH = re.compile(r"(?<![\w./~-])((?:scripts|src|tests|docs|deploy)/[^\s`'\"()\[\],;:|=]*?\.(?:py|md|sh|env|json|csv))(?![\w/.-])")
_STATUS = re.compile(r"^\s*(?:[-*]\s+)?\**Статус\**\s*:\s*\**\s*(\S.*)$")


@dataclass(frozen=True)
class Missing:
    source: str  # repo-relative posix path of the scanned document
    line: int
    target: str  # the path as written

    def render(self) -> str:
        return f"{self.source}:{self.line}: missing {self.target}"


def _code_fragments(text: str) -> list[tuple[int, str]]:
    """(1-based line number, text) of every inline code span and every line inside a fenced code block."""
    out: list[tuple[int, str]] = []
    fence: str | None = None
    for i, line in enumerate(text.splitlines(), 1):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            continue
        if fence is not None:
            close = m.group(1) if m else ""
            if close[:1] == fence[0] and len(close) >= len(fence) and not line.strip()[len(close) :]:
                fence = None
            else:
                out.append((i, line))
            continue
        out += [(i, s.group(2)) for s in _CODE_SPAN.finditer(line)]
    return out


def is_skipped(path: str) -> bool:
    """Templates, cache directories and local-only files are not checked against git."""
    if _TEMPLATE.search(path):
        return True
    if any(seg in CACHE_SEGMENTS for seg in path.split("/")[:-1]):
        return True
    return any(path == p or path.startswith(p + "/") for p in LOCAL_ONLY)


def extract_paths(text: str) -> list[tuple[int, str]]:
    """Checkable repo paths written in backticks: (line number, path). Pure; skipped paths are left out."""
    out: list[tuple[int, str]] = []
    for n, frag in _code_fragments(text):
        for m in _PATH.finditer(frag):
            if not is_skipped(m.group(1)):
                out.append((n, m.group(1)))
    return out


def card_status(text: str) -> str | None:
    """Value of the first ``Статус:`` line of an idea card, or None."""
    for line in text.splitlines():
        m = _STATUS.match(line)
        if m:
            return m.group(1).strip()
    return None


def is_active_card(text: str) -> bool:
    status = (card_status(text) or "").lower()
    return status.startswith(CARD_STATUSES)


def check(sources: dict[str, str], tracked: set[str]) -> tuple[list[Missing], int]:
    """(paths not in ``tracked``, number of path occurrences checked) for ``sources`` (repo path -> text). No I/O."""
    out: list[Missing] = []
    checked = 0
    for source in sorted(sources):
        for line, path in extract_paths(sources[source]):
            checked += 1
            if path not in tracked:
                out.append(Missing(source, line, path))
    return out, checked


def tracked_files(root: Path) -> set[str]:
    """Files listed by ``git ls-files`` (repo-relative posix paths)."""
    res = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return {p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p}


def collect_sources(root: Path, tracked: set[str]) -> dict[str, str]:
    """The fixed documents plus active idea cards (accepted / testing / forward)."""
    out: dict[str, str] = {}
    for rel in FIXED_SOURCES:
        f = root / rel
        if f.is_file():
            out[rel] = f.read_text(encoding="utf-8", errors="replace")
    for rel in sorted(tracked):
        if rel.startswith("docs/ideas/IDEA-") and rel.endswith(".md") and (root / rel).is_file():
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            if is_active_card(text):
                out[rel] = text
    return out


def check_tree(root: Path) -> tuple[list[Missing], int, int]:
    """(missing paths, path occurrences checked, documents scanned) for the repository at ``root``."""
    root = root.resolve()
    tracked = tracked_files(root)
    sources = collect_sources(root, tracked)
    missing, checked = check(sources, tracked)
    return missing, checked, len(sources)


def render(missing: list[Missing], checked: int, n_sources: int) -> str:
    lines = [m.render() for m in missing]
    files = len({m.source for m in missing})
    lines.append(f"{len(missing)} missing path(s) in {files} file(s); {checked} path(s) checked in {n_sources} file(s)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    a = ap.parse_args(argv)
    missing, checked, n_sources = check_tree(a.root)
    text = render(missing, checked, n_sources)
    print(text, end="")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
