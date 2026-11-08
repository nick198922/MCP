**Крок:** постійна картка (p), відкритий лише заблокований 8a — додано пункт 280 (тести збою інструментів, продовження BACKLOG 6.53).

**Результат:**
• Виправлено `iv_rank` (зіпсований `atm_iv`) і `ohlcv_history` (зіпсована клітинка): рядок пропускається з попередженням.
• Нові тести збою для `alerts_recent`, `journal_stats`, `positions_risk`, `positions_list`, `plan_list`; знайдено й виправлено винятки (текст/blob/inf у сховищі, ділення на нуль у `plan_list`).
• Спільний хелпер `drop_corrupt_rows` у `tools/_util.py`; без тесту лишається 25 інструментів.
• `make lint` чистий, `make test`: 7609 passed, 3 skipped.

**Критик:** approve ([рев'ю](../../reviews/2026-11-08-tool-failure-tests-3.md)), зауваження не блокують.

**Далі:** `owner`. Відкрито в 6.53: `plan_review` / `plan_resolve` кидають `ValueError` на відкритому плані з текстом у `entry`; `plan_link` і list-інструменти (`journal_list`, `events_list`, `rules_list`) не перевірено.
