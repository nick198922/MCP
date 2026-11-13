# 2026-11-13 — систематичний обхід JSON-клітинок сховища у відповідях MCP (пункт 361)

Інваріант 1 `CLAUDE.md`. Пункти 352–358 знаходили по одному полю, яке на вкладеності 300 рівнів валило серіалізатор MCP; тут перевірено всі місця в `src/tradedesk/store/` і `src/tradedesk/tools/`, де розібраний `json.loads` вміст клітинки БД може потрапити у відповідь інструмента ([попереднє](2026-11-13-json-cells-depth.md)).

## Перевірено (клітинка у відповіді вже обмежена)

| Інструмент | Клітинка | Чим обмежено |
|---|---|---|
| `journal_list`, `journal_close` | `journal.tags` | `clean_tags` (пункти 353, 354) |
| `positions_list`, `positions_close` | `positions.tags` | `clean_tags` (пункт 352) |
| `plan_list`, `plan_review`, `plan_link` | `trade_plan.tags`, `rules` | `clean_row_tags`, `clean_row_rules` у `_sound` (пункти 356, 357) |
| `carry_paper_status`, `carry_paper_close` | `carry_paper.tags`, `entry_flags` | `_clean_pair_tags` (пункти 356, 357) |
| `assistant_paper_open`, `assistant_paper_status` | `rec_paper.legs` | показ лише правильних ніг (`_good_legs`, пункт 356) |
| `events_list` | `events.meta` | `cap_cell_depth` (пункт 358) |
| `alerts_recent` | `alerts.payload` | `cap_cell_depth` (пункт 358) |
| `paper_carry_status` | `cp_cycle.payload`, `cp_log.payload` (mark, side), `cp_position.trade` | `_capped` → `cap_cell_depth` (пункт 358) |

## Виправлено

- Неохоплених місць, де клітинка доходить до відповіді MCP, обхід не знайшов: у `tools/` немає жодного власного `json.loads`, а кожен виклик методу сховища з розібраною клітинкою або обмежує її, або не віддає.
- `Store.record_trader_positions`: стан `trader_position_state.positions_json`, глибший приблизно за 10 000 рівнів, давав `RecursionError` з `json.loads` (ловилися лише `ValueError` / `TypeError` / `KeyError`). Через це завдання колектора падало на кожному тіку для такого рахунку. Тепер така клітинка читається як «попереднього набору немає», так само як невалідний JSON. Це не MCP-вихід, але це той самий клас збою в `store/`.

## Не MCP-вихід

- `cp_position.state` (`paper_carry_status`) читається лише як числа (`entry_apr`, `fees`) і `dir`; у відповідь не потрапляє.
- `events.meta` у `event_study` — читається лише `ts`.
- `tags` у `journal_stats` і `positions_risk` — лише суми чисел; `rules` / `tags` у `review_open` — лише id і попередження; `rec_paper_get` у `plan_add` / `review_open` і `plan_get` / `journal_get` у `plan_link` — перевірка наявності чи поля розрахунку.
- Читає лише колектор чи виконання: `radar_list` (`smile`, `expiries` — числа), `cycle_regime_log_rows` (`components`), `cp_cycle_get`, `last_alerts_with_prefix`, `exec_audit_list` (`execution/`), `record_trader_positions`.
- `json.load` у `events_seed_load`, `rules_presets_load`, `calendar._load_macro`, `macro._load_seed` — файли з пакета, не клітинки БД.
- `json.loads` в `analytics/` (`pm_threshold`, `pm_calibration`, `negrisk`, `polymarket`) розбирає поля Gamma до рядків або чисел. `demo_shadow` і `perp_launch` інструменти не імпортують.

## Охоронець

Новий `tests/test_json_cells_guard.py` (6 тестів):
- три переліки, зчитані з коду через `ast`: усі виклики `json.load(s)` у `store/` і `tools/`; методи сховища, що повертають розібрану клітинку (прямо чи через хелпер); кожен їхній виклик з `tools/` (функція + порядковий номер). Нове місце валить тест, доки його не внесено до переліку з причиною;
- прогін 8 інструментів читання (`journal_list`, `positions_list`, `plan_list`, `carry_paper_status`, `assistant_paper_status`, `events_list`, `alerts_recent`, `paper_carry_status`) на сховищі, де кожна розібрана колонка вкладена на 300 рівнів (масив і об'єкт). Відповідь має бути без помилки, з `ok=true` і попередженням, а вкладеність — не глибша за `MAX_CELL_DEPTH` + 10. Перевірено мутацією: якщо вимкнути `cap_cell_depth` в `events_list`, прогін падає з «Circular reference detected (depth exceeded)»;
- `record_trader_positions` на стані 20 000 рівнів. Без виправлення падає з `RecursionError`.

Поза обходом (`collector/`, `execution/` не чіпали): `analytics/demo_shadow._tags` / `_violations` ловлять лише `ValueError`, без `RecursionError`. Їх читають скрипти й `execution/`, а не інструменти MCP.
