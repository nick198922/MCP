"""Check that every file in tests/fixtures/ carries a provenance mark (date and source).

Read-only, stdlib only. CLAUDE.md asks that new samples are stored "with a date and source mark". A file counts as marked
when at least one of these holds (``marks``):

- ``name-date``: its name (path relative to tests/fixtures/) contains a calendar date ``YYYY-MM-DD``;
- ``field``: it is a JSON object with a top-level key ``source`` or ``captured*`` (a leading ``_`` is ignored:
  ``_source``, ``captured_at``, ``_captured_utc``); nested keys do not count, they are usually part of the payload;
- ``comment``: a text file has a comment line ``# source: ...`` / ``// captured: ...`` / ``<!-- source: ... -->``;
- ``readme``: its path relative to tests/fixtures/ is mentioned in ``tests/fixtures/README.md``.

Binary files (zip, anything that is not UTF-8 or contains NUL) are judged by name and README only. Fixture contents
are never changed.

    uv run python scripts/fixtures_check.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/fixtures.txt]

Exit code 1 when a file has no mark; 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES_DIR = "tests/fixtures"
README = "README.md"
MARKS = ("name-date", "field", "comment", "readme")

_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?!\d)")
_COMMENT = re.compile(r"^\s*(?:#|//|;|--|/\*|<!--)\s*_?(?:source|captured\w*)\s*[:=]", re.IGNORECASE | re.MULTILINE)


def name_has_date(rel: str) -> bool:
    """A calendar date ``YYYY-MM-DD`` anywhere in the relative path. Pure."""
    return _DATE.search(rel) is not None


def _is_provenance_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lstrip("_").lower()
    return k == "source" or k.startswith("captured")


def has_field(text: str) -> bool:
    """A JSON object whose top-level keys include ``source`` / ``captured*`` (leading ``_`` ignored). Pure."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and any(_is_provenance_key(k) for k in data)


def has_comment(text: str) -> bool:
    """A comment line starting with ``source:`` / ``captured...:`` (``#``, ``//``, ``;``, ``--``, ``/*``, ``<!--``). Pure."""
    return _COMMENT.search(text) is not None


def readme_mentions(rel: str, readme: str) -> bool:
    """``rel`` (or ``tests/fixtures/<rel>``) appears in the README as a whole path, not as part of a longer name. Pure."""
    pattern = r"(?<![\w./-])(?:" + re.escape(FIXTURES_DIR + "/") + r")?" + re.escape(rel) + r"(?![\w/-]|\.\w)"
    return re.search(pattern, readme) is not None


def marks(rel: str, text: str | None, readme: str) -> list[str]:
    """Provenance marks of one fixture. ``rel`` is the path relative to tests/fixtures/ (``/`` separators), ``text`` its
    UTF-8 content or None for a binary file, ``readme`` the text of tests/fixtures/README.md ("" when absent). Pure."""
    out: list[str] = []
    if name_has_date(rel):
        out.append("name-date")
    if text is not None:
        if has_field(text):
            out.append("field")
        if has_comment(text):
            out.append("comment")
    if readme and readme_mentions(rel, readme):
        out.append("readme")
    return out


@dataclass
class Report:
    files: int = 0
    by_mark: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MARKS, 0))
    binary: int = 0
    unmarked: list[str] = field(default_factory=list)
    readme_only: list[str] = field(default_factory=list)  # informational: marked only by the README

    @property
    def failed(self) -> bool:
        return bool(self.unmarked)


def check(fixtures: dict[str, str | None], readme: str) -> Report:
    """``fixtures`` = {relative path: text or None for binary}. No I/O itself."""
    r = Report()
    for rel in sorted(fixtures):
        text = fixtures[rel]
        r.files += 1
        if text is None:
            r.binary += 1
        found = marks(rel, text, readme)
        for m in found:
            r.by_mark[m] += 1
        if not found:
            r.unmarked.append(rel)
        elif found == ["readme"]:
            r.readme_only.append(rel)
    return r


def _section(title: str, items: list[str]) -> list[str]:
    return ["", f"{title} ({len(items)}):"] + ([f"  {n}" for n in items] or ["  -"])


def render(r: Report) -> str:
    counts = ", ".join(f"{m} {r.by_mark[m]}" for m in MARKS)
    lines = [f"{FIXTURES_DIR}: {r.files} files ({r.binary} binary); marks: {counts}"]
    lines += _section("no provenance mark", r.unmarked)
    lines += _section("marked only by README.md (information only)", r.readme_only)
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str | None:
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def load_fixtures(directory: Path) -> tuple[dict[str, str | None], str]:
    """({relative path: text or None}, README text). The README itself is not a fixture."""
    readme_path = directory / README
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    files = {
        p.relative_to(directory).as_posix(): _read(p)
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p != readme_path and "__pycache__" not in p.parts
    }
    return files, readme


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    files, readme = load_fixtures(args.root.resolve() / FIXTURES_DIR)
    report = check(files, readme)
    text = render(report)
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
