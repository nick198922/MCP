# 2026-10-22 — міграції SQLite безпечні для кількох процесів

- `Store._migrate` перечитує `PRAGMA user_version` після `BEGIN IMMEDIATE`: процес, що чекав на блокування, більше не повторює вже застосований блок (`duplicate column name: positions_json`).
- Знайдено, коли CI почав ганяти тести паралельно (`pytest -n auto`, PR #181): воркери одночасно мігрували спільну `data/tradedesk.sqlite`. Та сама гонка можлива в робочій базі (служба + скрипт на кшталт `radar_backfill.py` після нової схеми).
- Тест `tests/test_store.py::test_concurrent_inits_on_one_file_apply_each_migration_once` (6 одночасних `init()` на одному файлі; на старому коді падав 5 з 5).
