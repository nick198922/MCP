"""Guard over the metadata lines of docs/ideas/IDEA-*.md (no network).

The coordinator picks the next card by grepping «Статус:», «Пріоритет: власник» and «Порядок: N», and
scripts/results_board.py groups the board by the same lines. A card with a broken line (unknown status, a non-numeric
order, no status at all, two status lines) silently drops out of the queue; this test makes it visible.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import results_board as rb  # noqa: E402

IDEAS = ROOT / "docs" / "ideas"

# Card statuses in use: docs/ideas/README.md, docs/ideas/TEMPLATE.md and results_board.GROUPS
# (`benchmark` there is board-only, derived from a marked `confirmed` card).
STATUSES = {"proposed", "accepted", "forward", "testing", "confirmed", "inconclusive", "rejected", "built"}
NAME_RE = re.compile(r"IDEA-(\d{8})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
ORDER_RE = re.compile(r"Порядок:\s*\d+\s*")  # same as results_board.parse_card
OWNER_RE = re.compile(r"Пріоритет: власник\b")  # scripts/agents_loop.sh greps '^Пріоритет: власник'

# (card, start of the line) accepted on purpose. Each line reads as "not owner priority" on the board and in the
# coordinator's grep, which is what it means; rewriting it would change the card's content, not fix a queue key.
ALLOWED = {
    # Prose inside a section (line 81, not header metadata): when to run the step relative to other cards.
    ("IDEA-20261015-alt-downside-beta-regime-basket.md", "Пріоритет: після вже прийнятих карток у черзі"): "prose",
    # Explicit non-owner marker in the header (card written by Claude from course lessons, not an owner request).
    ("IDEA-20261021-open-levels-dowomo.md", "Пріоритет: звичайний"): "explicit non-owner",
    ("IDEA-20261021-smt-weaker-asset-amplitude.md", "Пріоритет: звичайний"): "explicit non-owner",
}


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return False
    return True


def card_problems(name: str, text: str) -> list[tuple[int, str, str]]:
    """(line number, line, what is wrong) for every bad metadata line of one card; line 0 is the card as a whole."""
    out = []
    m = NAME_RE.fullmatch(name)
    if not m or not _valid_date(m.group(1)):
        out.append((0, name, "file name is not IDEA-YYYYMMDD-slug.md"))
    lines = text.splitlines()
    status = [(i, s) for i, s in enumerate(lines, 1) if s.startswith("Статус:")]
    if not status:
        out.append((0, "", "no «Статус:» line"))
    else:
        i, s = status[0]
        w = rb.STATUS_RE.match(s)
        if not w or w.group(1) not in STATUSES:
            out.append((i, s, f"first word of «Статус:» is not one of {sorted(STATUSES)}"))
    out += [(i, s, "second «Статус:» line (the board and the coordinator read only the first)") for i, s in status[1:]]
    for i, s in enumerate(lines, 1):
        if s.startswith("Порядок:") and not ORDER_RE.fullmatch(s):
            out.append((i, s, "«Порядок:» is not a whole number"))
        if s.startswith("Пріоритет:") and not OWNER_RE.match(s):
            out.append((i, s, "«Пріоритет:» is not «власник»"))
    return out


def _allowed(name: str, line: str) -> bool:
    return any(card == name and line.startswith(prefix) for card, prefix in ALLOWED)


def _all_problems() -> list[tuple[str, int, str, str]]:
    # Every .md but the two non-cards, so a misnamed card (outside the IDEA-*.md glob) is reported, not skipped.
    cards = sorted(p for p in IDEAS.glob("*.md") if p.name not in ("README.md", "TEMPLATE.md"))
    assert len(cards) > 100, "docs/ideas/IDEA-*.md not found"
    return [(p.name, i, s, why) for p in cards for i, s, why in card_problems(p.name, p.read_text(encoding="utf-8"))]


def test_idea_card_metadata_is_well_formed():
    bad = [f"{n}:{i}: {why}: {s[:100]}" for n, i, s, why in _all_problems() if not _allowed(n, s)]
    assert not bad, "fix the card's metadata line (or allowlist it in ALLOWED with a reason):\n" + "\n".join(bad)


def test_allowlist_has_no_stale_entries():
    hit = {(n, prefix) for n, _, s, _ in _all_problems() for card, prefix in ALLOWED if card == n and s.startswith(prefix)}
    assert set(ALLOWED) == hit, f"stale ALLOWED entries: {sorted(set(ALLOWED) - hit)}"


def test_status_set_matches_results_board_groups():
    board = {s for _, sts in rb.GROUPS for s in sts} - {"benchmark"}
    assert board == STATUSES


def test_guard_catches_broken_metadata():
    good = "# IDEA-20261105-x: X\n\nСтатус: testing, призупинено\nПріоритет: власник (шортові сетапи)\nПорядок: 3\n"
    assert card_problems("IDEA-20261105-x.md", good) == []

    def why(name: str, text: str) -> list[str]:
        return [w for _, _, w in card_problems(name, text)]

    assert why("IDEA-20261105-x.md", "# X\n\nТекст без статусу\n") == ["no «Статус:» line"]
    assert "is not one of" in why("IDEA-20261105-x.md", "Статус: `rejected`\n")[0]
    assert "is not one of" in why("IDEA-20261105-x.md", "Статус: done\n")[0]
    assert "second" in why("IDEA-20261105-x.md", "Статус: accepted\nСтатус: rejected\n")[0]
    assert why("IDEA-20261105-x.md", "Статус: accepted\nПорядок: 3a\n") == ["«Порядок:» is not a whole number"]
    assert why("IDEA-20261105-x.md", "Статус: accepted\nПріоритет: шортові сетапи (власник)\n") == ["«Пріоритет:» is not «власник»"]
    for name in ("IDEA-2026110-x.md", "IDEA-20261340-x.md", "IDEA-20261105-Bad_slug.md", "idea-20261105-x.md"):
        assert why(name, "Статус: accepted\n") == ["file name is not IDEA-YYYYMMDD-slug.md"], name
