# 05.11 — вік знімка асистента в здоров'ї колекторів

Пункт 101 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.13 (в).

- `assistant_snapshot` пише JSON-файл, а не таблицю, тому його тиху зупинку раніше бачила лише панель. Тепер `scripts/collector_health.brief_line` читає `asof_utc` файлу (шлях як `SNAPSHOT_PATH` панелі: `TRADEDESK_ASSISTANT_SNAPSHOT_PATH` або `<TRADEDESK_DATA_DIR>/assistant/snapshot.json`; лише читання, без мережі).
- Чисте рішення: `analytics/collector_health.snapshot_line` (час параметром). Поріг — 2 каденції джоби: 600 с за замовчуванням (`config.Settings.assistant_snapshot_seconds`, планувальник), або `TRADEDESK_ASSISTANT_SNAPSHOT_SECONDS` із `features.env`, тобто 20 хв.
- У брифі рядок з'являється лише коли файлу немає, час не прочитано або знімок застарів («Знімок асистента застарів: останній … UTC, N тому»). Коли все гаразд, вивід не змінився. Вимикач `TRADEDESK_ASSISTANT_SNAPSHOT_ENABLED` зі значенням не «1» у `features.env` прибирає перевірку. Текстовий звіт і панель не змінено.
- Тести: свіжий, застарілий, відсутній і зіпсований файл, каденція з планувальника й `features.env`, рядок у `brief_line` разом із рядком БД. `EXEMPT` у тесті-охоронці: нова причина (перевіряється за віком).
- Живцем 05.11 на сервері: файл `/var/lib/tradedesk-mcp/assistant/snapshot.json` свіжий, рядка про знімок немає; решта рядка брифу та сама (Polymarket).
