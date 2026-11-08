**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`, пункт 203 (додано: відкритим лишався лише заблокований 8a). Інших розблокованих карток із пріоритетом власника не знайдено.

**Результат:**
• `tests/test_analytics_direct_11.py`: 12 тестів на 11 функцій `analytics/` (`json_num`, `et_midnight`, `et_time`, `et_date`, `title_direction`, `parse_expiry`, `parse_ts`, `month_start`, `trend_ok`, `macd_hist`, `is_second_to_last_day`).
• Еталони: реальні фікстури, календарні факти, ручні розрахунки. Помилок не знайдено, `src/` не змінено.
• Без тесту лишилось 22 із 2173 функцій (BACKLOG 6.9).
• Нотатка без зміни коду: `json_num` пропускає NaN/inf (на числа не впливає).
• Мої перевірки: `make lint` чистий, `make test` — 6868 passed, 3 skipped.

**Критик:** approve (sonnet), блокерів немає. Дві дрібниці: позиційне створення `Ind` у тесті, один довгий рядок. [Рев'ю](../../reviews/2026-11-07-analytics-tests-8.md).

**Далі:** owner. Нових питань немає.
