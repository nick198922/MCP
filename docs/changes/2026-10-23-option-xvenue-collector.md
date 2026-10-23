# 2026-10-23 — колектор денних опціонів Deribit ↔ Bybit, перший зріз

- Нове завдання колектора `option_xvenue` (`src/tradedesk/collector/option_xvenue.py`): щогодини найближча експірація за 2–20 год до 08:00 UTC, ATM ±3 страйки, колли й пути BTC і ETH; обидві біржі в одному рядку — bid/ask, розмір, IV, індекс кожної біржі, час читання кожної ноги. Лише читання: Deribit через `rt.deribit`, Bybit через ccxt (`Exchanges.public_get` → `_call`). Збій однієї біржі — `missing`, рядки іншої все одно пишуться; немає денної експірації на Bybit — `missing`, не падіння.
- Таблиця `option_xvenue_snapshot` (схема 25), зберігання 365 днів (`TRADEDESK_RETENTION_OPTION_XVENUE_DAYS`).
- Чиста функція перетину книг `analytics/option_xvenue.crossing` / `crossing_from_row`: bid_B − ask_D і bid_D − ask_B у частках спота після комісії 0.03 % на ногу (`FEE_RATE`), глибина ≥ 0.1 монети (`MIN_DEPTH`), плюс різниця індексів і розбіжність часу ніг. Поріг 0.15 % тут навмисно відсутній (реєструється окремим комітом).
- Завдання **вимкнене** (`TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED=1`), у `deploy/features.env` не додано: умова 3 картки.
- `scripts/probe_bybit_options.py`: перевірка денних опціонів Bybit через ccxt. Живцем 23.10: є (BTC 756, ETH 666 тикерів, усі USDT-розрахункові); індекс Bybit +0.15 % до Deribit.
- Картка `IDEA-20261023-bybit-deribit-daily-option-dislocation-forward`: відмічено пункти критика й розробника, додано розділ «Перевірка невідомих» і нові пункти «Прогресу»; пункт у `docs/BACKLOG.md` (Фаза 3).
