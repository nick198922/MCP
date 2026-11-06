# Тест закриття сховища в `Runtime.aclose()` (пункт 160)

- У `tests/test_provider_lifecycle.py` додано три тести поруч із наявним `test_store_survives_runtime_aclose` (той уже перевіряв, що після `aclose()` з'єднання aiosqlite закрите, а `Store.conn` кидає `RuntimeError`): Runtime без БД (`db_path=""`, `store is None`) закривається двічі без винятку; повторний `aclose()` сховища (і `aclose()` до `store.init()`) не кидає виняток; новий Runtime на тому самому файлі після закриття першого відкриває БД, бачить його рядок, ту саму версію схеми й приймає запис.
- Помилок не знайдено, `src/` не змінено.
- Перевірка: `make lint`; `uv run pytest -q tests/test_provider_lifecycle.py` — 5 passed.
