**Крок:** p — постійна картка вдосконалень (`Порядок: 1`); решта карток власника чекає часу чи власника. Додано пункт 263 з BACKLOG 6.53.
**Результат:**
• Захищено 5 місць від зіпсованих полів живих даних (NaN, inf, `None`, рядок, `10**400`): `perps.current_funding` (mark), `forecast_log._rv30`, книга `hyperliquid_book`, `pm_threshold.quote_at` / `last_trade_before`, `radar.forward_vols`.
• Джерело названо в `warnings` / `missing`, крім чистих функцій `pm_threshold` (там немає каналу попереджень).
• 54 нові тести (`tests/test_live_float_guard_2.py`); на живих Binance / Bybit / HL нових попереджень немає.
• У BACKLOG 6.53 лишилось 6 місць у `tools/` і `analytics/`, ще 2 у `collector/`.
**Критик:** C — approve, блокерів немає ([рев'ю](../../reviews/2026-11-08-live-float-guard-2.md)). Не перевірено прогоном, що нові тести падають на старому коді (`git stash` заборонено); висновок із коду.
**Перевірка координатора:** `make lint` чисто, `make test` 7318 passed.
**Далі:** `owner`. Нових питань немає.
