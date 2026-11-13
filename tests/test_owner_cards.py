"""scripts/owner_cards.py: the owner's cards with open items, listed for the coordinator (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import owner_cards as oc  # noqa: E402

CARD = "# IDEA-{d}-x: t\n\nСтатус: {status} (довгий опис)\nПріоритет: власник\n{order}\n\n## Прогрес\n{items}\n\n## Механізм\n- [ ] not an item\n"


def write(d, name, status="accepted", order="Порядок: 0", items="- [ ] 1. перший\n- [x] 2. зроблено", owner=True):
    text = CARD.format(d=name[:8], status=status, order=order, items=items)
    (d / f"IDEA-{name}.md").write_text(text if owner else text.replace("Пріоритет: власник\n", ""), encoding="utf-8")


def test_lists_open_owner_cards_newest_first_per_order_then_unordered_oldest_first(tmp_path):
    write(tmp_path, "20261024-old")
    write(tmp_path, "20261111-new")
    write(tmp_path, "20261102-one", order="Порядок: 1")
    write(tmp_path, "20261020-free-a", order="")
    write(tmp_path, "20261021-free-b", order="")
    write(tmp_path, "20261101-done", items="- [x] 1. все")
    write(tmp_path, "20261101-closed", status="rejected")
    write(tmp_path, "20261101-not-owner", owner=False)
    names = [c[0] for c in oc.owner_cards(tmp_path)]
    assert names == [
        "IDEA-20261111-new.md",
        "IDEA-20261024-old.md",
        "IDEA-20261102-one.md",
        "IDEA-20261020-free-a.md",
        "IDEA-20261021-free-b.md",
    ]
    first = oc.owner_cards(tmp_path)[0]
    assert first[2] == "accepted" and first[3] == 1 and first[4] == "1. перший"  # items under «Механізм» do not count


def test_note_is_short_and_empty_without_cards(tmp_path):
    assert oc.note([]) == ""
    for i in range(20):
        write(tmp_path, f"202611{i:02d}-c{i}", items="- [ ] " + "x" * 500)
    text = oc.note(oc.owner_cards(tmp_path))
    assert text.startswith("OWNER CARDS") and text.count("\n- ") == oc.LIMIT
    assert all(len(line) < 400 for line in text.splitlines())


def test_refill_note_names_the_standing_card_even_with_no_open_items(tmp_path):
    refill = "\n\n## Як поповнювати\n- BACKLOG\n"
    write(tmp_path, "20261102-standing", items="- [x] 1. зроблено")
    with open(tmp_path / "IDEA-20261102-standing.md", "a", encoding="utf-8") as f:
        f.write(refill)
    write(tmp_path, "20261103-two", items="- [ ] 1. a\n- [ ] 2. b")
    with open(tmp_path / "IDEA-20261103-two.md", "a", encoding="utf-8") as f:
        f.write(refill)
    write(tmp_path, "20261104-closed", status="rejected")
    with open(tmp_path / "IDEA-20261104-closed.md", "a", encoding="utf-8") as f:
        f.write(refill)
    write(tmp_path, "20261105-no-refill")
    write(tmp_path, "20261106-not-owner", owner=False)
    with open(tmp_path / "IDEA-20261106-not-owner.md", "a", encoding="utf-8") as f:
        f.write(refill)
    cards = oc.refill_cards(tmp_path)
    assert cards == [("IDEA-20261102-standing.md", 0), ("IDEA-20261103-two.md", 2)]
    text = oc.refill_note(cards)
    assert text.count("REFILL: ") == 2
    assert "IDEA-20261102-standing.md has 0 open item(s)" in text
    assert "Як поповнювати" in text and "before step f" in text and "BUDGET" in text
    assert oc.refill_note([]) == ""


def test_main_prints_both_notes(tmp_path, capsys):
    write(tmp_path, "20261102-standing", items="- [x] 1. зроблено")
    with open(tmp_path / "IDEA-20261102-standing.md", "a", encoding="utf-8") as f:
        f.write("\n\n## Як поповнювати\n- BACKLOG\n")
    write(tmp_path, "20261111-open")
    assert oc.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OWNER CARDS") and "\nREFILL: IDEA-20261102-standing.md has 0" in out


def test_the_live_standing_card_gets_a_refill_note():
    ideas = Path(__file__).resolve().parent.parent / "docs" / "ideas"
    assert "IDEA-20261102-standing-improvements.md" in [c[0] for c in oc.refill_cards(ideas)]


def test_the_launcher_appends_the_note():
    sh = (Path(__file__).resolve().parent.parent / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
    assert 'OWNER_NOTE="$(python3 "$REPO/scripts/owner_cards.py" docs/ideas' in sh
