**Крок:** p/постійна картка `standing-improvements` (пункт 445): усі картки власника заблоковані, незаблокованих пунктів у постійній < 3.

**Результат:**
• `scripts/x_backfill.py --stat` більше не падає, якщо в `state.json` у завершеного місяця `oldest` у хибному форматі: такий місяць іде в нове поле `bad_oldest`, решта звіту рахується.
• Новий тест у `tests/test_x_backfill.py`; `make lint` і `make test` — 11019 passed.

**Критик (C, sonnet):** approve. Низьке зауваження: `if oldest:` пропускає `0`/`""` (не блокує). [Рев'ю](../../reviews/2026-11-22-x-backfill-bad-oldest.md).

**Заблоковано (чому не взято):**
• `agents-mcp-access` п. 1: правка `.claude/agents/*` відхилена дозволами прогону (Edit denied), попри відповідь власника в Q-20261114-2. Потрібна правка в сесії чату або дозвіл на Edit для `.claude/`.
• `options-playbook` п. 4: черга правил порожня (4.10–4.12).
• `structural-edges-depth` 8f: чекає даних (≈ 2028); `book-walls` п. 2: не раніше 15.12; `profit-search`, `x-traders`, sieve (пауза), `demo-setups-depth`, `short-setups-demo`: час / дані / відповіді власника.

**Далі:** `owner`. Нових питань нема.
