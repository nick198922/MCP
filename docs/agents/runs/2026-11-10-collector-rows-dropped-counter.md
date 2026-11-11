**Крок:** p/standing-improvements, пункт 307 — розробник → критик C (Sonnet; дифф малий, без core/providers/execution).

**Результат:**
• `system_health` → `collector[]` тепер має `last_rows_dropped` (останній запуск) і `rows_dropped_total` (від старту сервера).
• Хелпер `collector/bad_rows.py` на ContextVar; 7 місць відкидання в 6 модулях колектора; сигнатури завдань не змінено.
• 11 нових тестів без мережі; `make lint` чистий, `make test`: 8919 passed, 3 skipped.
• Обмеження: одиниця змішана (рядки / поля, що стали NULL); лічильник у пам'яті, скидається при рестарті; `flow_snapshot`, `option_*`, `bars_4h`, `pm_gap`, `carry_paper_*` не рахуються. Панель (`scripts/dashboard.py`) читає БД, тож лічильник їй ще не видно — окремий пункт.

**Критик:** approve, блокерів немає ([рев'ю](../../reviews/2026-11-10-collector-rows-dropped-counter.md)).

**Далі:** owner. Пропозиція наступного пункту: показати лічильник на панелі.
