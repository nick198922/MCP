**Крок:** постійна картка `standing-improvements`, п. 353 (розробник → критик C). Профіт-серч (п. 2 вичерпано, п. 3 — партія 9 щойно без карток) і structural-edges (8f/8k/8l чекають часу до 2027-03) не мали розблокованої роботи.

**Результат:**
• `clean_tags` винесено в `tools/_util.py`; застосовано в `positions_close` і `journal_list` (глибокі / не-список / NaN `tags` не ламають відповідь, закриття лишається записаним, `ok` не кидає).
• 45 нових тестів (`tests/test_tool_failure_extra_20.py`); `make lint` чисто, `make test` 9760 passed.
• Знайдено, не виправлено: `journal_close` має ту саму помилку (п. 354 додано в картку).

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-13-tags-close-journal.md). Дрібно: `finite_json` не ловить `RecursionError` (не блокує).

**Далі:** `owner`. Нових питань нема.
