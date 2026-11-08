# Рев'ю: тести закриття сховища в `Runtime.aclose()` (пункт 160)

Режим C. Вердикт: **approve**.

Перевірено сам: `git diff`, `git status`, `src/tradedesk/runtime.py` (aclose, рядки 196-217; `Store(s.db_path) if s.db_path else None`, рядок 167), `src/tradedesk/store/db.py` (клас `Store`), `make lint` (чисто), `uv run pytest -q tests/test_provider_lifecycle.py` (5 passed).

## Знахідки

1. Обсяг (ок). Змінено лише `tests/test_provider_lifecycle.py`, новий файл `docs/changes/2026-11-06-store-aclose-test.md` і галочка пункту в картці. `src/` та `execution/` не чіпали, мережі в тестах нема.

2. `test_runtime_without_store_closes_twice` (ок, не порожній). `db_path=""` справді дає `store is None` (runtime.py:167), тож гілку `if self.store is not None` у `aclose` тест проходить. Подвійний виклик покриває повторне закриття провайдерів.

3. `test_store_aclose_is_idempotent` (ок, не порожній). Покриває `aclose()` до `init()` (`_conn is None`), закриття, повторне закриття і `RuntimeError` від `Store.conn` після закриття. Якби `aclose` не обнуляв `_conn`, останнє твердження впало б.

4. `test_new_runtime_reopens_same_db_file` (ок). Перевіряє те, що просить пункт: новий Runtime на тому самому файлі, та сама версія схеми (`> 0`), рядок першого Runtime збережено, запис проходить. Тест зелений, а `Store.init` чесно повторно мігрує (`_migrate` пропускає застосовані версії).

5. Низька серйозність: третій тест сам по собі не довів би, що перше з'єднання закрите (SQLite у WAL дозволив би другому з'єднанню читати й писати і за відкритого першого). Цю властивість уже ловить наявний `test_store_survives_runtime_aclose` і другий новий тест (`conn` кидає після закриття), тож прогалини немає. Виправлення не потрібне.

6. Документація (ок). Запис у `docs/changes/` є, українською, чесно каже «помилок не знайдено, `src/` не змінено». Інваріант 7 відповідає. Нових інструментів нема, тож README/PARITY/EXPECTED_TOOLS не потрібні.

## Блокерів немає.
