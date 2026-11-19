# 2026-11-19 — фільтр `tag`: лише цілий тег, кирилиця, без масок (пункт 396a)

Джерело — знахідка пункту 395 ([рев'ю](../reviews/2026-11-19-tool-desc-units.md)). Схему бази не змінено.

- `store/db.py`: `journal_list`, `plan_list` (`tag` і `exclude_tag`), `positions_list` фільтрують через `json_each(tags) ... value = ? COLLATE NOCASE` замість `tags LIKE '%"tag"%'`. Зачеплені інструменти: `journal_list`, `journal_stats`, `plan_list`, `plan_review` (відсів тіньових планів `src:demo-shadow`), `positions_list`, `positions_risk`.
- Кириличний тег (`хедж`) тепер знаходиться, і в старих рядках (`json.dumps` писав `\u…`), і в рядках, записаних без `ensure_ascii`. `%` і `_` — звичайні символи (`a_b` більше не знаходить `axb`). Збіг — лише цілий тег; ASCII-літери, як і раніше, без урахування регістру (кирилиця — з урахуванням).
- Невалідний JSON у `tags` (інший записувач) читається як «без тегів» і не ламає запит; голий JSON-рядок (`"hedge"`) — як один тег, як і раніше.
- `exclude_tag` на рядках із `tags` = NULL тепер повертає їх (раніше `NULL NOT LIKE ?` їх відсікав).
- Описи `positions_list`, `journal_list`, `plan_list` оновлено; `tests/test_tool_descriptions_units.py` тепер перевіряє нову поведінку, у `tests/test_store.py` — 3 нові тести (журнал, позиції, плани з `exclude_tag`).
