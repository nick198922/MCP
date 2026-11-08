# Рев'ю: тести збою, частина 6 (пункт 288)

Вердикт: **approve**

Перевірено самостійно:
- `make lint`: чисто.
- `pytest` на `test_tool_failure_extra_6.py`, `test_tool_failure_extra_5.py`, `test_positions.py`, `test_rules.py`, `test_rules_check.py`, `test_journal.py`, `test_events.py`, `test_analytics_carry_rules_exits.py`: 251 passed. Окремого `test_dsl.py` немає, `compile_expr` покривають `test_rules.py` і новий файл.
- Diff `src/` прочитано повністю. `execution/`, ключів і мережі зміни не торкаються. Нових інструментів немає, тож чекліст нового інструмента не застосовується.

Знахідки:

1. (info) Інваріант 1: усі виправлені шляхи тепер повертають `ok=false` з `invalid_input` або `data_unavailable`. Винятки назовні (`OverflowError` на `ts=10**20`, `RecursionError` у `compile_expr`, `AttributeError` на seed не у формі `{"presets": [...]}`) закрито. `compile_expr` ловить `ValueError`, `RecursionError` і `MemoryError` та підіймає `DslError`.
2. (info) Регресій немає:
   - `positions_close` і `rules_disable` на зіпсованому рядку все одно пишуть закриття чи перемикання, а відповідь лише без рядка, із попередженням. Це покриває `test_positions_close_corrupt_position_is_closed_without_its_row` і `test_rules_disable_corrupt_rule_is_switched_without_its_row`.
   - Межа cooldown пресетів 60–86400 збігається з `rules_add`.
   - Порожній або не-dict seed дає `ok=false`.
   - Якщо всі пресети зіпсовані, теж `ok=false`.
   - Вбудований seed завантажується без пропусків (`test_rules_presets_load_bundled_seed_loads_without_skips`).
   - Шляхи на коректних даних покриті окремими «unchanged»-тестами.
3. (info) Тести змістовні: параметризовані по полях і значенням, є перевірка «валідні екстремальні значення зберігаються», тож відсікання не надмірне. Твердження автора «59 з 84 падають на старому коді» я не перевіряв (stash заборонено в цьому прогоні); за змістом перевірок (inf, 10**20, blob, не-dict seed) на старому коді вони падали б.
4. (low, не блокує) `_preset_ok` відхиляє `cooldown_seconds` у вигляді float (наприклад 1800.0). Для вбудованого seed це не проблема (тест є), для чужого seed пресет пропускається з попередженням. Прийнятно.
5. (low, не блокує) Відкриті питання автор сам назвав у `docs/changes/`: `enabled` правила читається як bool для будь-якого значення, `positions_list` на `tags` "[NaN]" не перевірено. Варто записати наступним пунктом постійної картки.
6. (info) Документація: BACKLOG 6.53, HANDOFF і `docs/changes/2026-11-08-tool-failure-tests-6.md` оновлені. Перенесення `finite_json` і `reject_bad_ts` в `_util.py` без зміни поведінки для `events` і `journal` (тести `extra_5` проходять).
