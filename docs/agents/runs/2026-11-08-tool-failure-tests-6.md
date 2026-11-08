**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`: відкритих пунктів лишався один (8a, чекає сервера), тож додано пункт 288 (тести збою, джерело BACKLOG 6.53).

**Результат:**
• `tests/test_tool_failure_extra_6.py`: 84 тести для `positions_add`, `positions_close`, `rules_add`, `rules_disable`, `rules_list`, `rules_presets_load` і `journal_list` (blob з невалідним UTF-8).
• Знайдено й виправлено: `OverflowError` на часі 10**20, inf / Infinity у збережених і відповідних числах, `RecursionError` у `compile_expr`, падіння `rules_presets_load` на сіді не тієї форми, зламана відповідь на blob.
• Допоміжні `reject_bad_ts` і `finite_json` перенесено в `tools/_util.py`.
• `make lint` чисто, `make test`: 7801 passed, 3 skipped.

**Критик:** approve (`docs/reviews/2026-11-08-tool-failure-tests-6.md`); не блокує: пресети з `cooldown_seconds` у вигляді float (1800.0) пропускаються.

**Далі:** owner. Рішення, які можна змінити: пошкоджена позиція / правило все одно закривається / вимикається (на відміну від `journal_close`); порожній список пресетів тепер `ok=false`. Наступний пункт картки: `enabled` правила як bool, `positions_list` з `tags` "[NaN]".
