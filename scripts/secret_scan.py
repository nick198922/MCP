"""Scan files tracked by git for secret-like strings (tokens, API keys, passwords).

Read-only, stdlib only. Rules:

- Files come from ``git ls-files`` in the repository root. Symbolic links are skipped (they may point outside the
  repository, e.g. ``docs/library/courses``), as are files larger than MAX_BYTES and binary files (a NUL byte in
  the first 8 KiB, or not valid UTF-8).
- Patterns (see PATTERNS):
  ``telegram``  a bot token ``<8-10 digits>:<35 chars of [A-Za-z0-9_-]>``;
  ``api_key``   ``api_key`` / ``api-secret`` / ``apikey`` ... followed by ``=`` or ``:`` and 16+ alphanumerics;
  ``bearer``    ``Bearer <20+ chars of [A-Za-z0-9._-]>``;
  ``assign``    ``token`` / ``secret`` / ``password`` / ``passwd`` followed by ``=`` or ``:`` and a value of 20+
                hex/base64 characters that contains both a letter and a digit (so ``password=settings.x`` or an
                identifier like ``telegram_bot_token_name`` is not a value); an EVM address (``0x`` + 40 hex,
                e.g. ``"token": "0x…"`` in market data) is public and not flagged.
  A bare hex string (a commit hash, a sha256) is never flagged: every pattern needs its keyword or shape.
  Known false positives left in on purpose: base58 addresses (Solana mints) after ``token:``, synthetic test keys.
- A match is skipped as a placeholder when its value contains ``xxx``, ``redacted``, ``test``, ``dummy``,
  ``example``, ``placeholder``, ``changeme``, ``your``, ``fake`` or ``sample`` (case-insensitive), or has fewer
  than 6 distinct characters (``aaaa...``, ``0000...``). Values like ``<TOKEN>`` or ``${VAR}`` never match the
  value character classes in the first place.
- Values are masked everywhere: only the first 4 characters are kept. The raw value is never stored or printed.

    uv run python scripts/secret_scan.py [--root .] [--out ~/.cache/tradedesk-agents/tmp/secret_scan.md]

Exit code 0 always (an inventory, not a gate). Nothing is changed or removed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 5_000_000
BINARY_PROBE = 8192
MASK_KEEP = 4
MIN_DISTINCT = 6

PATTERNS: dict[str, re.Pattern[str]] = {
    # Only a digit may not precede: ".../bot<token>/getMe" URLs put letters right before the token.
    "telegram": re.compile(r"(?<![0-9])(\d{8,10}:[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])"),
    "api_key": re.compile(r"(?i)api[_-]?(?:key|secret)[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9]{16,})"),
    "bearer": re.compile(r"Bearer\s+([A-Za-z0-9._-]{20,})"),
    "assign": re.compile(r"(?i)(?:token|secret|password|passwd)[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9+/_-]{20,}={0,2})"),
}

PLACEHOLDER_WORDS = ("xxx", "redacted", "test", "dummy", "example", "placeholder", "changeme", "your", "fake", "sample")


@dataclass(frozen=True)
class Finding:
    path: str  # relative to the repository root, "/" separators
    line: int
    kind: str  # key of PATTERNS
    masked: str  # first MASK_KEEP characters of the value, then "…"
    length: int  # length of the value


def mask(value: str) -> str:
    """First MASK_KEEP characters, then an ellipsis. Pure."""
    return value[:MASK_KEEP] + "…"


def is_placeholder(value: str) -> bool:
    """Obvious stand-in values (see module docstring). Pure."""
    low = value.lower()
    return any(w in low for w in PLACEHOLDER_WORDS) or len(set(value)) < MIN_DISTINCT


_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _looks_like_value(kind: str, value: str) -> bool:
    if kind == "assign":
        if _EVM_ADDRESS.fullmatch(value):  # a public contract/wallet address (``token=0x…``), not a key
            return False
        return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return True


def find_secrets(text: str, path: str) -> list[Finding]:
    """Secret-like values in one text, masked. Pure; a value is reported once per line even if several
    patterns match it (the first pattern in PATTERNS wins)."""
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen: set[tuple[int, int]] = set()
        for kind, rx in PATTERNS.items():
            for m in rx.finditer(line):
                value = m.group(1)
                span = m.span(1)
                if any(s < span[1] and span[0] < e for s, e in seen):
                    continue
                if is_placeholder(value) or not _looks_like_value(kind, value):
                    continue
                seen.add(span)
                out.append(Finding(path, lineno, kind, mask(value), len(value)))
    return out


def is_binary(data: bytes) -> bool:
    """NUL byte in the first BINARY_PROBE bytes, or not valid UTF-8. Pure."""
    if b"\0" in data[:BINARY_PROBE]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


@dataclass
class Report:
    files: int = 0
    scanned: int = 0
    binary: int = 0
    skipped: list[str] = field(default_factory=list)  # symlinks, too large, unreadable
    findings: list[Finding] = field(default_factory=list)


def scan(root: Path, paths: list[str]) -> Report:
    """Read each tracked path under ``root`` (no symlinks, nothing outside the root) and collect findings."""
    r = Report()
    root = root.resolve()
    for rel in sorted(paths):
        r.files += 1
        p = root / rel
        if p.is_symlink() or not p.is_file():
            r.skipped.append(f"{rel} (symlink or not a file)")
            continue
        try:
            if not p.resolve().is_relative_to(root):
                r.skipped.append(f"{rel} (outside the repository)")
                continue
            if p.stat().st_size > MAX_BYTES:
                r.skipped.append(f"{rel} (larger than {MAX_BYTES} bytes)")
                continue
            data = p.read_bytes()
        except OSError as e:
            r.skipped.append(f"{rel} (unreadable: {type(e).__name__})")
            continue
        if is_binary(data):
            r.binary += 1
            continue
        r.scanned += 1
        r.findings.extend(find_secrets(data.decode("utf-8"), rel))
    return r


def tracked_files(root: Path) -> list[str]:
    res = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def render(r: Report) -> str:
    lines = [
        "# Secret scan of git-tracked files",
        "",
        f"Tracked files: {r.files}; scanned as text: {r.scanned}; binary: {r.binary}; skipped: {len(r.skipped)}; "
        f"findings: {len(r.findings)}.",
        "",
        "## Findings (values masked: first 4 characters)",
        *([f"- `{f.path}:{f.line}` {f.kind}: `{f.masked}` ({f.length} chars)" for f in r.findings] or ["- none"]),
    ]
    if r.skipped:
        lines += ["", "## Skipped", *[f"- `{s}`" for s in r.skipped]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    text = render(scan(root, tracked_files(root)))
    print(text, end="")
    if args.out:
        out = args.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
