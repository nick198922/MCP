# 07.11 — архів відповідей у `QUESTIONS.md`

Пункт 194 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md).

- `docs/QUESTIONS.md`: відповіді Q-20261102-1 і Q-20261103-1 перенесено дослівно в [`history/QUESTIONS-answered.md`](../history/QUESTIONS-answered.md) — нагору, найновіші першими, як у переносі 03.11 (змінено лише відносні посилання — `../`). Шаблон і відкрите Q-20261106-1 не змінено; підвал: відкрите одне питання, відповіді 02–03.11 — в архіві.
- Посилання на Q-20261103-1 у [`knowledge/trading-strategy.md`](../knowledge/trading-strategy.md) тепер веде в архів; рядок про питання в `HANDOFF.md` оновлено.
- Парсер `scripts/agents_status.py` (`open_questions`, ним же користується `scripts/dashboard.py`) бере лише `Статус: open`, тож перенос його не зачіпає; `tests/test_agents_status.py` проходить.
