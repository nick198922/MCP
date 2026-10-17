"""scripts/results_board.py: the results board generated from idea cards."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import results_board as rb  # noqa: E402

CARD = """# IDEA-20261013-liquidation-flush-overshoot: каскади ліквідацій | перебіг

Статус: rejected — основна комірка +0.07 %, CI [−0.41; +0.53] ([звіт](../verification/2026-10-14-liquidation-flush.md))
"""


def test_parse_card_reads_status_verdict_report_and_priority():
    c = rb.parse_card(CARD, "IDEA-20261013-liquidation-flush-overshoot")
    assert c["status"] == "rejected" and c["report"] == "2026-10-14-liquidation-flush"
    assert c["verdict"].startswith("основна комірка") and "](" not in c["verdict"]
    owner = rb.parse_card("# IDEA-20261017-x: X\n\nСтатус: accepted (власник)\nПріоритет: власник\nПорядок: 2\n", "IDEA-20261017-x")
    assert owner["owner"] and owner["order"] == 2 and owner["report"] == ""


def test_render_groups_and_orders_owner_cards_first():
    cards = [
        rb.parse_card(CARD, "IDEA-20261013-liquidation-flush-overshoot"),
        rb.parse_card("# IDEA-20261017-b: B\n\nСтатус: accepted\nПріоритет: власник\nПорядок: 2\n", "IDEA-20261017-b"),
        rb.parse_card("# IDEA-20261017-a: A\n\nСтатус: accepted\nПріоритет: власник\nПорядок: 1\n", "IDEA-20261017-a"),
        rb.parse_card("# IDEA-20261016-c: C\n\nСтатус: proposed\n", "IDEA-20261016-c"),
    ]
    text = rb.render(cards, date(2026, 10, 17))
    work = text[text.index("## 🔄 У роботі") : text.index("## ✗ Відхилено")]
    assert work.index("IDEA-20261017-a") < work.index("IDEA-20261017-b") < work.index("IDEA-20261016-c")
    assert "👤 #1" in work and "✗ Відхилено: **1**" in text and "| B | " in work
    assert "liquidation-flush-overshoot](ideas/" in text and "(verification/2026-10-14-liquidation-flush.md)" in text


def test_repo_cards_all_parse():
    root = Path(__file__).resolve().parent.parent / "docs/ideas"
    cards = [rb.parse_card(p.read_text(encoding="utf-8"), p.stem) for p in root.glob("IDEA-*.md")]
    assert cards and all(
        c["status"] in {"proposed", "accepted", "testing", "confirmed", "rejected", "inconclusive", "built"} for c in cards
    )
