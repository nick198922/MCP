**Крок:** p — постійна картка `standing-improvements` (відкритий лише 8a, що чекає сервера); додано пункт 119.
**Результат:**
• 22 прямі тести для `analytics/calendar.py` (`classify_expiry`, `funding_schedule`, `filter_macro`), еталони порахував вручну.
• Помилок не знайдено, `src/` не змінено.
• Модулі без прямого імпорту в тестах: `sentiment`, `trades` (записано в BACKLOG 6.9).
• `make lint` чистий, `make test`: 4813 passed, 3 skipped.
**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-05-analytics-direct-tests.md).
**Далі:** `owner`. Нових питань немає. В очікуванні лишається гілка `agent/2026-11-04-library-seller-exhaustion-card`.
