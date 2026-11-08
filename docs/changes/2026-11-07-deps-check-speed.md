# Швидший `scripts/deps_check.py` (пункт 192)

- `imports_of` більше не обходить вирази: імпорт буває лише інструкцією, тож обхід спускається тільки в списки інструкцій (`body`, `orelse`, `finalbody`, `handlers`, `cases`) у тому ж порядку полів, що й повний обхід. Умовні імпорти (`try ... except ImportError`, `if TYPE_CHECKING:`, `if find_spec("x"):`) обробляються так само, як раніше.
- Результат той самий: звіт `deps_check.py` до і після збігається байт у байт; повна таблиця `imports_of` (модуль, рядок, умовність, порядок) на 950 файлах `src/` + `scripts/` + `tests/` (8479 імпортів) теж однакова.
- Час `imports_of` на цих 950 файлах: 13.0 с → 7.2 с. Решта — сам `ast.parse` (~6.4 с), його не прибрати.
- Було / стало, `uv run pytest -q tests/test_deps_check.py --durations=5` (та сама машина):
  - `test_main_on_repository`: 10.0 с → 5.9 с (повторний запуск 5.2 с);
  - `test_repository_scripts_import_nothing_undeclared_unconditionally`: 6.6 с → 3.9 с;
  - весь файл: 16.8 с → 11.4 с (у ньому тепер 16 тестів замість 15).
- Новий тест `test_imports_nested_in_statements_are_found`: імпорти у функції, async-функції, `async with` / `async for`, класі, методі з `try`, обробнику `except` / `except*`, `else` / `finally`, `if` / `elif` / `else`, `with`, `for` / `while` з `else`, `match` / `case` (зокрема `find_spec` усередині `case`) знаходяться, умовність правильна.
- `deribit_vrp` (BACKLOG 6.50) лишається.
