# 2026-11-07 · Швидші сканери `ast` у тестах (пункт 255)

Пункт 255 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Змінено лише три тестові файли: `tests/test_scripts_import_safe.py`, `tests/test_network_boundary.py`, `tests/test_no_real_network.py`. `src/`, `scripts/` і `execution/` не чіпав. Знахідки сканерів ті самі.

- **Замір до змін.**
  - `uv run pytest -q -n auto --durations=15`: 7174 passed, 3 skipped за 259.2 с. 155 с із них забирає `test_s3_known_result_in_sample` (позначка `slow`), а `make test` його не запускає.
  - Набір `make test` (`-n auto --dist worksteal -m "not slow"`): 7173 passed, 3 skipped за **132.9 с**.
  - Найповільніший тест цього набору — `test_scripts_import_without_side_effects`: 18.7 с під xdist і 19.1 с окремо. Це справжня робота, а не тиснява між воркерами.
- **Причина.** 13 с з 18.5 с ішло на `ast.parse`: 1147 розборів на 233 файли. `module_info` кожного скрипта верхнього рівня будувався заново для кожної теки (`scripts/`, `research/`, `server/`), а `import_side_effects` розбирав той самий текст двічі.
- **Що змінено:**
  - `test_scripts_import_safe.scan`: кожен файл розбирається двічі (раз для `module_info`, раз для власної перевірки). Один `module_info` спільний для тек, у яких однаково видно сусідів, котрих імпортує файл. Дерева в пам'яті не тримаю: усі разом важать ≈ 340 МіБ на воркер. Результат `scan()` на справжньому `scripts/` збігся з версією з HEAD: 232 файли, ті самі 5 файлів зі знахідками.
  - `test_network_boundary.network_imports` і `test_no_real_network.find_network_risks` отримали текстовий префільтр: файл без жодної назви мережевої бібліотеки не розбирається. Таких ≈ 70 % файлів. Як і в `test_naive_datetime`, текст із символом, який NFKC перетворює на ASCII-літеру, розбирається завжди (повноширинне `ｈｔｔｐｘ` теж імпортує httpx). Додано по тесту на префільтр.
  - Реальний годинник і мережа: окремі охоронці вже діють (`test_clock_bombs`, autouse-фікстура `no_real_network`), нових залежностей не знайшов.
- **До / після** (тест окремо, `-p no:xdist`):
  - `test_scripts_import_without_side_effects`: 19.1 → 9.0 с (`scan()` у скрипті 16.1 → 6.5 с);
  - `test_scripts_network_imports_match_allowed_list`: 5.4 → 3.7 с;
  - `test_src_network_imports_match_allowed_list`: 2.4 → 1.1 с;
  - `test_tests_reach_no_real_network`: 3.7 → 2.3 с.
  - Разом ≈ −14.5 с процесорного часу з ≈ 800 с, тобто ≈ 2 с стіни `make test` при 6 воркерах. Це оцінка: повний набір після змін за завданням не запускав.
- **Не змінено** (один рядок у BACKLOG 6.37): `test_naive_datetime` (5.8 с, майже всі файли містять `datetime` і `now`, тож надійного вужчого префільтра без ризику пропуску немає); `test_deps_check` (5.0 + 4.0 с, робота в `scripts/deps_check.py`, а спільна фікстура тестувала б не `main()`); `test_tools_wrong_type_fields[cycle_regime / deribit_vrp]` і частини `test_setup_signals` (обчислення в `src/`); перевірку залежності від порядку тестів (pytest-randomly не встановлено, повний прогін у зворотному порядку не робив).
- Перевірки: `make lint` пройшов; змінені файли разом із `test_naive_datetime` і `test_clock_bombs` дали 59 passed (після виправлення власного нового тесту).
