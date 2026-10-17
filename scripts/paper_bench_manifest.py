"""sha256 manifest of the raw cache behind docs/paper/bench.json (review 2026-10-17, finding 3).

    uv run python scripts/paper_bench_manifest.py [--cache-dir ~/.cache/tradedesk-agents/paper_carry_bench]
        [--out docs/paper/bench-data.sha256]

Writes `sha256sum`-compatible lines (`<hex>  <relative path>`) for every *.json in the cache, sorted, so a later
re-download can be compared file by file (`cd <cache> && sha256sum -c <manifest>`). Logs are skipped. Read-only on the
cache; no network.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/paper_carry_bench")
    ap.add_argument("--out", type=Path, default=Path("docs/paper/bench-data.sha256"))
    a = ap.parse_args()
    files = sorted(p for p in a.cache_dir.rglob("*.json") if p.is_file())
    total = 0
    lines = []
    for p in files:
        total += p.stat().st_size
        lines.append(f"{sha256_file(p)}  {p.relative_to(a.cache_dir).as_posix()}")
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(files)} files, {total / 1e6:.1f} MB -> {a.out}")


if __name__ == "__main__":
    main()
