"""Repair the agents' run-file links that are one directory short (run by the launcher before lint and tests).

A run file lives in docs/agents/runs/, two levels below docs/, yet the agents keep writing links as if it were one
level below (`../reviews/x.md` instead of `../../reviews/x.md`), and tests/test_docs_links.py fails the delivery: about
15 steps from 2026-11-04 needed an extra run for it (doctor, 2026-11-06). A relative link whose target does not exist
but exists with one more `../` is rewritten; every other link is left as it is.

    python3 scripts/fix_run_links.py [file ...]   # default: every docs/agents/runs/*.md changed against origin/main

Prints the repaired files on stdout (one per line), the details on stderr.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"(\]\(\s*<?)(\.\./[^)\s>#]+)")


def fix_text(text: str, here: Path) -> tuple[str, int]:
    """The text with short links repaired, and how many were; ``here`` is the file's directory."""
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        target = m.group(2)
        if (here / target).exists() or not (here / ("../" + target)).exists():
            return m.group(0)
        count += 1
        return m.group(1) + "../" + target

    return LINK.sub(repl, text), count


def changed_run_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "docs/agents/runs"], capture_output=True, text=True).stdout
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "origin/main...HEAD", "--", "docs/agents/runs"],
        capture_output=True,
        text=True,
    ).stdout
    names = {line[3:].strip() for line in out.splitlines()} | set(diff.split())
    return [root / n for n in sorted(names) if n.endswith(".md") and (root / n).is_file()]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path.cwd()
    files = [Path(a) for a in args] if args else changed_run_files(root)
    for f in files:
        text = f.read_text(encoding="utf-8")
        new, n = fix_text(text, f.resolve().parent)
        if n:
            f.write_text(new, encoding="utf-8")
            print(f"{f}: {n} link(s) one directory short, repaired", file=sys.stderr)
            print(f)  # stdout: the repaired files only, for `git add`
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
