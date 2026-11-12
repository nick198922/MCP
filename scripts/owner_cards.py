"""The owner's priority cards that still have open checklist items, in the coordinator's order (step p of
.claude/nightly.md: `Порядок: N` ascending, then cards without it, oldest first). The launcher appends this list to the
coordinator's prompt (the newest card first among cards with the same `Порядок`: the owner's latest decision leads;
at most LIMIT cards). Its own Grep over ~150 cards was cut off on 12.11 and it reported «nothing to do» while the
owner's profit-search card had open items.

    python3 scripts/owner_cards.py [docs/ideas]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ORDER = re.compile(r"^Порядок:\s*(\d+)", re.M)
STATUS = re.compile(r"^Статус:\s*(\S+)", re.M)
OPEN_ITEM = re.compile(r"^\s*- \[ \] (.+)$", re.M)
DONE_STATUSES = {"rejected", "confirmed", "built", "inconclusive"}
LIMIT = 12


def owner_cards(ideas: Path) -> list[tuple[str, int | None, str, int, str]]:
    """(file name, order, status, open items, first open item) of each owner card with open items, in order."""
    out = []
    for f in sorted(ideas.glob("IDEA-*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"^Пріоритет:\s*власник", text, re.M):
            continue
        status = (STATUS.search(text) or [None, "?"])[1].rstrip(" (—,")
        items = OPEN_ITEM.findall(text.split("## Прогрес", 1)[1].split("\n## ", 1)[0]) if "## Прогрес" in text else []
        if status in DONE_STATUSES or not items:
            continue
        m = ORDER.search(text)
        out.append((f.name, int(m[1]) if m else None, status, len(items), items[0]))
    out.sort(key=lambda c: c[0], reverse=True)  # newest first within the same order (names start with the date)
    with_order = sorted((c for c in out if c[1] is not None), key=lambda c: c[1])  # stable: newest first per order
    without = sorted((c for c in out if c[1] is None), key=lambda c: c[0])  # step p: cards without an order, oldest first
    return with_order + without


def note(cards: list[tuple[str, int | None, str, int, str]], width: int = 220) -> str:
    if not cards:
        return ""
    lines = [
        "OWNER CARDS with open items (step p, in this order; open a card in full and check its items one by one before "
        "calling it blocked — an item marked as a repeating batch is never blocked by an earlier batch):"
    ]
    for name, order, status, n, first in cards[:LIMIT]:
        first = re.sub(r"\s+", " ", first)
        first = first if len(first) <= width else first[: width - 1] + "…"
        lines.append(f"- {name} (Порядок {order if order is not None else '—'}, {status}, open {n}): {first}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print(note(owner_cards(Path(args[0] if args else "docs/ideas"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
