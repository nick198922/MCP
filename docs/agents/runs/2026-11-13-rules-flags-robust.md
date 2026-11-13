**Крок:** постійна картка `standing-improvements`, п. 357 (profit-search п. 2–4 вичерпано, 8f/8k/8l чекають даних 2027–28, сито на паузі).

**Результат:**
• `rules` (`plan_list`/`plan_review`/`plan_link`) і `entry_flags` (`carry_paper_status`/`_close`) на 300 рівнів більше не валять серіалізатор MCP; нормалізація лише у відповіді, сховище не змінюється, є попередження.
• 39 нових тестів, `make lint && make test`: 9896 passed.
• Знайдено новий п. 358: `events_list` (`meta`), `alerts_recent` (`payload`) та інші JSON-клітинки.

**Критик:** C (sonnet) — approve, два дрібні зауваження без блокерів ([рев'ю](../../reviews/2026-11-13-rules-flags-robust.md)).

**Далі:** owner.
