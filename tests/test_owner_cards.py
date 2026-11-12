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


def test_the_launcher_appends_the_note():
    sh = (Path(__file__).resolve().parent.parent / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")
    assert 'OWNER_NOTE="$(python3 "$REPO/scripts/owner_cards.py" docs/ideas' in sh
