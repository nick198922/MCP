# 2026-11-12 · Спільний `is_permanent` замість трьох копій у скриптах Polymarket

[Постійна картка](../ideas/IDEA-20261102-standing-improvements.md), «Як поповнювати» → технічний борг (дублікати в `scripts/`).

- **Що було.** `_is_permanent` і `_NO_RETRY_REASONS` (яку помилку провайдера не повторювати в `_retry`) скопійовано в `scripts/verify_pm_threshold.py`, `verify_pm_touch.py`, `verify_pm_yes_calibration.py`, тіла ідентичні.
- **Що стало.** Одна функція `is_permanent(e)` і константа `NO_RETRY_REASONS` у `src/tradedesk/core/errors.py`, поруч із `ProviderError`, мапінг якого (`Http`: 404 → `data_unavailable`, інші 4xx → `HTTP <code>: …`) вона й розбирає. Скрипти імпортують її; `_retry` у них не змінено. Не в `scripts/common_helpers.py`, бо той лише на stdlib (панель запускається системним `python3` без `tradedesk`).
- **Поведінка та сама:** ті самі причини без повтору, будь-який HTTP 4xx у `detail`, крім 429, — постійна помилка; решта (таймаути, 5xx, не `ProviderError`) — тимчасова.
- **Тести** (`tests/test_pm_threshold.py`): наявні перевірки класифікації через справжній `Http` (400/403/404/422/429/500/502) тепер викликають спільну функцію; новий тест на всі причини, `HTTP 418/429/4000`, порожній `detail` і не-провайдерські винятки; тест-охоронець, що три скрипти беруть саме спільну функцію й локальних копій немає.
- `make lint` чисто; `test_pm_threshold.py`, `test_core.py`, `test_scripts_import.py` — 312 passed; `test_common_helpers.py`, `test_scripts_import_safe.py` — 113 passed.
