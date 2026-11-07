# Рев'ю: lifespan не падає на зіпсованій БД (пункт 222, режим C)

Вердикт: **approve**

Перевірив сам: `git diff`, `make lint` (чисто), `tests/test_provider_lifecycle.py` + `tests/test_server.py` (84 passed), мутація (див. п. 2).

## Знахідки

1. (інфо, ок) Інваріант 7: `finally` з `scheduler.stop()` і `rt.aclose()` не чіпано, `try/yield` залишається; тест перевіряє, що після виходу `rt.store.conn` недоступний (сховище не лишилось напіввідкритим).
2. (інфо, ок) Тест ловить регрес. Тимчасово замінив `except STORE_ERRORS` на інший тип: `test_lifespan_survives_corrupt_store` падає з `sqlite3.DatabaseError: file is not a database` на вході в lifespan. Повернув, diff чистий.
3. (інфо, ок) Справна БД: `store_ok=True`, `build_default_scheduler(rt)` викликається як раніше; `test_lifespan_healthy_store_starts_collector` перевіряє `scheduler.jobs` і `journal_list ok=true`. Без сховища (`db_path` порожній) шлях той самий, що був.
4. (інфо, ок) Інваріант 1: інструменти зі сховищем і так проходять через `store_ready`, який повторює `init()` і пише `missing: store`; тест це підтверджує (`ok=false`, `file is not a database`). `market_ticker` працює.
5. (низька) Ловляться лише `sqlite3.Error` і `OSError`; інші винятки `init()` як і раніше обривають вхід. Це заявлено в змінах, узгоджено з `STORE_ERRORS`. Виправляти не треба.
6. (низька) Помилка пишеться з `e` без шляху файлу, але текст `OSError` може містити шлях. Секретів там немає. Виправляти не треба.
7. Обсяг: змінено лише `server.py`, тест, `docs/changes`, пункт 222 і BACKLOG 6.41. `execution/`, `core/`, колектори й реєстрації не чіпано.

Блокерів немає.
