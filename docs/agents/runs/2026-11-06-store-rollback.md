**Крок:** p — постійна картка `standing-improvements`: відкритих пунктів лишалось менше трьох, додано пункт 168 (BACKLOG 6.44).

**Результат:**
• Усі методи запису `Store` ідуть через `Store._write()`: коміт, при помилці чи скасуванні задачі — безумовний rollback; запис серіалізовано `_tx_lock` (без нього rollback однієї задачі стирав би незакомічені рядки іншої).
• `carry_paper_close` і `cp_cycle_commit` перенесено в `_write()`.
• 8 нових тестів: IntegrityError, скасування задачі при черзі в aiosqlite, ізоляція паралельних записів, AST-охоронець (коміт лише в `_write`/`_migrate`).
• `make lint` чисто, `make test`: 6210 passed, 3 skipped.

**Критик:** перший раунд — request changes (скасування лишало транзакцію відкритою); після фіксу повторне рев'ю — approve ([рев'ю](../../reviews/2026-11-06-store-rollback.md)). Дрібні зауваження (регістр у регулярці охоронця) — з BACKLOG 6.46.

**Далі:** owner. Нове у BACKLOG 6.46: скрипти `journal_cancel_tg.py`, `journal_close_tg.py`, `bybit_import.py` пишуть у БД без блокування й rollback. Питань до власника немає.
