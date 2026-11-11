# BACKLOG: прибрано розділ «З тижневого аудиту 2026-11-10» (пункт 317)

Картка: [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 317. Код не змінено.

- Пункт про `scripts/checkpoint_report.py` (лічильники карток `forward`) закрито. Його виконано пунктами 312–313, критик C дав approve обом ([рев'ю 312](../reviews/2026-11-10-checkpoint-forward-counters.md), [рев'ю 313](../reviews/2026-11-11-checkpoint-carry-done-counter.md)).
- Чотири зроблені пункти перенесено дослівно, з підпунктами, у [`history/BACKLOG-done.md`](../history/BACKLOG-done.md), розділ «Перенесено 2026-11-11»: черга агентів, розклад витрат carry A, лічильники `forward`, підказка `git show`. Змінено лише відносні посилання (додано `../`).
- У BACKLOG лишились вступ розділу і 5 відкритих пунктів: ємність демо, `closed_ts` привидів, таймаути `instruments-info`, `r_band` n 100, таймер аудиту. Вступ тепер посилається на перенесене: на ці пункти досі посилаються текстом `docs/verification/2026-11-10-carry-a-cost-breakdown.md` і `docs/changes/2026-11-10-checkpoint-forward-counters.md`.
- Розмір `docs/BACKLOG.md` (`wc -c`): 167 594 → 163 139 байт (−4 455).
- Перевірка: `make lint` чисто. Тести, що читають BACKLOG (`tests/test_docs_links.py`, `tests/test_todo_inventory.py`): 11 passed. `scripts/docs_links.py` з перевіркою якорів: 0 битих посилань.
