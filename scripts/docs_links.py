"""Check relative markdown links in docs/**/*.md and README.md: missing files and missing #anchors.

Read-only (stdlib only). Prints one line per broken link, ``path:line: kind target``; ``--out FILE`` also writes the
report there. Exit code 1 when anything is broken, 0 otherwise.

Skipped: external links (``https:``, ``mailto:``, any ``scheme:``, ``//host``), anything under ``docs/library/courses``
(a symlink to private material that is not in git), links inside code spans and fenced code blocks. Anchors follow
GitHub's slugs (lower case, punctuation dropped, each space -> ``-``, Cyrillic kept, duplicates ``-1``, ``-2``) plus
explicit ``<a id/name="...">``; they are checked only in ``.md`` targets and compared case-insensitively.

    uv run python scripts/docs_links.py [--root .] [--out /tmp/docs_links.txt]
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

SKIP_PREFIXES = ("docs/library/courses",)

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_LINK = re.compile(
    r"(?<!\\)!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]"  # [text], one level of nested brackets allowed
    r"\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"  # (target "optional title")
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_HTML_ID = re.compile(r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class Broken:
    source: str  # repo-relative posix path of the file with the link
    line: int
    kind: str  # "file" | "anchor" | "outside"
    target: str  # the link target as written

    def render(self) -> str:
        return f"{self.source}:{self.line}: broken {self.kind} {self.target}"


def _code_free_lines(text: str, blank_spans: bool = True) -> list[tuple[int, str]]:
    """(1-based line number, line) outside fenced code blocks, inline code spans blanked when ``blank_spans``."""
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
            continue
        out.append((i, _CODE_SPAN.sub(lambda s: " " * len(s.group(0)), line) if blank_spans else line))
    return out


def extract_links(text: str) -> list[tuple[int, str]]:
    """Inline links and images ``[text](target)`` outside code: (line number, target with <> stripped)."""
    out: list[tuple[int, str]] = []
    for n, line in _code_free_lines(text):
        for m in _LINK.finditer(line):
            target = m.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if target:
                out.append((n, target))
    return out


def _heading_text(raw: str) -> str:
    """Approximate rendered text of a heading: code ticks, links, images, html tags and emphasis removed."""
    s = re.sub(r"[ \t]+#+$", "", raw.strip())  # closing ATX sequence
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)  # [text](url) -> text
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("`", "").replace("*", "")


def github_slug(text: str) -> str:
    """GitHub heading slug (github-slugger): lower case, drop everything but letters, digits, ``_``, ``-`` and
    spaces, then each space -> ``-``. Cyrillic letters are kept."""
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def heading_anchors(text: str) -> set[str]:
    """Anchors of a markdown file: GitHub slugs of ATX and setext headings (with -1, -2 for duplicates) and
    explicit ``<a id="...">`` / ``<a name="...">``."""
    seen: dict[str, int] = {}
    out: set[str] = set()

    def add(heading: str) -> None:
        base = github_slug(_heading_text(heading))
        slug, n = base, seen.get(base, 0)
        while slug in out:
            n += 1
            slug = f"{base}-{n}"
        seen[base] = n
        out.add(slug)

    lines = _code_free_lines(text, blank_spans=False)  # GitHub keeps code-span text in heading slugs
    prev: tuple[int, str] | None = None
    for n, line in lines:
        out.update(m.group(1) for m in _HTML_ID.finditer(line))
        m = _ATX.match(line)
        if m:
            add(m.group(2) or "")
        elif (
            prev is not None
            and prev[0] == n - 1
            and re.match(r"^ {0,3}(=+|-+)[ \t]*$", line)
            and prev[1].strip()
            and not re.match(r"^ {0,3}([-*+>|#]|\d+[.)])", prev[1])
        ):
            add(prev[1])
        prev = (n, line)
    return out


def resolve(source: str, target: str) -> tuple[str | None, str | None] | None:
    """(repo-relative path or None for a same-file anchor, anchor or None); None when the link is skipped
    (external URL). The path may start with ``..`` when it leaves the repository."""
    if _SCHEME.match(target) or target.startswith("//"):
        return None
    path, _, anchor = target.partition("#")
    path = unquote(path.split("?", 1)[0])
    anchor_out = unquote(anchor) if anchor else None
    if not path:
        return None, anchor_out
    base = "" if path.startswith("/") else posixpath.dirname(source)
    joined = posixpath.normpath(posixpath.join(base, path.lstrip("/")))
    return joined, anchor_out


def _skipped(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in SKIP_PREFIXES)


def check(sources: dict[str, str], exists, read_md) -> list[Broken]:
    """Broken links in ``sources`` (repo-relative path -> text). ``exists(path)`` says whether a repo-relative path
    exists; ``read_md(path)`` returns a markdown target's text (only called for existing ``.md`` files with an anchor).
    No I/O of its own."""
    anchors: dict[str, set[str]] = {}

    def anchors_of(path: str, text: str | None = None) -> set[str]:
        if path not in anchors:
            anchors[path] = {a.lower() for a in heading_anchors(read_md(path) if text is None else text)}
        return anchors[path]

    out: list[Broken] = []
    for source in sorted(sources):
        text = sources[source]
        for line, target in extract_links(text):
            r = resolve(source, target)
            if r is None:
                continue
            path, anchor = r
            if path is None:
                path = source
            if path == ".." or path.startswith("../"):
                out.append(Broken(source, line, "outside", target))
                continue
            if _skipped(path):
                continue
            if path != source and not exists(path):
                out.append(Broken(source, line, "file", target))
                continue
            if anchor and path.endswith(".md"):
                text_md = text if path == source else None
                if anchor.lower() not in anchors_of(path, text_md):
                    out.append(Broken(source, line, "anchor", target))
    return out


def collect_sources(root: Path) -> dict[str, str]:
    """docs/**/*.md (symlinked directories such as docs/library/courses are not entered) and README.md."""
    files: list[Path] = []
    docs = root / "docs"
    for dirpath, dirnames, filenames in os.walk(docs):  # followlinks=False
        dirnames.sort()
        files += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".md")]
    if (root / "README.md").is_file():
        files.append(root / "README.md")
    out: dict[str, str] = {}
    for f in files:
        rel = f.relative_to(root).as_posix()
        if not _skipped(rel):
            out[rel] = f.read_text(encoding="utf-8", errors="replace")
    return out


def check_tree(root: Path) -> tuple[list[Broken], int]:
    """(broken links, number of files checked) for the repository at ``root``."""
    root = root.resolve()
    sources = collect_sources(root)
    broken = check(
        sources,
        exists=lambda p: (root / p).exists(),
        read_md=lambda p: (root / p).read_text(encoding="utf-8", errors="replace"),
    )
    return broken, len(sources)


def render(broken: list[Broken], n_sources: int) -> str:
    lines = [b.render() for b in broken]
    files = len({b.source for b in broken})
    lines.append(f"{len(broken)} broken link(s) in {files} file(s); {n_sources} file(s) checked")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    a = ap.parse_args(argv)
    broken, n_sources = check_tree(a.root)
    text = render(broken, n_sources)
    print(text, end="")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
