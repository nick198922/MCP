#!/usr/bin/env python3
"""For the agents in headless runs, where `rm` is not allowed: delete throwaway files they made themselves.

    scripts/agents_rm.py PATH...

Deletes a file (or an empty directory) only if it is inside this clone and git does not track it, or inside the
agents' cache directory (TRADEDESK_AGENTS_CACHE_DIR, default ~/.cache/tradedesk-agents). Refuses tracked files (a
deletion of project code goes through a reviewed change), anything under .git, symbolic links, non-empty directories
and every path outside those two places. Prints one line per path; exits 1 if any was refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _cache_dir() -> Path:
    raw = os.environ.get("TRADEDESK_AGENTS_CACHE_DIR") or str(Path.home() / ".cache" / "tradedesk-agents")
    return Path(raw).expanduser().resolve()


def _repo_root() -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out.stdout.strip()).resolve()


def _tracked(root: Path, path: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    res = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel], capture_output=True)
    return res.returncode == 0


def check(arg: str, root: Path | None, cache: Path) -> tuple[Path | None, str]:
    """Return (path to delete, '') or (None, reason it is refused)."""
    given = Path(arg).expanduser()
    if given.is_symlink():
        return None, "symbolic link"
    if not given.exists():
        return None, "no such file"
    path = given.resolve()
    if path.is_dir() and any(path.iterdir()):
        return None, "directory is not empty"
    if not (path.is_file() or path.is_dir()):
        return None, "not a regular file"
    if path.is_relative_to(cache) and path != cache:
        return path, ""
    if root is not None and path.is_relative_to(root) and path != root:
        if path.is_relative_to(root / ".git"):
            return None, "inside .git"
        if path.is_file() and _tracked(root, path):
            return None, "tracked by git (leave it, or name it in the run summary for the owner)"
        return path, ""
    return None, "outside this clone and the agents' cache"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 1
    root, cache, rc = _repo_root(), _cache_dir(), 0
    for arg in argv:
        path, reason = check(arg, root, cache)
        if path is None:
            print(f"refused: {arg}: {reason}")
            rc = 1
            continue
        try:
            path.rmdir() if path.is_dir() else path.unlink()
        except OSError as exc:
            print(f"failed: {arg}: {exc.strerror}")
            rc = 1
        else:
            print(f"deleted: {arg}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
