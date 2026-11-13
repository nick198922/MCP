# 2026-11-13 — `rules` у `plan_*` і `entry_flags` у `carry_paper_*` (пункт 357)

Інваріант 1 `CLAUDE.md`; знайдено пробою в пункті 356 ([зміни](2026-11-13-tags-legs-robust.md)).

- **Помилка:** `rules` плану або `entry_flags` пари carry, вкладені на 300 рівнів, розбиралися, але валили серіалізатор MCP («Circular reference detected (depth exceeded)», `is_error=true`) у `plan_list`, `plan_review`, `plan_link`, `carry_paper_status` і `carry_paper_close` (у закритті — уже після запису закриття й рядка журналу).
- **Виправлено:** нові `clean_row_rules` і `clean_row_flags` у `tools/_util.py` (поряд із `clean_row_tags`). `rules` показуються як словник лише з рядковими значеннями (не-словник → `{}`), `entry_flags` — як список рядкових елементів (рядок `"x"` → `["x"]`, інше → `[]`); `null` лишається як є; попередження з id. NaN / Infinity у `rules`: план пропускається, як і раніше; у `entry_flags`: пара показується з `[]` і попередженням (пари не ховаються). Клітинки в сховищі не змінюються; `ok` не змінився.
- Новий `tests/test_tool_failure_extra_24.py` (39 тестів: 11 видів пошкоджених `rules` у `plan_list`, NaN, `plan_review`, `plan_link`, 11 видів `entry_flags` у `carry_paper_status`, NaN, закриття й скасування пари з одноразовим закриттям, правильні дані без попереджень, збої сховища). Якщо вимкнути нормалізацію, падають 31 з 39.
- Відкрите (пункт 358 картки): `meta` у `events_list` і `payload` у `alerts_recent` на 300 рівнів так само валять серіалізатор (перевірено пробою).
