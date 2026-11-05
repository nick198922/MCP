"""Guard for relative markdown links (item 58 of docs/ideas/IDEA-20261102-standing-improvements.md).

Every relative link `[..](path)` / `[..](path#anchor)` / `![..](path)` in docs/**/*.md, README.md and CHANGELOG.md
must point to an existing file or directory. No network. Anchors are not checked; links inside fenced code
blocks and inline code spans are ignored; external links (scheme:, //host) and pure anchors (#x) are skipped.
Moving a card or report to docs/history/ silently breaks links to it: this test makes that loud.
"""

import os
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# Local-only paths: not in git (or git keeps only part of them), so a missing target there is not an error,
# and markdown files under them are not scanned (they differ between this server and CI).
LOCAL_ONLY = (
    "docs/library/courses",  # symlink to ~/private/library/courses, gitignored (paid courses)
    "docs/library/img",  # video frames and sheets stay local; git has only some of them
    "docs/RESULTS.md",  # gitignored, generated on the server
)

# Broken links allowed to stay, as (source file, link target) relative to the repo root.
# Add an entry only with a comment why the link cannot be fixed; a stale entry fails the test.
ALLOWLIST: set[tuple[str, str]] = set()

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
LINK = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]+)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _local_only(rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in LOCAL_ONLY)


def strip_code(text: str) -> str:
    """Drop fenced code blocks (``` or ~~~) and inline code spans; keep line structure."""
    out, fence = [], None
    for line in text.splitlines():
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            out.append("")
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip()[len(m.group(1)) :]:
                fence = None
            out.append("")
            continue
        out.append(INLINE_CODE.sub("", line))
    return "\n".join(out)


def relative_targets(text: str) -> list[str]:
    """Relative link targets without anchor/query; external links and pure anchors dropped."""
    targets = []
    for raw in LINK.findall(strip_code(text)):
        target = raw[1:-1].strip() if raw.startswith("<") else raw
        if not target or target.startswith(("#", "//")) or SCHEME.match(target):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if target:
            targets.append(unquote(target))
    return targets


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
    for dirpath, dirnames, filenames in os.walk(DOCS):  # followlinks=False: the courses symlink is not entered
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        dirnames[:] = sorted(d for d in dirnames if not _local_only(f"{rel_dir}/{d}"))
        files += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".md") and not _local_only(f"{rel_dir}/{f}")]
    return [f for f in files if f.is_file()]


def resolve(source: Path, target: str) -> Path:
    base = ROOT if target.startswith("/") else source.parent
    return Path(os.path.normpath(base / target.lstrip("/")))


def broken_links() -> tuple[int, set[tuple[str, str]]]:
    checked, broken = 0, set()
    for src in markdown_files():
        for target in relative_targets(src.read_text(encoding="utf-8")):
            checked += 1
            path = resolve(src, target)
            if path.exists():
                continue
            try:
                rel = path.relative_to(ROOT).as_posix()
            except ValueError:
                rel = None
            if rel is not None and _local_only(rel):
                continue
            broken.add((src.relative_to(ROOT).as_posix(), target))
    return checked, broken


def test_parser_ignores_code_external_and_anchors():
    text = (
        "See [card](../ideas/IDEA-x.md#h) and ![img](img/a.png 'title').\n"
        "Ext [x](https://example.com/a.md), [m](mailto:a@b.c), [a](#local), [w](<with space.md>).\n"
        "Inline `[no](skip-inline.md)` here.\n"
        "```bash\n[no](skip-fenced.md)\n```\n"
        "~~~\n[no](skip-tilde.md)\n~~~\n"
        "[enc](a%20b.md?x=1)\n"
    )
    assert relative_targets(text) == ["../ideas/IDEA-x.md", "img/a.png", "with space.md", "a b.md"]


def test_markdown_files_are_found():
    files = {f.relative_to(ROOT).as_posix() for f in markdown_files()}
    assert {"README.md", "docs/HANDOFF.md", "docs/BACKLOG.md"} <= files
    assert not any(_local_only(f) for f in files)
    # Item 107: the docs the owner follows at the 30.11 review (registrations, reviews, changes, verification
    # reports) must stay scanned; a rename of the checkpoint file or a LOCAL_ONLY entry over docs/ideas would
    # otherwise drop them from the guard silently.
    assert "docs/CHECKPOINT-2026-11-30.md" in files
    cards = {f for f in files if f.startswith("docs/ideas/IDEA-")}
    assert len(cards) > 100 and cards == {p.relative_to(ROOT).as_posix() for p in (DOCS / "ideas").glob("IDEA-*.md")}


def test_relative_links_point_to_existing_files():
    checked, broken = broken_links()
    assert checked > 100, "the link scanner found suspiciously few links"
    new = sorted(broken - ALLOWLIST)
    assert new == [], "broken relative links (source, target); fix the path or add to ALLOWLIST with a reason:\n" + "\n".join(
        f"  {s} -> {t}" for s, t in new
    )
    stale = sorted(ALLOWLIST - broken)
    assert stale == [], f"these ALLOWLIST entries are no longer broken, remove them: {stale}"
