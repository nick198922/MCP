# Рев'ю: пункт 133, завислі демо-угоди (режим C)

Вердикт: **approve**. Блокерів немає.

## Що перевірено самостійно
- `make lint`: чисто (ruff check і format).
- `tests/test_stuck_demo.py`, `tests/test_architecture_imports.py`: 13 passed. `tests/test_checkpoint_report.py`, `tests/test_collector_health.py`: 76 passed.
- Інваріанти: `analytics/stuck_demo.py` чистий (лише `math`, `typing`, час параметром `now_ms`, без I/O і без імпорту `execution/`). `execution/` не змінено (git status). Секретів немає. БД у `scripts/collector_health.py` відкривається через `connect_ro` із `has_table`, SELECT-ом, без запису; тест порівнює байти файлу до і після.
- Константи утримання скопійовано з `execution/demo_trader.py` (`SETUP_HOLD_MS`, `MAX_HOLD_MS`, типові 7 діб). Їх закріплює `test_hold_copies_match_the_demo_trader`: рівність словників і `hold_ms` для всіх `dt.SETUPS` × {4h, 1d, 1h, "", None}. Правило збігається з `_sync_trade` (рядок 509: `now - (opened_ts or now) > hold_ms`).
- Крайні випадки є в тестах: рівно на межі hold+grace (не завис), `closed`, `pending`, відсутні `opened_ts` (вік від `signal_ts`, з позначкою), відсутній `timeframe`, нечислові/NaN значення, S17 і 1d, обрізання списку в брифі, відсутня таблиця, відсутня БД, збій запиту.
- Користь для 30.11: `checkpoint.build` додає примітку з лічильником у звіт; вхід `demo_trades` — `SELECT * FROM demo_trade`, тож поля `opened_ts`, `link_id`, `status` є. Тест `test_checkpoint_notes_the_count` перевіряє `build` і `render`. Це відповідає пункту: завислі рядки названо, а n демо позначено неповним.

## Зауваження (не блокують)
1. Низька. Перевіряється лише статус `open`, як у пункті. Рядок, що завис у `closing` чи `unknown`, не буде названий. Якщо такі трапляються, розширити окремим пунктом.
2. Низька. Звіт контрольної точки з `--date` у минулому бачить поточний статус рядка. Це вже названо в `docs/changes/` як обмеження, достатньо.
3. Низька. `ruff format` не змінює довгі рядки в тестах (`# fmt: skip`), але стилістично вони довші за решту файлів. Лише коментар, не вимога.
