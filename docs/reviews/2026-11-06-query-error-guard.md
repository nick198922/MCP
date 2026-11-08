# Рев'ю: захист запитів до сховища в інструментах історії (пункт 166)

Режим C. Вердикт: **approve**. Блокерів немає.

## Що перевірено самостійно
- `make lint`: чисто.
- `pytest tests/test_tools_no_store.py tests/test_tools_never_raise.py tests/test_server.py`: 90 passed.
- Мутація: у `alerts_recent` замінив `except STORE_ERRORS` на `except ZeroDivisionError`. `test_store_query_error_goes_to_missing` впав («tools that raised: alerts_recent»). Мутацію відкатано, файл повернуто до стану з diff.
- Усі 8 звернень `rt.store.*` у `history.py` тепер під `try/except STORE_ERRORS`; `liquidations.py` та `system_health` теж.

## Знахідки
1. (info) Вихід на робочому сховищі не змінився: змінено лише обробку винятків. Єдина зміна тексту: `config`-повідомлення для `range_detect`/`market_regime`/`range_setup` тепер «to enable source='store'» замість «to use …». Заявлено в change-файлі, нешкідливо.
2. (info) `range_setup` з `source='store'`: `init()` стояв без `try`, тобто було реальне порушення інваріанта 1. Виправлено, додано окремий тест для режимів `query_error` і `unreachable`. Добре.
3. (info) `system_health` з `ok=true` при помилці сховища: розумно. Звіт про провайдерів, інструменти й колектор лишається, помилка видна в `missing` і `store.status="error"`. Тест це перевіряє, включно з `health is not None`.
4. (info) `QUERY_GUARDED == STORE_TOOLS` у `test_store_tool_discovery` змушує новий інструмент сховища мати перевірку запитів. Сильна гарантія.
5. (low, не блокер) `STORE_ERRORS = (sqlite3.Error, OSError)` не покриває, наприклад, `ValueError`/`RuntimeError` з `Store` при закритому з'єднанні. `system_health` окремо ловить `RuntimeError`, а запити інструментів його не ловлять. Це не нове в цьому diff і той самий набір, що в попередніх кроках (журнал, правила), тож лише зауваження на майбутнє.
6. (low) Рядок `NO_QUERY_WITH_ARGS` у тесті довший за інші, але `ruff format --check` проходить, нічого робити не треба.

## Інваріанти CLAUDE.md
1 (не кидати): виконано для всіх 10 інструментів. 2, 3, 4, 5: без змін, нових інструментів нема. `docs/changes/2026-11-06-query-error-guard.md` є. Повний набір тестів не запускав (це робить координатор).
