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


def test_marked_confirmed_card_goes_to_benchmark_group_not_candidates():
    plain = rb.parse_card("# IDEA-20261020-p: P\n\nСтатус: confirmed — перевага після витрат\n", "IDEA-20261020-p")
    # The marker sits past the 230-char verdict cut, as on the real alt-basket card.
    long_line = "Статус: confirmed — частоти " + "x " * 150 + "(еталон, не для розробника)\n"
    bench = rb.parse_card("# IDEA-20261023-b: B\n\n" + long_line, "IDEA-20261023-b")
    assert not plain["benchmark"] and bench["benchmark"] and "не для розробника" not in bench["verdict"]
    assert bench["status"] == "confirmed" and rb.card_progress(bench) == (1, 1)
    other = rb.parse_card("# IDEA-20261020-r: R\n\nСтатус: rejected — не для розробника\n", "IDEA-20261020-r")
    assert not other["benchmark"]
    text = rb.render([plain, bench, other], date(2026, 11, 4))
    names = [g for g, _ in rb.GROUPS]
    assert names.index("📏 Еталон (не стратегія)") == names.index("✅ Підтверджено або побудовано") + 1
    ok = text[text.index("## ✅ Підтверджено") : text.index("## 📏 Еталон")]
    bm = text[text.index("## 📏 Еталон") : text.index("## ✗ Відхилено")]
    assert "IDEA-20261020-p" in ok and "IDEA-20261023-b" not in ok
    assert "IDEA-20261023-b" in bm and "IDEA-20261020-p" not in bm
    assert "✅ Підтверджено або побудовано: **1**" in text and "📏 Еталон (не стратегія): **1**" in text
    assert "📏 Еталон — `confirmed` з позначкою «не для розробника»" in text


def test_repo_cards_all_parse():
    root = Path(__file__).resolve().parent.parent / "docs/ideas"
    cards = [rb.parse_card(p.read_text(encoding="utf-8"), p.stem) for p in root.glob("IDEA-*.md")]
    assert cards and all(
        c["status"] in {"proposed", "accepted", "forward", "testing", "confirmed", "rejected", "inconclusive", "built"} for c in cards
    )


def test_repo_alt_basket_benchmark_card_is_a_benchmark():
    p = Path(__file__).resolve().parent.parent / "docs/ideas/IDEA-20261023-alt-basket-leverage-benchmark.md"
    c = rb.parse_card(p.read_text(encoding="utf-8"), p.stem)
    assert c["status"] == "confirmed" and c["benchmark"] and rb.board_status(c) == "benchmark"


def test_progress_checklist_and_fallback_by_status():
    text = "# IDEA-20261017-p: P\n\nСтатус: testing\n\n## Прогрес\n- [x] one\n- [X] two\n- [ ] three\n\n## Інше\n- [ ] not counted\n"
    c = rb.parse_card(text, "IDEA-20261017-p")
    assert (c["steps_done"], c["steps_total"]) == (2, 3) and rb.card_progress(c) == (2, 3)
    assert rb.checklists(text) == [("Прогрес", 2, 3), ("Інше", 0, 1)]
    assert rb.card_progress(rb.parse_card("Статус: rejected — ні\n", "IDEA-x")) == (1, 1)
    assert rb.card_progress(rb.parse_card("Статус: accepted\n", "IDEA-y")) == (0, 1)
