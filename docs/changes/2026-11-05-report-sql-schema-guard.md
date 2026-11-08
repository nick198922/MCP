# 2026-11-05 · Охоронець схеми SQL звітів контрольної точки

Пункт 88 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.15.

- Новий `tests/test_report_sql_schema.py`. Тест будує порожню БД тим самим кодом міграцій, що в проді (`Store.init`, схема 38), і звіряє з нею SQL чотирьох звітів: `scripts/checkpoint_report.py`, `scripts/collector_health.py` (разом з фрагментами `analytics/collector_health.SPECS`), `scripts/pm_gap_paper_report.py`, `scripts/condor_forward_costs.py`.
- Звичайні SQL-рядки тест знаходить обходом AST скриптів (10 запитів) і компілює через `EXPLAIN`: SQLite перевіряє таблиці й колонки ще до читання рядків. Таблиці, названі лише в рядку (`PRAGMA table_info(x)`, `sqlite_master … name = 'x'`), теж мають існувати.
- Запити з f-рядків (4) перелічено в `DYNAMIC` разом із тестом, що виконує їх на схемі. Це `series()` по всіх `SPECS` і групах, `COLS` Polymarket ⊆ колонок `pm_gap_snapshot` (`load` мовчки відкидав би відсутню колонку), `load_rows` / `detect_t0` кондора і `load_inputs` контрольної точки після T0 кондора без помилок запитів. Новий f-рядок із SQL валить тест, поки його не внесуть у перелік.
- SKIP: чотири `SELECT *` (`demo_trade`, `cp_position`, `pm_gap_outcome`, `option_leg_snapshot`). Наявність таблиці тест перевіряє, а колонки далі читає `analytics/` як ключі словника, тож їх тут не звіряють (BACKLOG 6.15).
- Розбіжностей зі схемою не знайдено, звіти не змінювались. Перевірено, що охоронець ловить помилки: перейменована колонка в запиті, `table_info` неіснуючої таблиці, зайва колонка в `COLS` і `where` спеки з відсутньою колонкою дають падіння тесту.
